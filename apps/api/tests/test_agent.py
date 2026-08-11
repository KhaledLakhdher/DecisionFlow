"""The analyst agent.

The model is stubbed here rather than called. Not to avoid cost, but because a
test whose outcome depends on a language model is not a test — it cannot fail
deterministically, and a green run proves nothing about the code. What is
tested is the machinery around the model: the guard, the retry loop, refusal
handling, and persistence.

The live model is exercised separately, end to end, against the real API.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from decisionflow.core.errors import LLMUnavailableError
from decisionflow.db.session import TenantContext, tenant_session
from decisionflow.llm import provider
from decisionflow.services import analytics as analytics_service
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import pipeline as pipeline_service
from tests.conftest import auth_header, register_account

SALES_CSV = b"""order_id,customer_id,Order Date,Revenue,Region
1001,C-001,2026-01-15,1200.00,North
1002,C-002,2026-01-20,800.00,South
1003,C-001,2026-02-05,1500.00,North
1004,C-003,2026-02-18,400.00,East
"""


class FakeProvider:
    """Scripted model responses, in order.

    Each entry is either a dict (returned as the JSON body of a SQL call) or a
    string (returned as narrative prose).
    """

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, **kwargs: Any) -> provider.Completion:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        item = self.responses.pop(0)
        text = json.dumps(item) if isinstance(item, dict) else str(item)
        return provider.Completion(text=text, model="fake-model")


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a scripted provider and hand back the installer."""

    def install(*responses: Any) -> FakeProvider:
        fake = FakeProvider(*responses)
        monkeypatch.setattr(provider, "complete", fake)
        monkeypatch.setattr("decisionflow.llm.agent.provider.complete", fake)
        return fake

    return install


async def _ready_dataset(client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID) -> str:
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("sales.csv", io.BytesIO(SALES_CSV), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    dataset_id = uuid.UUID(response.json()["dataset"]["id"])

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        await ingestion_service.ingest_dataset(session, dataset_id=dataset_id)
        await pipeline_service.clean_dataset(session, dataset_id=dataset_id)
        await analytics_service.analyse_dataset(session, dataset_id=dataset_id)

    return str(dataset_id)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------
async def test_question_produces_a_grounded_answer(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake = fake_llm(
        {"sql": "SELECT sum(revenue) AS total FROM clean.sales", "explanation": "Sums revenue."},
        "Total revenue is $3,900.00 across four orders.",
    )

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "What is total revenue?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["answerable"] is True
    assert body["sql"].startswith("SELECT sum(revenue)")
    # The narrative model must receive the *actual* rows, not be asked to guess.
    assert body["rows"] == [{"total": 3900.0}]
    assert "3,900" in body["answer"]
    assert "3900" in fake.prompts[-1], "narrative prompt must contain the real result"


async def test_schema_and_real_values_reach_the_prompt(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    """Grounding is the point of Modules 2 and 4; verify it actually lands."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake = fake_llm(
        {"sql": "SELECT count(*) AS n FROM clean.sales", "explanation": "Counts rows."},
        "There are four records.",
    )

    await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "How many records?"},
        headers=headers,
    )

    prompt = fake.prompts[0]
    assert "revenue" in prompt and "region" in prompt
    # Concrete values stop the model inventing WHERE region = 'Northern'.
    assert "North" in prompt
    # Semantic roles tell it which column means money.
    assert "measure" in prompt and "identifier" in prompt


# --------------------------------------------------------------------------
# The guard, reached through the agent
# --------------------------------------------------------------------------
async def test_dangerous_sql_is_rejected_and_retried(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    """The model's first attempt tries to read a file; the guard stops it."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake = fake_llm(
        {"sql": "SELECT * FROM read_csv('/etc/passwd')", "explanation": "Reads a file."},
        {"sql": "SELECT count(*) AS n FROM clean.sales", "explanation": "Counts rows."},
        "There are four records.",
    )

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "Show me the passwords"},
        headers=headers,
    )
    body = response.json()

    assert body["attempts"] == 2
    assert body["answerable"] is True
    assert "read_csv" not in (body["sql"] or "")
    assert any("not permitted" in correction for correction in body["corrections"])
    # The specific reason must reach the model, or the retry repeats the query.
    assert "not permitted" in fake.prompts[1]


async def test_statement_chaining_never_executes(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake_llm(
        {"sql": "SELECT 1; DROP TABLE clean.sales", "explanation": "Oops."},
        {"sql": "SELECT count(*) AS n FROM clean.sales", "explanation": "Counts rows."},
        "Four records.",
    )

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "Drop everything"},
        headers=headers,
    )
    assert response.status_code == 200

    # The table must still be there.
    preview = await client.get(
        f"/api/v1/datasets/{dataset_id}/preview?layer=clean", headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["rows"]


async def test_exhausted_retries_report_failure_rather_than_inventing(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    """A wrong number is worse than no number."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake_llm(
        {"sql": "DROP TABLE clean.sales", "explanation": "nope"},
        {"sql": "COPY (SELECT 1) TO '/tmp/x.csv'", "explanation": "nope"},
    )

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "Break things"},
        headers=headers,
    )
    body = response.json()

    assert body["answerable"] is False
    assert body["sql"] is None
    assert body["attempts"] == 2
    assert len(body["corrections"]) == 2


async def test_invalid_column_error_is_fed_back(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    """Execution failures are retryable, not fatal."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake = fake_llm(
        {"sql": "SELECT sum(profit) AS p FROM clean.sales", "explanation": "Hallucinated column."},
        {"sql": "SELECT sum(revenue) AS total FROM clean.sales", "explanation": "Correct."},
        "Total revenue is $3,900.00.",
    )

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "What is total profit?"},
        headers=headers,
    )
    body = response.json()

    assert body["answerable"] is True
    assert body["attempts"] == 2
    assert "failed to execute" in body["corrections"][0]
    assert "profit" in fake.prompts[1], "the failing query's error must reach the model"


# --------------------------------------------------------------------------
# Refusal
# --------------------------------------------------------------------------
async def test_model_may_decline_an_unanswerable_question(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    """Declining is a correct answer, not an error.

    Far better than a confident query over the wrong column.
    """
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake_llm({"sql": None, "explanation": "This dataset has no employee data."})

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "How many employees do we have?"},
        headers=headers,
    )
    body = response.json()

    assert response.status_code == 200, "a refusal is a valid outcome, not a failure"
    assert body["answerable"] is False
    assert body["sql"] is None
    assert "employee" in body["answer"]


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
async def test_exchange_is_recorded_with_its_provenance(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake_llm(
        {"sql": "SELECT sum(revenue) AS total FROM clean.sales", "explanation": "Sums revenue."},
        "Total revenue is $3,900.00.",
    )

    answer = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "What is total revenue?"},
        headers=headers,
    )
    conversation_id = answer.json()["conversation_id"]

    messages = (
        await client.get(
            f"/api/v1/datasets/{dataset_id}/conversations/{conversation_id}", headers=headers
        )
    ).json()

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "What is total revenue?"
    # Provenance: an AI figure with no traceable query should not be trusted.
    assert messages[1]["sql"].startswith("SELECT sum(revenue)")
    assert messages[1]["row_count"] == 1


async def test_followup_continues_the_same_conversation(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, headers, org_id)

    fake_llm(
        {"sql": "SELECT count(*) AS n FROM clean.sales", "explanation": "a"},
        "Four.",
        {"sql": "SELECT sum(revenue) AS total FROM clean.sales", "explanation": "b"},
        "$3,900.00.",
    )

    first = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "How many orders?"},
        headers=headers,
    )
    conversation_id = first.json()["conversation_id"]

    second = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "And total revenue?", "conversation_id": conversation_id},
        headers=headers,
    )
    assert second.json()["conversation_id"] == conversation_id

    messages = (
        await client.get(
            f"/api/v1/datasets/{dataset_id}/conversations/{conversation_id}", headers=headers
        )
    ).json()
    assert len(messages) == 4


# --------------------------------------------------------------------------
# Access control and preconditions
# --------------------------------------------------------------------------
async def test_conversations_are_invisible_across_workspaces(
    client: AsyncClient, unique_email, fake_llm
) -> None:
    tokens = await register_account(client, unique_email())
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _ready_dataset(client, auth_header(tokens), org_id)

    fake_llm(
        {"sql": "SELECT count(*) AS n FROM clean.sales", "explanation": "a"},
        "Four.",
    )
    await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "How many?"},
        headers=auth_header(tokens),
    )

    outsider = await register_account(client, unique_email())
    response = await client.get(
        f"/api/v1/datasets/{dataset_id}/conversations", headers=auth_header(outsider)
    )
    assert response.status_code == 404


async def test_asking_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        f"/api/v1/datasets/{uuid.uuid4()}/ask", json={"question": "hello"}
    )
    assert response.status_code == 401


async def test_unprepared_dataset_cannot_be_questioned(
    client: AsyncClient, unique_email
) -> None:
    """Answering from a half-ingested table would produce a confident wrong number."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)

    upload = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("sales.csv", io.BytesIO(SALES_CSV), "text/csv")},
        headers=headers,
    )
    dataset_id = upload.json()["dataset"]["id"]

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/ask",
        json={"question": "What is total revenue?"},
        headers=headers,
    )
    assert response.status_code == 422


async def test_empty_question_is_rejected(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    response = await client.post(
        f"/api/v1/datasets/{uuid.uuid4()}/ask",
        json={"question": "   "},
        headers=auth_header(tokens),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Provider behaviour
# --------------------------------------------------------------------------
def test_malformed_json_is_a_clear_error() -> None:
    with pytest.raises(LLMUnavailableError, match="malformed JSON"):
        provider.parse_json("this is not json")


def test_json_fences_are_tolerated() -> None:
    assert provider.parse_json('```json\n{"sql": "SELECT 1"}\n```') == {"sql": "SELECT 1"}


def test_non_object_json_is_rejected() -> None:
    with pytest.raises(LLMUnavailableError, match="not an object"):
        provider.parse_json("[1, 2, 3]")
