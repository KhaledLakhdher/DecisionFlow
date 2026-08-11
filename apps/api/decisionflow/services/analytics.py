"""Semantic classification and KPI computation.

Runs after cleaning, against the `clean` layer — classification depends on the
*post-coercion* types, so a revenue column that arrived as "$1,249.99" text is
a measure by the time this sees it. Classifying the raw layer would file it as
a string dimension and produce no revenue KPIs at all.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import duckdb
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from decisionflow.core.errors import NotFoundError, ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.data import warehouse
from decisionflow.data.metrics import (
    KpiSpec,
    build_breakdown_sql,
    build_growth_sql,
    build_kpi_specs,
    build_timeseries_sql,
)
from decisionflow.data.profiling import ColumnProfile
from decisionflow.data.semantics import (
    ColumnSemantics,
    DatasetSemantics,
    SemanticRole,
    SemanticTag,
    classify_dataset,
)
from decisionflow.db.models.ingestion import Dataset, DatasetColumn, Kpi

log = get_logger(__name__)


def _profile_from_json(raw: dict[str, Any]) -> ColumnProfile:
    """Rebuild a profile from its stored JSON.

    Unknown keys are dropped rather than raising: a profile written by an older
    revision must not break classification after the dataclass gains a field.
    """
    known = set(ColumnProfile.__dataclass_fields__)
    return ColumnProfile(**{key: value for key, value in raw.items() if key in known})


def load_semantics(columns: list[DatasetColumn]) -> DatasetSemantics:
    """Reconstruct the semantic model from stored column rows.

    Reads the persisted role when there is one, so a human correction survives,
    and only falls back to classifying when a column has never been classified.
    """
    stored = [column.semantic_role for column in columns]
    if columns and all(role is not None for role in stored):
        return DatasetSemantics(
            columns=[
                ColumnSemantics(
                    column=column.normalized_name,
                    # Narrowed by the `all(...)` guard above; mypy cannot see it.
                    role=SemanticRole(column.semantic_role or SemanticRole.IGNORED.value),
                    tags=[SemanticTag(tag) for tag in column.semantic_tags],
                    rationale=column.semantic_rationale or "",
                )
                for column in columns
            ]
        )

    return classify_dataset(
        [
            (
                column.normalized_name,
                column.effective_type,
                _profile_from_json(column.profile),
            )
            for column in columns
        ]
    )


def _to_decimal(value: Any) -> Decimal | None:
    """Coerce a DuckDB aggregate into a Decimal, or None.

    NaN and infinity are rejected outright: Postgres NUMERIC cannot store them,
    and a KPI that fails to persist would take the whole batch down.
    """
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _execute_specs(
    connection: duckdb.DuckDBPyConnection, specs: list[KpiSpec]
) -> dict[str, Decimal | None]:
    """Run every KPI query.

    One failing metric must not lose the rest — a malformed column can make a
    single aggregate throw, and a dashboard missing one tile beats a dashboard
    missing all of them.
    """
    values: dict[str, Decimal | None] = {}
    for spec in specs:
        try:
            row = connection.execute(spec.sql).fetchone()
            values[spec.key] = _to_decimal(row[0] if row else None)
        except Exception as exc:
            log.warning("analytics.kpi_failed", key=spec.key, error=str(exc))
            values[spec.key] = None
    return values


def _execute_growth(
    connection: duckdb.DuckDBPyConnection, sql: str | None
) -> dict[str, Any]:
    if sql is None:
        return {}
    try:
        row = connection.execute(sql).fetchone()
    except Exception as exc:
        log.warning("analytics.growth_failed", error=str(exc))
        return {}

    if row is None or row[2] is None:
        # Only one period exists, so there is nothing to compare against.
        return {}

    return {
        "period": str(row[0]),
        "value": float(row[1]) if row[1] is not None else None,
        "previous": float(row[2]),
        "change": float(row[3]) if row[3] is not None else None,
    }


async def analyse_dataset(session: AsyncSession, *, dataset_id: uuid.UUID) -> Dataset:
    """Classify columns and compute every applicable KPI."""
    dataset = await session.scalar(
        select(Dataset).where(Dataset.id == dataset_id).options(selectinload(Dataset.columns))
    )
    if dataset is None:
        raise NotFoundError("Dataset not found.")
    if dataset.cleaned_at is None:
        raise ValidationError("This dataset must be cleaned before it can be analysed.")

    # Always reclassify here: this runs right after cleaning, when the types
    # have just changed underneath any previous classification.
    semantics = classify_dataset(
        [
            (column.normalized_name, column.effective_type, _profile_from_json(column.profile))
            for column in dataset.columns
        ]
    )
    by_column = {column.column: column for column in semantics.columns}

    specs = build_kpi_specs(semantics, schema=warehouse.CLEAN_SCHEMA, table=dataset.slug)
    growth_sql = build_growth_sql(
        semantics, schema=warehouse.CLEAN_SCHEMA, table=dataset.slug
    )

    def _run(connection: duckdb.DuckDBPyConnection) -> tuple[dict[str, Any], dict[str, Any]]:
        return _execute_specs(connection, specs), _execute_growth(connection, growth_sql)

    async with warehouse.warehouse(dataset.org_id) as connection:
        values, growth = await asyncio.to_thread(_run, connection)

    for column in dataset.columns:
        classified = by_column.get(column.normalized_name)
        if classified is None:
            continue
        column.semantic_role = classified.role.value
        column.semantic_tags = classified.tag_values
        column.semantic_rationale = classified.rationale

    # Replaced wholesale rather than upserted: the applicable set of metrics
    # changes with the data, and a KPI left over from a previous shape would
    # display a stale number with no indication it is no longer computed.
    await session.execute(delete(Kpi).where(Kpi.dataset_id == dataset.id))
    session.add_all(
        [
            Kpi(
                org_id=dataset.org_id,
                dataset_id=dataset.id,
                key=spec.key,
                label=spec.label,
                description=spec.description,
                value=values.get(spec.key),
                format=spec.format.value,
                higher_is_better=spec.higher_is_better,
                sql=spec.sql,
                details={"depends_on": spec.depends_on}
                | ({"growth": growth} if spec.key == "total_revenue" and growth else {}),
            )
            for spec in specs
        ]
    )
    await session.commit()

    log.info(
        "analytics.analysed",
        dataset_id=str(dataset.id),
        kpis=len(specs),
        measures=len(semantics.measures),
        dimensions=len(semantics.dimensions),
        has_time=semantics.time_column is not None,
    )
    return dataset


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
async def list_kpis(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> list[Kpi]:
    result = await session.scalars(
        select(Kpi).where(Kpi.org_id == org_id, Kpi.dataset_id == dataset_id)
    )
    kpis = list(result)

    # Ordered by editorial importance, not alphabetically. Sorting by key put
    # "Total revenue" seventh, behind "Average transaction" — the headline
    # figure should lead the dashboard.
    priority = {
        "total_revenue": 0,
        "gross_margin": 1,
        "order_count": 2,
        "average_order_value": 3,
        "unique_customers": 4,
        "repeat_customer_rate": 5,
        "revenue_per_customer": 6,
        "total_units": 7,
        "average_transaction": 8,
        "row_count": 9,
    }
    return sorted(kpis, key=lambda kpi: (priority.get(kpi.key, 50), kpi.key))


async def timeseries(
    *, dataset: Dataset, columns: list[DatasetColumn], grain: str
) -> dict[str, Any]:
    """Revenue (or record count) over time."""
    semantics = load_semantics(columns)
    built = build_timeseries_sql(
        semantics, schema=warehouse.CLEAN_SCHEMA, table=dataset.slug, grain=grain
    )
    if built is None:
        raise ValidationError("This dataset has no date column, so it has no time series.")

    sql, measure = built
    rows = await warehouse.fetch_all(dataset.org_id, sql)
    return {
        "grain": grain,
        "measure": measure,
        "time_column": semantics.time_column.column if semantics.time_column else None,
        "points": [
            {"period": str(row["period"]), "value": _as_float(row["value"])} for row in rows
        ],
        "sql": sql,
    }


async def breakdown(
    *,
    dataset: Dataset,
    columns: list[DatasetColumn],
    dimension: str | None,
    limit: int,
) -> dict[str, Any]:
    """Top values of a dimension by revenue (or record count)."""
    semantics = load_semantics(columns)
    available = [column.column for column in semantics.dimensions]

    if dimension is None:
        if not available:
            raise ValidationError("This dataset has no groupable dimension.")
        dimension = available[0]
    elif dimension not in available:
        options = ", ".join(available) or "none"
        raise ValidationError(
            f"'{dimension}' is not a groupable dimension. Available: {options}."
        )

    sql, measure = build_breakdown_sql(
        semantics,
        schema=warehouse.CLEAN_SCHEMA,
        table=dataset.slug,
        dimension=dimension,
        limit=limit,
    )
    rows = await warehouse.fetch_all(dataset.org_id, sql)
    return {
        "dimension": dimension,
        "measure": measure,
        "available_dimensions": available,
        "items": [
            {"label": str(row["label"]), "value": _as_float(row["value"])} for row in rows
        ],
        "sql": sql,
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result
