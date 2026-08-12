"""The analyst agent: question in, grounded answer out.

    question → SQL → guard → execute → narrate

Two model calls, not one. Asking a model to both write SQL and describe results
in a single turn means it narrates results it has not seen — which is exactly
how a plausible, fabricated number reaches an executive. Here the second call
receives the real rows and nothing else to invent from.

The retry loop matters as much as the guard. When generated SQL is rejected or
fails to execute, the specific reason goes back to the model as correction
context. "COPY is not permitted" produces a fixed query; "invalid query"
produces the same query again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decisionflow.core.errors import UnsafeQueryError
from decisionflow.core.logging import get_logger
from decisionflow.data import sqlguard, warehouse
from decisionflow.data.semantics import DatasetSemantics
from decisionflow.data.warehouse import QueryTimeoutError
from decisionflow.db.models.ingestion import Dataset, DatasetColumn
from decisionflow.llm import prompts, provider

log = get_logger(__name__)

# One retry is worth it — the common failures (a hallucinated column, a
# forbidden function) are exactly what a specific error message fixes. Beyond
# two attempts the model is usually stuck, and each try costs a call.
MAX_SQL_ATTEMPTS = 2

# Schema for the SQL-generation call. Constraining generation beats asking for
# JSON and hoping.
_SQL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "nullable": True},
        "explanation": {"type": "string"},
    },
    "required": ["explanation"],
}


@dataclass(slots=True)
class AgentAnswer:
    """Everything the agent produced, including how it got there."""

    question: str
    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    explanation: str | None = None
    model: str | None = None
    attempts: int = 1
    # Rejections and execution errors along the way. Surfaced rather than
    # hidden: a user who sees "I retried because X" trusts the result more
    # than one shown a number with no provenance.
    corrections: list[str] = field(default_factory=list)
    answerable: bool = True
    input_tokens: int | None = None
    output_tokens: int | None = None


async def ask(
    *,
    question: str,
    dataset: Dataset,
    columns: list[DatasetColumn],
    semantics: DatasetSemantics,
    table: str | None = None,
    extra_columns: list[tuple[str, str]] | None = None,
) -> AgentAnswer:
    """Answer a natural-language question about one dataset.

    `table` may be a star view rather than the plain cleaned table, in which
    case `extra_columns` describes the dimension columns the join brings in.
    Those columns do not exist on the dataset itself, so without them the model
    has no way to know it can group by one.
    """
    table = table or f"{warehouse.CLEAN_SCHEMA}.{dataset.slug}"
    schema_block = prompts.describe_dataset(
        dataset, columns, semantics, table, extra_columns=extra_columns
    )
    allowed = {table, dataset.slug, table.split(".")[-1]}

    correction: str | None = None
    corrections: list[str] = []
    total_input = 0
    total_output = 0

    for attempt in range(1, MAX_SQL_ATTEMPTS + 1):
        completion = await provider.complete(
            prompts.build_sql_prompt(
                question=question,
                schema_block=schema_block,
                table=table,
                correction=correction,
            ),
            system_instruction=prompts.SQL_SYSTEM_INSTRUCTION,
            response_schema=_SQL_RESPONSE_SCHEMA,
        )
        total_input += completion.input_tokens or 0
        total_output += completion.output_tokens or 0

        payload = provider.parse_json(completion.text)
        raw_sql = payload.get("sql")
        explanation = str(payload.get("explanation") or "").strip()

        # The model is explicitly allowed to decline. A refusal is a correct
        # answer when the columns cannot support the question, and far better
        # than a confident query over the wrong field.
        if not raw_sql:
            return AgentAnswer(
                question=question,
                answer=explanation or "That question cannot be answered from this dataset.",
                explanation=explanation,
                model=completion.model,
                attempts=attempt,
                corrections=corrections,
                answerable=False,
                input_tokens=total_input or None,
                output_tokens=total_output or None,
            )

        try:
            safe = sqlguard.validate(str(raw_sql), allowed_tables=allowed)
            rows = await warehouse.run_sandboxed(dataset.org_id, safe.sql)
        except (UnsafeQueryError, QueryTimeoutError) as exc:
            correction = str(exc)
            corrections.append(correction)
            log.info("agent.sql_rejected", attempt=attempt, reason=correction)
            continue
        except Exception as exc:
            correction = f"The query failed to execute: {exc}"
            corrections.append(correction)
            log.info("agent.sql_failed", attempt=attempt, error=str(exc)[:200])
            continue

        narrative = await provider.complete(
            prompts.build_narrative_prompt(
                question=question, sql=safe.sql, rows=rows, row_limit=safe.limit
            ),
            system_instruction=prompts.NARRATIVE_SYSTEM_INSTRUCTION,
        )
        total_input += narrative.input_tokens or 0
        total_output += narrative.output_tokens or 0

        log.info(
            "agent.answered",
            attempts=attempt,
            rows=len(rows),
            dataset_id=str(dataset.id),
        )
        return AgentAnswer(
            question=question,
            answer=narrative.text,
            sql=safe.sql,
            rows=rows,
            row_count=len(rows),
            explanation=explanation,
            model=completion.model,
            attempts=attempt,
            corrections=corrections,
            input_tokens=total_input or None,
            output_tokens=total_output or None,
        )

    # Out of attempts. Report the failure honestly rather than inventing an
    # answer — a wrong number here is worse than no number.
    return AgentAnswer(
        question=question,
        answer=(
            "I could not produce a valid query for that question. "
            f"Last problem: {corrections[-1] if corrections else 'unknown'}"
        ),
        attempts=MAX_SQL_ATTEMPTS,
        corrections=corrections,
        answerable=False,
        input_tokens=total_input or None,
        output_tokens=total_output or None,
    )


async def summarise(dataset: Dataset, kpis: list[Any]) -> str:
    """A short executive summary over a dataset's headline metrics."""
    completion = await provider.complete_with_fallback(
        prompts.build_kpi_summary_prompt(dataset, kpis),
        system_instruction=prompts.NARRATIVE_SYSTEM_INSTRUCTION,
    )
    return completion.text
