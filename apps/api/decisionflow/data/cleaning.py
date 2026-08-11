"""Cleaning: decide what to fix, then fix it in one pass.

Two stages, deliberately separate.

`plan_cleaning` reads a profile and decides what *should* happen, producing a
list of typed actions. It touches no data and is trivially testable.

`build_clean_sql` compiles that plan into a single `CREATE TABLE ... AS SELECT`
against the raw table. One statement, executed by DuckDB, vectorised across
every row — as opposed to iterating rows in Python, which for a million-row
table is the difference between a second and several minutes.

The plan is also the audit trail. A BI tool that silently rewrites a customer's
numbers is worse than one that does nothing, so every action is recorded on the
dataset and shown back to the user.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from decisionflow.data.profiling import NULL_TOKENS, ColumnProfile
from decisionflow.data.warehouse import validate_identifier
from decisionflow.db.models.ingestion import ColumnType

# A coercion is only applied when nearly every value supports it. Below this,
# converting would turn the minority into NULLs — destroying data to tidy a
# column, which is not a trade worth making automatically.
COERCION_THRESHOLD = 0.95


class CleaningAction(StrEnum):
    TRIM_WHITESPACE = "trim_whitespace"
    NORMALIZE_NULL_TOKENS = "normalize_null_tokens"
    CAST_TO_NUMBER = "cast_to_number"
    CAST_TO_DATE = "cast_to_date"
    CAST_TO_BOOLEAN = "cast_to_boolean"
    DEDUPLICATE_ROWS = "deduplicate_rows"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _sentence(reasons: list[str]) -> str:
    """Join reasons into one sentence, capitalising only the first letter.

    Deliberately not `str.capitalize()`, which also *lowercases the remainder*
    — it turned "'NA'" into "'na'" in text shown directly to users.
    """
    if not reasons:
        return ""
    joined = "; ".join(reasons)
    return joined[0].upper() + joined[1:] + "."


@dataclass(slots=True)
class ColumnPlan:
    """What to do with one column, and why."""

    column: str
    actions: list[CleaningAction]
    resulting_type: ColumnType
    reason: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actions"] = [action.value for action in self.actions]
        data["resulting_type"] = self.resulting_type.value
        return data


@dataclass(slots=True)
class CleaningPlan:
    columns: list[ColumnPlan]
    deduplicate: bool

    def to_actions(self) -> list[dict[str, Any]]:
        """Flat, JSON-safe record of everything this plan changes."""
        actions: list[dict[str, Any]] = [
            plan.to_dict() for plan in self.columns if plan.actions
        ]
        if self.deduplicate:
            actions.append(
                {
                    "column": None,
                    "actions": [CleaningAction.DEDUPLICATE_ROWS.value],
                    "resulting_type": None,
                    "reason": "Exact duplicate rows were removed.",
                }
            )
        return actions

    @property
    def is_noop(self) -> bool:
        return not self.deduplicate and all(not plan.actions for plan in self.columns)


def plan_column(
    *, column: str, column_type: ColumnType, profile: ColumnProfile
) -> ColumnPlan:
    """Decide the transforms for one column from its profile."""
    actions: list[CleaningAction] = []
    resulting_type = column_type
    reasons: list[str] = []

    if column_type is not ColumnType.STRING:
        # Non-text columns were already parsed correctly by the reader; there
        # is nothing to repair without inventing values.
        return ColumnPlan(column=column, actions=[], resulting_type=column_type, reason="")

    # Trimming is decided by padded values, not by blank ones. A column of
    # " Ada Lovelace " contains no blanks whatsoever and still needs trimming —
    # conflating the two silently leaves whitespace in every such column.
    if profile.whitespace_count:
        actions.append(CleaningAction.TRIM_WHITESPACE)
        reasons.append(
            f"{_plural(profile.whitespace_count, 'value')} had surrounding "
            "whitespace removed"
        )

    if profile.null_token_count:
        # Trimming must precede the comparison, or " N/A " never matches.
        if CleaningAction.TRIM_WHITESPACE not in actions:
            actions.append(CleaningAction.TRIM_WHITESPACE)
        actions.append(CleaningAction.NORMALIZE_NULL_TOKENS)
        reasons.append(
            f"{_plural(profile.null_token_count, 'placeholder value')} "
            "(blank, 'NA', '-', …) became NULL"
        )

    # Recover columns that are numbers or dates wearing a text disguise. Order
    # matters: check dates before numbers, because "2026" parses as both and a
    # year column is far more useful as a date than as an integer.
    if (profile.boolean_like_fraction or 0) >= COERCION_THRESHOLD:
        actions.append(CleaningAction.CAST_TO_BOOLEAN)
        resulting_type = ColumnType.BOOLEAN
        reasons.append("values were recognised as true/false and converted to boolean")
    elif (profile.date_like_fraction or 0) >= COERCION_THRESHOLD:
        actions.append(CleaningAction.CAST_TO_DATE)
        resulting_type = ColumnType.DATE
        reasons.append("values parsed as dates and were converted from text")
    elif (profile.numeric_like_fraction or 0) >= COERCION_THRESHOLD:
        actions.append(CleaningAction.CAST_TO_NUMBER)
        resulting_type = ColumnType.DECIMAL
        reasons.append(
            "values were numeric once currency symbols and separators were removed"
        )

    return ColumnPlan(
        column=column,
        actions=actions,
        resulting_type=resulting_type,
        reason=_sentence(reasons),
    )


def plan_cleaning(
    columns: list[tuple[str, ColumnType, ColumnProfile]],
    *,
    duplicate_rows: int,
    deduplicate: bool = True,
) -> CleaningPlan:
    return CleaningPlan(
        columns=[
            plan_column(column=name, column_type=column_type, profile=profile)
            for name, column_type, profile in columns
        ],
        deduplicate=deduplicate and duplicate_rows > 0,
    )


# --------------------------------------------------------------------------
# SQL compilation
# --------------------------------------------------------------------------
def _null_token_list() -> str:
    return ", ".join(f"'{token}'" for token in sorted(NULL_TOKENS))


def _column_expression(plan: ColumnPlan) -> str:
    """The SELECT expression implementing one column's plan.

    Built up in layers so each action composes with the next: trim, then blank
    out placeholders, then cast whatever survives.
    """
    column = validate_identifier(plan.column)

    if not plan.actions:
        return column

    expression = column

    if CleaningAction.TRIM_WHITESPACE in plan.actions:
        expression = f"trim({expression})"

    if CleaningAction.NORMALIZE_NULL_TOKENS in plan.actions:
        expression = (
            f"CASE WHEN lower({expression}) IN ({_null_token_list()}) "
            f"THEN NULL ELSE {expression} END"
        )

    if CleaningAction.CAST_TO_BOOLEAN in plan.actions:
        expression = (
            f"CASE WHEN lower({expression}) IN ('true','yes','y','t','1') THEN TRUE "
            f"WHEN lower({expression}) IN ('false','no','n','f','0') THEN FALSE "
            f"ELSE NULL END"
        )
    elif CleaningAction.CAST_TO_DATE in plan.actions:
        # TRY_CAST rather than CAST: a stray unparseable value becomes NULL
        # instead of aborting the whole load.
        expression = f"TRY_CAST({expression} AS DATE)"
    elif CleaningAction.CAST_TO_NUMBER in plan.actions:
        # Strip currency symbols, thousands separators and spaces before
        # casting, so "$1,234.56" survives as 1234.56.
        expression = (
            f"TRY_CAST(regexp_replace({expression}, '[$€£,\\s]', '', 'g') AS DOUBLE)"
        )

    return f"{expression} AS {column}"


def build_clean_sql(
    plan: CleaningPlan, *, source_schema: str, target_schema: str, table: str
) -> str:
    """Compile the plan into one CREATE TABLE AS SELECT."""
    validate_identifier(source_schema)
    validate_identifier(target_schema)
    validate_identifier(table)

    projection = ", ".join(_column_expression(column) for column in plan.columns)
    distinct = "DISTINCT " if plan.deduplicate else ""

    # Suppression is safe: schemas and table passed validate_identifier above,
    # and every column expression was built from validated identifiers too — no
    # customer-supplied text reaches this string.
    return (
        f"CREATE OR REPLACE TABLE {target_schema}.{table} AS "  # noqa: S608
        f"SELECT {distinct}{projection} FROM {source_schema}.{table}"
    )
