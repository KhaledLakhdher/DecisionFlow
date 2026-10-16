"""The investigator: why a number moved.

Every dimension is decomposed across the two periods in a single pass, then
ranked by how concentrated its movement is. The dimension whose change is
carried by the fewest segments is the one worth reporting — and when no
dimension is concentrated, that is the finding, not a failure to produce one.

All of it runs on one DuckDB connection. Opening a connection per dimension
would take the workspace's advisory lock a dozen times over for what is a
handful of grouped aggregates.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import duckdb

from decisionflow.core.errors import ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.data import contribution, warehouse
from decisionflow.db.models.ingestion import Dataset, DatasetColumn
from decisionflow.services import analytics as analytics_service

log = get_logger(__name__)


def _period_label(value: Any) -> str:
    """A period rendered for display, without a spurious time component."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _candidate_dimensions(
    columns: list[DatasetColumn], names: list[str]
) -> list[str]:
    """Dimensions narrow enough for a decomposition to mean anything.

    Cardinality comes from the stored profile rather than a COUNT DISTINCT per
    column — it was already computed during ingestion, and re-deriving it here
    would add a full scan per dimension to answer a question profiling already
    answered.
    """
    by_name = {column.normalized_name: column for column in columns}
    keep: list[str] = []
    for name in names:
        column = by_name.get(name)
        distinct = (column.profile or {}).get("distinct_count") if column else None
        if isinstance(distinct, int) and distinct > contribution.MAX_CARDINALITY:
            continue
        keep.append(name)
    return keep


async def diagnose(
    *,
    dataset: Dataset,
    columns: list[DatasetColumn],
    grain: str = "month",
    period: str | None = None,
) -> dict[str, Any]:
    """Explain the change in the headline measure between two periods.

    `period` targets a specific period — an anomalous one, usually — and is
    compared against whichever period immediately precedes it in the data.
    Without it the two most recent periods are used.
    """
    semantics = analytics_service.load_semantics(columns)
    if semantics.time_column is None:
        raise ValidationError(
            "This dataset has no date column, so there is no change to explain."
        )

    dimension_names = _candidate_dimensions(
        columns, [column.column for column in semantics.dimensions]
    )
    if not dimension_names:
        raise ValidationError(
            "This dataset has no groupable dimension narrow enough to break a "
            "change down by."
        )

    period_sql = contribution.build_period_sql(
        semantics, schema=warehouse.CLEAN_SCHEMA, table=dataset.slug, grain=grain
    )
    total_built = contribution.build_total_sql(
        semantics, schema=warehouse.CLEAN_SCHEMA, table=dataset.slug, grain=grain
    )
    # Both are None only when there is no time column, which was rejected above.
    assert period_sql is not None and total_built is not None
    total_sql, measure = total_built

    built: list[tuple[str, str]] = []
    for name in dimension_names:
        result = contribution.build_contribution_sql(
            semantics,
            schema=warehouse.CLEAN_SCHEMA,
            table=dataset.slug,
            dimension=name,
            grain=grain,
        )
        if result is not None:
            built.append((name, result[0]))

    def _run(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        periods = [row[0] for row in connection.execute(period_sql).fetchall()]
        if len(periods) < 2:
            return {"periods": periods}

        if period is None:
            current, previous = periods[0], periods[1]
        else:
            labels = [_period_label(item) for item in periods]
            if period not in labels:
                return {"periods": periods, "unknown_period": True}
            index = labels.index(period)
            if index + 1 >= len(periods):
                return {"periods": periods, "no_preceding": True}
            current, previous = periods[index], periods[index + 1]

        params = [current, previous]
        totals = connection.execute(total_sql, params).fetchone()

        explanations = []
        for name, sql in built:
            cursor = connection.execute(sql, params)
            names = [description[0] for description in cursor.description or []]
            rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
            explanations.append(
                contribution.explain(rows, dimension=name, measure=measure, sql=sql)
            )

        return {
            "current": current,
            "previous": previous,
            "totals": totals,
            "explanations": explanations,
        }

    async with warehouse.warehouse(dataset.org_id) as connection:
        outcome = await asyncio.to_thread(_run, connection)

    if "explanations" not in outcome:
        if outcome.get("unknown_period"):
            raise ValidationError(f"There is no {grain} period '{period}' in this data.")
        if outcome.get("no_preceding"):
            raise ValidationError(
                f"'{period}' is the earliest period in the data, so there is "
                "nothing to compare it against."
            )
        raise ValidationError(
            f"Explaining a change needs at least two {grain} periods of data; "
            f"this dataset has {len(outcome.get('periods', []))}."
        )

    explanations: list[contribution.DimensionExplanation] = outcome["explanations"]
    best = contribution.best_explanation(explanations)

    current_total = float(outcome["totals"][0]) if outcome["totals"] else 0.0
    previous_total = float(outcome["totals"][1]) if outcome["totals"] else 0.0
    change = current_total - previous_total

    log.info(
        "investigation.diagnosed",
        dataset_id=str(dataset.id),
        dimensions=len(explanations),
        concentrated=bool(best and best.concentrated),
    )

    return {
        "measure": measure,
        "grain": grain,
        "current_period": _period_label(outcome["current"]),
        "previous_period": _period_label(outcome["previous"]),
        "current_value": current_total,
        "previous_value": previous_total,
        "change": change,
        "change_ratio": (change / previous_total) if previous_total else None,
        "direction": "up" if change > 0 else "down" if change < 0 else "flat",
        # The verdict the whole module exists to produce. False here is a real
        # answer — the movement is broad-based and has no single cause.
        "concentrated": bool(best and best.concentrated),
        # Segments moving against each other. When true, share-of-net figures
        # are misleading and the absolute movements are the honest summary.
        "offsetting": bool(best and best.offsetting),
        "best_dimension": best.dimension if best else None,
        "dimensions": [
            {
                "dimension": item.dimension,
                "concentration": item.concentration,
                "total_delta": item.total_delta,
                "gross_movement": item.gross_movement,
                "offsetting": item.offsetting,
                "riser": item.riser.segment if item.riser else None,
                "faller": item.faller.segment if item.faller else None,
                "sql": item.sql,
                "segments": [
                    {
                        "segment": segment.segment,
                        "current": segment.current,
                        "previous": segment.previous,
                        "delta": segment.delta,
                        "share": segment.share,
                    }
                    for segment in item.segments
                ],
            }
            # Most explanatory first, so a caller taking the head of the list
            # gets the strongest account rather than an arbitrary one.
            for item in sorted(
                explanations, key=lambda item: item.concentration, reverse=True
            )
        ],
    }
