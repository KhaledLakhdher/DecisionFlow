"""Contribution analysis: which segment explains a change.

This is the module that separates "AI business analyst" from "chatbot over
SQL". Asking a language model *why* revenue fell gets you a fluent guess —
seasonality, market conditions, a competitor — none of it derived from the
data. The actual answer is arithmetic: decompose the change across each
dimension and see where the movement is concentrated.

So the analysis here is deterministic and auditable. Every number a narrative
later quotes was computed by the SQL below, and the SQL travels with the
finding. The model's job is to route and to phrase, never to derive.

The other half of being useful is refusing to name a culprit when there isn't
one. A decline spread evenly over forty segments has no single cause, and a
system that always produces one is reliably wrong in the cases that matter
most. `concentration` is what decides that, and it is reported either way.
"""

from __future__ import annotations

from dataclasses import dataclass

from decisionflow.data.semantics import DatasetSemantics
from decisionflow.data.warehouse import validate_identifier

GRAINS = frozenset({"day", "week", "month", "quarter", "year"})

# The share of all movement one segment must carry before it is named as the
# cause. At 0.5 a single segment has to account for more absolute change than
# every other segment combined, which is a high bar on purpose: the failure
# mode being guarded against is confidently blaming the wrong thing.
CONCENTRATION_THRESHOLD = 0.5

# Segments listed in a finding. The full set still determines the statistics —
# truncating before computing them would overstate concentration badly.
MAX_SEGMENTS = 8

# Dimensions wider than this are identifiers wearing a dimension's hat. Ranking
# a change across ten thousand customer ids produces ten thousand tiny deltas
# and explains nothing.
MAX_CARDINALITY = 50

# When total movement exceeds the net change by this much, segments are largely
# cancelling each other out and "X% of the change" stops being a sensible
# frame — a region that grew 2,980 against a net rise of 290 is 1028% of the
# change, which is true and tells the reader nothing. Past this ratio the
# movements are reported in absolute terms instead.
OFFSET_RATIO = 2.0


@dataclass(frozen=True, slots=True)
class SegmentChange:
    """One segment's movement between the two periods."""

    segment: str
    current: float
    previous: float
    delta: float
    # Signed share of the net change. A segment that fell by 1,200 against a
    # total fall of 1,000 has a share of 1.2 — it more than explains the drop,
    # and something else partly offset it. That is worth seeing, so it is not
    # clamped.
    share: float | None


@dataclass(frozen=True, slots=True)
class DimensionExplanation:
    """How well one dimension accounts for a change."""

    dimension: str
    measure: str
    total_delta: float
    # Sum of absolute movement across every segment. The denominator for
    # concentration, and itself a useful signal: gross movement far larger than
    # the net change means segments are cancelling each other out.
    gross_movement: float
    concentration: float
    segments: list[SegmentChange]
    sql: str

    @property
    def concentrated(self) -> bool:
        """Whether a single segment carries enough movement to be the story.

        Strictly greater, not greater-or-equal. Two segments moving equally is
        the case this decides, and at exactly half the leader has not moved
        more than everything else combined — it has tied with it. Calling one
        of two equal contributors "the cause" is the error this guards.
        """
        return self.concentration > CONCENTRATION_THRESHOLD

    @property
    def leader(self) -> SegmentChange | None:
        return self.segments[0] if self.segments else None

    @property
    def offsetting(self) -> bool:
        """Whether segments are moving against each other.

        The net change is a poor summary when they are: "revenue up 9%" hides
        one region collapsing and another appearing. A reader told only the net
        figure would conclude nothing happened.
        """
        return self.gross_movement > OFFSET_RATIO * abs(self.total_delta)

    @property
    def riser(self) -> SegmentChange | None:
        """The largest increase, or None if nothing rose."""
        gains = [segment for segment in self.segments if segment.delta > 0]
        return max(gains, key=lambda segment: segment.delta) if gains else None

    @property
    def faller(self) -> SegmentChange | None:
        """The largest decrease, or None if nothing fell."""
        losses = [segment for segment in self.segments if segment.delta < 0]
        return min(losses, key=lambda segment: segment.delta) if losses else None


def build_contribution_sql(
    semantics: DatasetSemantics,
    *,
    schema: str,
    table: str,
    dimension: str,
    grain: str = "month",
) -> tuple[str, str] | None:
    """SQL comparing one dimension's segments across two periods.

    Returns the query and the measure's name, or None when the dataset has no
    time column to compare across.

    The query takes two parameters — the current and previous period — bound
    rather than interpolated. They are the only values here that do not come
    from a validated identifier.
    """
    time_column = semantics.time_column
    if time_column is None:
        return None
    if grain not in GRAINS:
        raise ValueError(f"Unsupported time grain: {grain}")

    validate_identifier(schema)
    validate_identifier(table)
    dimension_col = validate_identifier(dimension)
    time_col = validate_identifier(time_column.column)
    source = f"{schema}.{table}"

    revenue = semantics.revenue_column
    if revenue is not None:
        measure_sql = f"sum({validate_identifier(revenue.column)})"
        measure_name = revenue.column
    else:
        measure_sql = "count(*)"
        measure_name = "records"

    # COALESCE matters: sum() over an empty FILTER yields NULL, and a segment
    # that simply did not exist last period should read as zero, not unknown.
    # That distinction is the difference between "new segment" and "no data".
    period_expr = f"date_trunc('{grain}', {time_col})"
    sql = (
        f"SELECT {dimension_col} AS segment, "  # noqa: S608
        f"COALESCE({measure_sql} FILTER (WHERE {period_expr} = CAST(? AS TIMESTAMP)), 0) "
        "AS current_value, "
        f"COALESCE({measure_sql} FILTER (WHERE {period_expr} = CAST(? AS TIMESTAMP)), 0) "
        "AS previous_value "
        f"FROM {source} "
        f"WHERE {dimension_col} IS NOT NULL AND {time_col} IS NOT NULL "
        "GROUP BY 1"
    )
    return sql, measure_name


def build_total_sql(
    semantics: DatasetSemantics,
    *,
    schema: str,
    table: str,
    grain: str = "month",
) -> tuple[str, str] | None:
    """SQL for the measure's overall value in each of the two periods.

    Queried separately rather than summed from the segment breakdown: rows with
    a NULL dimension are excluded from that breakdown but still count towards
    the business's actual total. Deriving the headline from the segments would
    quietly under-report it.
    """
    time_column = semantics.time_column
    if time_column is None:
        return None
    if grain not in GRAINS:
        raise ValueError(f"Unsupported time grain: {grain}")

    validate_identifier(schema)
    validate_identifier(table)
    time_col = validate_identifier(time_column.column)

    revenue = semantics.revenue_column
    if revenue is not None:
        measure_sql = f"sum({validate_identifier(revenue.column)})"
        measure_name = revenue.column
    else:
        measure_sql = "count(*)"
        measure_name = "records"

    period_expr = f"date_trunc('{grain}', {time_col})"
    sql = (
        f"SELECT COALESCE({measure_sql} FILTER (WHERE {period_expr} = "  # noqa: S608
        "CAST(? AS TIMESTAMP)), 0) AS current_value, "
        f"COALESCE({measure_sql} FILTER (WHERE {period_expr} = "
        "CAST(? AS TIMESTAMP)), 0) AS previous_value "
        f"FROM {schema}.{table} WHERE {time_col} IS NOT NULL"
    )
    return sql, measure_name


def build_period_sql(
    semantics: DatasetSemantics, *, schema: str, table: str, grain: str = "month"
) -> str | None:
    """SQL listing the periods present in the data, most recent first."""
    time_column = semantics.time_column
    if time_column is None:
        return None
    if grain not in GRAINS:
        raise ValueError(f"Unsupported time grain: {grain}")

    validate_identifier(schema)
    validate_identifier(table)
    time_col = validate_identifier(time_column.column)

    return (
        f"SELECT DISTINCT date_trunc('{grain}', {time_col}) AS period "  # noqa: S608
        f"FROM {schema}.{table} WHERE {time_col} IS NOT NULL "
        "ORDER BY 1 DESC"
    )


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def explain(
    rows: list[dict[str, object]],
    *,
    dimension: str,
    measure: str,
    sql: str,
) -> DimensionExplanation:
    """Turn segment totals into a ranked explanation of the change.

    Pure: takes the query's output, returns the finding. Keeping the arithmetic
    separate from the I/O is what makes "does this correctly identify a
    concentrated drop?" a test that needs no database.
    """
    changes: list[SegmentChange] = []
    for row in rows:
        current = _as_float(row.get("current_value"))
        previous = _as_float(row.get("previous_value"))
        changes.append(
            SegmentChange(
                segment=str(row.get("segment")),
                current=current,
                previous=previous,
                delta=current - previous,
                share=None,
            )
        )

    total_delta = sum(change.delta for change in changes)
    gross_movement = sum(abs(change.delta) for change in changes)

    # Ranked by absolute movement: the largest fall is as much the story as the
    # largest rise, and a signed sort would bury one of them.
    changes.sort(key=lambda change: abs(change.delta), reverse=True)

    concentration = abs(changes[0].delta) / gross_movement if gross_movement else 0.0

    ranked = [
        SegmentChange(
            segment=change.segment,
            current=change.current,
            previous=change.previous,
            delta=change.delta,
            share=(change.delta / total_delta) if total_delta else None,
        )
        for change in changes[:MAX_SEGMENTS]
    ]

    return DimensionExplanation(
        dimension=dimension,
        measure=measure,
        total_delta=total_delta,
        gross_movement=gross_movement,
        concentration=concentration,
        segments=ranked,
        sql=sql,
    )


def best_explanation(
    explanations: list[DimensionExplanation],
) -> DimensionExplanation | None:
    """The dimension that accounts for the change most sharply.

    Ties and near-ties are not worth agonising over — what matters is not
    picking a dimension whose movement is diffuse when a concentrated one
    exists. Callers still check `.concentrated` before phrasing the result as
    a cause.
    """
    candidates = [item for item in explanations if item.gross_movement > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.concentration)
