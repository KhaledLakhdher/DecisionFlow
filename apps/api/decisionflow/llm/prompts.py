"""Prompt construction.

The whole point of Modules 2 and 4 was to make this possible. A model handed a
bare `CREATE TABLE` guesses: it does not know that `status` holds
'shipped'/'pending', that `revenue` was recovered from text, or that `notes` is
entirely empty and useless. Grounding it in the profile and the semantic layer
turns guesswork into lookup.

Prompts are built from stored facts rather than assembled ad hoc, so what the
model sees is exactly what the rest of the system believes.
"""

from __future__ import annotations

import json
from typing import Any

from decisionflow.data.semantics import DatasetSemantics, SemanticRole
from decisionflow.db.models.ingestion import Dataset, DatasetColumn, Kpi

SQL_SYSTEM_INSTRUCTION = """\
You are a precise analytics engineer. You translate business questions into a \
single DuckDB SQL SELECT statement.

Rules, without exception:
- Output exactly one SELECT statement. Never multiple statements.
- Never use DDL, DML, COPY, ATTACH, INSTALL, PRAGMA, or any file-reading \
function such as read_csv. They are blocked and will fail.
- Only reference the table you are given.
- Prefer explicit column names over SELECT *.
- Always alias aggregates with a readable name (e.g. `AS total_revenue`).
- When the question implies a time period, group by the date column.
- If the question cannot be answered from the available columns, say so \
instead of inventing a query.
"""

NARRATIVE_SYSTEM_INSTRUCTION = """\
You are a business analyst explaining figures to a non-technical executive.

- Lead with the answer, in one sentence.
- Quote the actual numbers from the result. Never invent or round misleadingly.
- Be brief: three sentences at most unless the data genuinely needs more.
- If the result is empty, say plainly that no matching records were found.
- Do not describe the SQL or the mechanics. The reader cares about the business.
- Do not speculate about causes the data cannot show.
"""


def _format_column(column: DatasetColumn) -> str:
    """One line describing a column, as densely as is useful."""
    profile: dict[str, Any] = column.profile or {}
    parts = [f"  - {column.normalized_name} ({column.effective_type.value}"]

    role = column.semantic_role
    if role:
        parts.append(f", {role}")
    parts.append(")")

    detail: list[str] = []
    if column.name != column.normalized_name:
        detail.append(f'originally "{column.name}"')

    null_fraction = profile.get("null_fraction")
    if isinstance(null_fraction, int | float) and null_fraction > 0.05:
        detail.append(f"{null_fraction * 100:.0f}% empty")

    # Concrete values are the single most useful thing a model can see: it
    # stops it inventing WHERE status = 'complete' when the data says 'shipped'.
    top_values = profile.get("top_values") or []
    if top_values:
        sample = ", ".join(str(item.get("value")) for item in top_values[:6])
        detail.append(f"values: {sample}")
    elif column.sample_values:
        sample = ", ".join(str(value) for value in column.sample_values[:4])
        detail.append(f"e.g. {sample}")

    minimum, maximum = profile.get("min_value"), profile.get("max_value")
    if minimum is not None and maximum is not None:
        detail.append(f"range {minimum:g} to {maximum:g}")

    min_date, max_date = profile.get("min_date"), profile.get("max_date")
    if min_date and max_date:
        detail.append(f"spans {min_date} to {max_date}")

    if detail:
        parts.append(" — " + "; ".join(detail))
    return "".join(parts)


def describe_dataset(
    dataset: Dataset,
    columns: list[DatasetColumn],
    semantics: DatasetSemantics,
    table: str,
    *,
    extra_columns: list[tuple[str, str]] | None = None,
) -> str:
    """The schema block shared by every prompt about this dataset."""
    lines = [
        f'Table: {table}  (dataset "{dataset.name}")',
        f"Rows: {dataset.clean_row_count or dataset.row_count or 0}",
        "",
        "Columns:",
    ]
    # Ignored columns are deliberately still listed, marked, so the model does
    # not "helpfully" invent a use for one it cannot see is empty.
    lines.extend(_format_column(column) for column in columns)

    hints: list[str] = []
    if semantics.revenue_column:
        hints.append(f"revenue: {semantics.revenue_column.column}")
    if semantics.time_column:
        hints.append(f"date axis: {semantics.time_column.column}")
    if semantics.customer_key:
        hints.append(f"customer key: {semantics.customer_key.column}")
    if semantics.order_key:
        hints.append(f"order key: {semantics.order_key.column}")
    dimensions = [column.column for column in semantics.dimensions]
    if dimensions:
        hints.append(f"groupable: {', '.join(dimensions)}")

    if hints:
        lines += ["", "Meaning:", *(f"  - {hint}" for hint in hints)]

    ignored = [column.column for column in semantics.by_role(SemanticRole.IGNORED)]
    if ignored:
        lines += [
            "",
            f"Unusable (empty or single-valued), do not use: {', '.join(ignored)}",
        ]

    if extra_columns:
        # These live only on the joined view. Listing them is what makes
        # "revenue by customer country" answerable when country was uploaded
        # in a separate file — the model cannot infer a column it never sees.
        lines += [
            "",
            "Columns joined in from related tables (already available on "
            f"{table}, no JOIN needed):",
            *(f"  - {name} — {description}" for name, description in extra_columns),
        ]

    return "\n".join(lines)


def build_sql_prompt(
    *, question: str, schema_block: str, table: str, correction: str | None = None
) -> str:
    """Prompt for translating a question into SQL."""
    sections = [
        schema_block,
        "",
        f"Question: {question}",
        "",
        f"Write one DuckDB SELECT statement against {table} that answers it.",
    ]

    if correction:
        # Feeding the specific rejection back is what makes the retry useful;
        # "try again" produces the same query a second time.
        sections += [
            "",
            "Your previous attempt was rejected:",
            f"  {correction}",
            "Write a corrected query that avoids this.",
        ]

    sections += [
        "",
        "Respond with JSON: {\"sql\": \"...\", \"explanation\": \"one sentence\"}",
        'If the question cannot be answered from these columns, respond with '
        '{"sql": null, "explanation": "why not"}.',
    ]
    return "\n".join(sections)


def build_narrative_prompt(
    *, question: str, sql: str, rows: list[dict[str, Any]], row_limit: int
) -> str:
    """Prompt for turning a result set into a business answer."""
    if rows:
        # Truncated deliberately: a hundred rows of JSON crowds out the
        # question and costs tokens without improving the answer.
        shown = rows[:20]
        result_block = json.dumps(shown, indent=2, default=str)
        if len(rows) > len(shown):
            result_block += f"\n... and {len(rows) - len(shown)} more rows"
    else:
        result_block = "(no rows returned)"

    truncation_note = (
        f"\nNote: results were capped at {row_limit} rows." if len(rows) >= row_limit else ""
    )

    return (
        f"Question: {question}\n\n"
        f"Query run:\n{sql}\n\n"
        f"Result:\n{result_block}{truncation_note}\n\n"
        "Answer the question for a business reader, using these numbers."
    )


def build_kpi_summary_prompt(dataset: Dataset, kpis: list[Kpi]) -> str:
    """Prompt for the executive summary over a dataset's headline metrics."""
    lines = [f'Dataset: "{dataset.name}"', ""]
    for kpi in kpis:
        if kpi.value is None:
            continue
        line = f"- {kpi.label}: {kpi.value:,.2f} ({kpi.format})"
        growth = (kpi.details or {}).get("growth") or {}
        change = growth.get("change")
        if change is not None:
            line += f", {change * 100:+.1f}% versus the previous period"
        lines.append(line)

    if dataset.quality_score is not None:
        lines += ["", f"Data quality score: {dataset.quality_score}/100"]

    lines += [
        "",
        "Write a short executive summary of this business's position. "
        "Lead with the most important figure. Mention one thing worth watching. "
        "Four sentences at most.",
    ]
    return "\n".join(lines)
