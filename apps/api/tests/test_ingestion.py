"""Dataset upload, ingestion, and inspection.

The ingestion job is invoked directly rather than through ARQ: the worker is a
separate process, and starting one per test would trade a great deal of
flakiness for very little extra coverage. What matters is that the same
function the worker calls does the right thing.
"""

from __future__ import annotations

import io
import uuid

import pytest
from httpx import AsyncClient

from decisionflow.data import warehouse
from decisionflow.data.schema import detect_schema, normalize_column_name, read_full
from decisionflow.db.models.ingestion import ColumnType, DatasetStatus
from decisionflow.db.session import TenantContext, tenant_session
from decisionflow.services import ingestion as ingestion_service
from tests.conftest import auth_header, register_account

SALES_CSV = b"""order_id,Customer Name,Order Date,Total Revenue (USD),Units,Is Repeat
1001,Ada Lovelace,2026-01-15,249.99,3,true
1002,Grace Hopper,2026-01-16,1520.50,12,false
1003,Alan Turing,2026-02-01,89.00,1,true
1004,Katherine Johnson,2026-02-14,430.25,5,false
1005,Ada Lovelace,2026-03-02,,2,true
"""


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    content: bytes = SALES_CSV,
    filename: str = "sales.csv",
) -> dict:
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": (filename, io.BytesIO(content), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["dataset"]


async def _run_ingestion(org_id: uuid.UUID, dataset_id: uuid.UUID) -> None:
    """Invoke the worker's ingestion path directly."""
    async with tenant_session(TenantContext(org_id=org_id)) as session:
        await ingestion_service.ingest_dataset(session, dataset_id=dataset_id)


# --------------------------------------------------------------------------
# Pure helpers — no infrastructure needed
# --------------------------------------------------------------------------
def test_column_names_are_normalised() -> None:
    taken: set[str] = set()
    assert normalize_column_name("Total Revenue (USD)", position=0, taken=taken) == (
        "total_revenue_usd"
    )
    assert normalize_column_name("Order Date", position=1, taken=taken) == "order_date"


def test_duplicate_headers_get_distinct_names() -> None:
    """Spreadsheets very often repeat a header; the table cannot."""
    taken: set[str] = set()
    first = normalize_column_name("Amount", position=0, taken=taken)
    second = normalize_column_name("Amount", position=1, taken=taken)
    assert first == "amount"
    assert second == "amount_2"


def test_blank_and_numeric_headers_become_valid_identifiers() -> None:
    taken: set[str] = set()
    assert normalize_column_name("", position=3, taken=taken) == "column_4"
    assert normalize_column_name("2026", position=1, taken=taken) == "col_2026"
    # A SQL keyword would otherwise produce DDL that fails to parse.
    assert normalize_column_name("select", position=2, taken=taken) == "select_col"


def test_normalised_names_are_accepted_by_the_warehouse_validator() -> None:
    """The two rules must agree, or ingestion fails on legitimate files."""
    taken: set[str] = set()
    for header in ("Total Revenue (USD)", "  spaced  ", "ÉTÉ 2026", "", "select", "99 bottles"):
        name = normalize_column_name(header, position=0, taken=taken)
        assert warehouse.validate_identifier(name) == name


def test_schema_detection_infers_types(tmp_path) -> None:
    path = tmp_path / "sales.csv"
    path.write_bytes(SALES_CSV)

    detected = detect_schema(read_full(path))
    by_name = {column.normalized_name: column for column in detected.columns}

    assert detected.row_count == 5
    assert by_name["order_id"].data_type is ColumnType.INTEGER
    assert by_name["customer_name"].data_type is ColumnType.STRING
    assert by_name["order_date"].data_type is ColumnType.DATE
    assert by_name["total_revenue_usd"].data_type is ColumnType.DECIMAL
    assert by_name["is_repeat"].data_type is ColumnType.BOOLEAN

    # The blank revenue cell in the last row must be reported as nullable.
    assert by_name["total_revenue_usd"].nullable is True
    assert by_name["order_id"].nullable is False


def test_original_column_names_are_preserved(tmp_path) -> None:
    """The raw header carries meaning the normalised form loses."""
    path = tmp_path / "sales.csv"
    path.write_bytes(SALES_CSV)

    detected = detect_schema(read_full(path))
    originals = {column.name for column in detected.columns}
    assert "Total Revenue (USD)" in originals


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
async def test_upload_registers_a_dataset(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    dataset = await _upload(client, auth_header(tokens))

    assert dataset["status"] == DatasetStatus.UPLOADED.value
    assert dataset["original_filename"] == "sales.csv"
    assert dataset["size_bytes"] == len(SALES_CSV)
    # Not known until ingestion has run.
    assert dataset["row_count"] is None


async def test_upload_rejects_unsupported_file_types(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("notes.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
        headers=auth_header(tokens),
    )
    assert response.status_code == 422


async def test_upload_rejects_an_empty_file(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")},
        headers=auth_header(tokens),
    )
    assert response.status_code == 422


async def test_upload_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("sales.csv", io.BytesIO(SALES_CSV), "text/csv")},
    )
    assert response.status_code == 401


async def test_viewer_cannot_upload(client: AsyncClient, unique_email) -> None:
    """Uploading changes the workspace's data, so it needs analyst or above."""
    owner = await register_account(client, unique_email())
    viewer_email = unique_email()

    invite = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": viewer_email, "role": "viewer"},
        headers=auth_header(owner),
    )
    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": invite.json()["invite_token"], "password": "correct-horse-battery-staple"},
    )

    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("sales.csv", io.BytesIO(SALES_CSV), "text/csv")},
        headers=auth_header(accepted.json()),
    )
    assert response.status_code == 403


async def test_repeated_uploads_get_distinct_slugs(client: AsyncClient, unique_email) -> None:
    """Two files named sales.csv cannot share one physical table."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)

    first = await _upload(client, headers)
    second = await _upload(client, headers)

    assert first["slug"] != second["slug"]


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
async def test_ingestion_populates_schema_and_rows(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset = await _upload(client, headers)
    await _run_ingestion(org_id, uuid.UUID(dataset["id"]))

    detail = await client.get(f"/api/v1/datasets/{dataset['id']}", headers=headers)
    assert detail.status_code == 200

    body = detail.json()
    assert body["status"] == DatasetStatus.READY.value
    assert body["row_count"] == 5
    assert body["column_count"] == 6

    names = [column["normalized_name"] for column in body["columns"]]
    assert names == [
        "order_id",
        "customer_name",
        "order_date",
        "total_revenue_usd",
        "units",
        "is_repeat",
    ]


async def test_ingestion_records_a_successful_run(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset = await _upload(client, headers)
    await _run_ingestion(org_id, uuid.UUID(dataset["id"]))

    runs = await client.get(f"/api/v1/datasets/{dataset['id']}/runs", headers=headers)
    assert runs.status_code == 200

    payload = runs.json()
    assert len(payload) == 1
    assert payload[0]["status"] == "succeeded"
    assert payload[0]["rows_ingested"] == 5
    assert payload[0]["duration_seconds"] is not None


async def test_preview_returns_real_rows(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset = await _upload(client, headers)
    await _run_ingestion(org_id, uuid.UUID(dataset["id"]))

    response = await client.get(
        f"/api/v1/datasets/{dataset['id']}/preview?limit=2", headers=headers
    )
    assert response.status_code == 200

    body = response.json()
    # Only ingestion ran here, so preview falls back to the raw layer and says so.
    assert body["layer"] == "raw"
    assert len(body["rows"]) == 2
    assert body["total_rows"] == 5
    assert body["rows"][0]["customer_name"] == "Ada Lovelace"
    assert body["rows"][0]["order_id"] == 1001


async def test_preview_is_refused_before_ingestion(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)

    dataset = await _upload(client, headers)
    response = await client.get(f"/api/v1/datasets/{dataset['id']}/preview", headers=headers)
    assert response.status_code == 422


async def test_reingestion_is_idempotent(client: AsyncClient, unique_email) -> None:
    """Re-running must replace the table, not append to it."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset = await _upload(client, headers)
    dataset_id = uuid.UUID(dataset["id"])

    await _run_ingestion(org_id, dataset_id)
    await _run_ingestion(org_id, dataset_id)

    detail = await client.get(f"/api/v1/datasets/{dataset['id']}", headers=headers)
    assert detail.json()["row_count"] == 5
    assert len(detail.json()["columns"]) == 6, "columns must be replaced, not duplicated"

    runs = await client.get(f"/api/v1/datasets/{dataset['id']}/runs", headers=headers)
    assert len(runs.json()) == 2, "each attempt is recorded"


async def test_ingestion_failure_is_recorded(client: AsyncClient, unique_email) -> None:
    """A failed job must leave the reason where the user can see it."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset = await _upload(client, headers)
    dataset_id = uuid.UUID(dataset["id"])

    # Point the dataset at an object that does not exist, so the download fails.
    async with tenant_session(TenantContext(org_id=org_id)) as session:
        row = await ingestion_service.get_dataset(
            session, org_id=org_id, dataset_id=dataset_id
        )
        row.storage_key = "orgs/missing/does-not-exist.csv"
        await session.commit()

    with pytest.raises(Exception):  # noqa: B017 - the specific type is not the point
        await _run_ingestion(org_id, dataset_id)

    detail = await client.get(f"/api/v1/datasets/{dataset['id']}", headers=headers)
    assert detail.json()["status"] == DatasetStatus.FAILED.value
    assert detail.json()["status_message"]

    runs = await client.get(f"/api/v1/datasets/{dataset['id']}/runs", headers=headers)
    assert runs.json()[0]["status"] == "failed"
    assert runs.json()[0]["error_message"]


# --------------------------------------------------------------------------
# Isolation and deletion
# --------------------------------------------------------------------------
async def test_datasets_are_invisible_across_workspaces(
    client: AsyncClient, unique_email
) -> None:
    """The whole point of the RLS layer, exercised through the API."""
    owner = await register_account(client, unique_email())
    dataset = await _upload(client, auth_header(owner))

    outsider = await register_account(client, unique_email())
    outsider_headers = auth_header(outsider)

    listed = await client.get("/api/v1/datasets", headers=outsider_headers)
    assert listed.json() == []

    fetched = await client.get(f"/api/v1/datasets/{dataset['id']}", headers=outsider_headers)
    assert fetched.status_code == 404


async def test_each_workspace_gets_its_own_warehouse_file(
    client: AsyncClient, unique_email
) -> None:
    """Tenant separation for analytical data is physical, not just logical."""
    first = await register_account(client, unique_email())
    second = await register_account(client, unique_email())

    first_org = uuid.UUID(first["active_org_id"])
    second_org = uuid.UUID(second["active_org_id"])

    await _run_ingestion(first_org, uuid.UUID((await _upload(client, auth_header(first)))["id"]))

    assert warehouse.warehouse_path(first_org) != warehouse.warehouse_path(second_org)
    assert warehouse.warehouse_path(first_org).exists()


async def test_delete_removes_dataset_and_table(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset = await _upload(client, headers)
    await _run_ingestion(org_id, uuid.UUID(dataset["id"]))

    slug = dataset["slug"]
    assert await warehouse.table_exists(org_id, warehouse.RAW_SCHEMA, slug)

    deleted = await client.delete(f"/api/v1/datasets/{dataset['id']}", headers=headers)
    assert deleted.status_code == 204

    assert not await warehouse.table_exists(org_id, warehouse.RAW_SCHEMA, slug)
    gone = await client.get(f"/api/v1/datasets/{dataset['id']}", headers=headers)
    assert gone.status_code == 404
