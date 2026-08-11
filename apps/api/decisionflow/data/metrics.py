"""Automatic KPI generation.

Given a classified dataset, decide which business metrics its shape can
actually support, and emit the SQL for each. A dataset with revenue and a date
gets growth; one with a customer key gets retention; one with neither gets
neither, rather than a dashboard of zeroes.

Every KPI carries the SQL that produced it. That is not decoration:

  * it is how a user checks a number they distrust;
  * it is what the LLM reads to explain the figure without re-deriving it;
  * it is the difference between an analytics tool and an oracle.

Nothing here executes anything. `build_kpi_specs` is a pure function of the
semantic model, which keeps the "which metrics apply?" logic testable without
a database.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from decisionflow.data.semantics import DatasetSemantics
from decisionflow.data.warehouse import validate_identifier


class MetricFormat(enum.StrEnum):
    """How to render a value. The UI should never guess from the number alone."""

    CURRENCY = "currency"
    NUMBER = "number"
    PERCENT = "percent"
    DECIMAL = "decimal"


@dataclass(slots=True)
class KpiSpec:
    """One metric: what it is called, how to compute it, how to show it."""

    key: str
    label: str
    description: str
    sql: str
    format: MetricFormat
    # Higher is better for revenue; not for cost or churn. The narrative layer
    # needs this to know whether a change is good news.
    higher_is_better: bool = True
    depends_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KpiValue:
    spec: KpiSpec
    value: float | None
    details: dict[str, Any] = field(default_factory=dict)


def build_kpi_specs(semantics: DatasetSemantics, *, schema: str, table: str) -> list[KpiSpec]:
    """Every KPI this dataset's shape supports.

    Deliberately conservative: a metric is only emitted when the columns it
    needs exist. Inventing a "conversion rate" from data that cannot express
    one produces a confident, wrong number — worse than an absent tile.
    """
    validate_identifier(schema)
    validate_identifier(table)
    source = f"{schema}.{table}"

    specs: list[KpiSpec] = [
        KpiSpec(
            key="row_count",
            label="Records",
            description="Total rows in the cleaned dataset.",
            sql=f"SELECT count(*) FROM {source}",  # noqa: S608
            format=MetricFormat.NUMBER,
        )
    ]

    revenue = semantics.revenue_column
    quantity = semantics.quantity_column
    cost = semantics.cost_column
    customer = semantics.customer_key
    order = semantics.order_key

    if revenue is not None:
        column = validate_identifier(revenue.column)
        specs.append(
            KpiSpec(
                key="total_revenue",
                label="Total revenue",
                description=f"Sum of {revenue.column}.",
                sql=f"SELECT sum({column}) FROM {source}",  # noqa: S608
                format=MetricFormat.CURRENCY,
                depends_on=[revenue.column],
            )
        )
        specs.append(
            KpiSpec(
                key="average_transaction",
                label="Average transaction",
                description=f"Mean of {revenue.column} across all records.",
                sql=f"SELECT avg({column}) FROM {source}",  # noqa: S608
                format=MetricFormat.CURRENCY,
                depends_on=[revenue.column],
            )
        )

    if quantity is not None:
        column = validate_identifier(quantity.column)
        specs.append(
            KpiSpec(
                key="total_units",
                label="Total units",
                description=f"Sum of {quantity.column}.",
                sql=f"SELECT sum({column}) FROM {source}",  # noqa: S608
                format=MetricFormat.NUMBER,
                depends_on=[quantity.column],
            )
        )

    if customer is not None:
        column = validate_identifier(customer.column)
        specs.append(
            KpiSpec(
                key="unique_customers",
                label="Unique customers",
                description=f"Distinct values of {customer.column}.",
                sql=f"SELECT count(DISTINCT {column}) FROM {source}",  # noqa: S608
                format=MetricFormat.NUMBER,
                depends_on=[customer.column],
            )
        )

    if order is not None:
        column = validate_identifier(order.column)
        specs.append(
            KpiSpec(
                key="order_count",
                label="Orders",
                description=f"Distinct values of {order.column}.",
                sql=f"SELECT count(DISTINCT {column}) FROM {source}",  # noqa: S608
                format=MetricFormat.NUMBER,
                depends_on=[order.column],
            )
        )

    # --- ratios, only where both halves exist ---------------------------
    if revenue is not None and order is not None:
        revenue_col = validate_identifier(revenue.column)
        order_col = validate_identifier(order.column)
        specs.append(
            KpiSpec(
                key="average_order_value",
                label="Average order value",
                description="Total revenue divided by the number of distinct orders.",
                # NULLIF guards the empty-dataset case: division by zero would
                # abort the whole KPI batch rather than yield an empty tile.
                sql=(
                    f"SELECT sum({revenue_col}) / NULLIF(count(DISTINCT {order_col}), 0) "  # noqa: S608
                    f"FROM {source}"
                ),
                format=MetricFormat.CURRENCY,
                depends_on=[revenue.column, order.column],
            )
        )

    if revenue is not None and customer is not None:
        revenue_col = validate_identifier(revenue.column)
        customer_col = validate_identifier(customer.column)
        specs.append(
            KpiSpec(
                key="revenue_per_customer",
                label="Revenue per customer",
                description="Total revenue divided by the number of distinct customers.",
                sql=(
                    f"SELECT sum({revenue_col}) / NULLIF(count(DISTINCT {customer_col}), 0) "  # noqa: S608
                    f"FROM {source}"
                ),
                format=MetricFormat.CURRENCY,
                depends_on=[revenue.column, customer.column],
            )
        )
        specs.append(
            KpiSpec(
                key="repeat_customer_rate",
                label="Repeat customer rate",
                description="Share of customers appearing in more than one record.",
                sql=(
                    "SELECT count(*) FILTER (WHERE appearances > 1) * 1.0 "  # noqa: S608
                    "/ NULLIF(count(*), 0) FROM ("
                    f"SELECT {customer_col}, count(*) AS appearances "
                    f"FROM {source} WHERE {customer_col} IS NOT NULL "
                    f"GROUP BY {customer_col})"
                ),
                format=MetricFormat.PERCENT,
                depends_on=[customer.column],
            )
        )

    if revenue is not None and cost is not None:
        revenue_col = validate_identifier(revenue.column)
        cost_col = validate_identifier(cost.column)
        specs.append(
            KpiSpec(
                key="gross_margin",
                label="Gross margin",
                description="Revenue minus cost, as a share of revenue.",
                sql=(
                    f"SELECT (sum({revenue_col}) - sum({cost_col})) "  # noqa: S608
                    f"/ NULLIF(sum({revenue_col}), 0) FROM {source}"
                ),
                format=MetricFormat.PERCENT,
                depends_on=[revenue.column, cost.column],
            )
        )

    return specs


def build_timeseries_sql(
    semantics: DatasetSemantics,
    *,
    schema: str,
    table: str,
    grain: str = "month",
) -> tuple[str, str] | None:
    """SQL for revenue (or record count) over time, plus the measure's name.

    Returns None when the dataset has no time column — a time series without a
    time axis is not something to fake.
    """
    time_column = semantics.time_column
    if time_column is None:
        return None

    validate_identifier(schema)
    validate_identifier(table)
    time_col = validate_identifier(time_column.column)
    source = f"{schema}.{table}"

    if grain not in {"day", "week", "month", "quarter", "year"}:
        raise ValueError(f"Unsupported time grain: {grain}")

    revenue = semantics.revenue_column
    if revenue is not None:
        measure_col = validate_identifier(revenue.column)
        measure_sql = f"sum({measure_col})"
        measure_name = revenue.column
    else:
        measure_sql = "count(*)"
        measure_name = "records"

    sql = (
        f"SELECT date_trunc('{grain}', {time_col}) AS period, "  # noqa: S608
        f"{measure_sql} AS value FROM {source} "
        f"WHERE {time_col} IS NOT NULL GROUP BY 1 ORDER BY 1"
    )
    return sql, measure_name


def build_breakdown_sql(
    semantics: DatasetSemantics,
    *,
    schema: str,
    table: str,
    dimension: str,
    limit: int = 10,
) -> tuple[str, str]:
    """SQL for the top values of a dimension by revenue (or record count)."""
    validate_identifier(schema)
    validate_identifier(table)
    dimension_col = validate_identifier(dimension)
    source = f"{schema}.{table}"
    limit = max(1, min(limit, 100))

    revenue = semantics.revenue_column
    if revenue is not None:
        measure_col = validate_identifier(revenue.column)
        measure_sql = f"sum({measure_col})"
        measure_name = revenue.column
    else:
        measure_sql = "count(*)"
        measure_name = "records"

    sql = (
        f"SELECT {dimension_col} AS label, {measure_sql} AS value FROM {source} "  # noqa: S608
        f"WHERE {dimension_col} IS NOT NULL GROUP BY 1 "
        f"ORDER BY 2 DESC NULLS LAST LIMIT {limit}"
    )
    return sql, measure_name


def build_growth_sql(
    semantics: DatasetSemantics, *, schema: str, table: str, grain: str = "month"
) -> str | None:
    """SQL comparing the most recent complete period against the one before it.

    This is the single most useful number in a business dashboard and the one
    traditional BI makes you configure by hand.
    """
    time_column = semantics.time_column
    revenue = semantics.revenue_column
    if time_column is None:
        return None

    validate_identifier(schema)
    validate_identifier(table)
    time_col = validate_identifier(time_column.column)
    source = f"{schema}.{table}"

    measure_sql = (
        f"sum({validate_identifier(revenue.column)})" if revenue is not None else "count(*)"
    )

    # Window function over aggregated periods: aggregate first, then look back
    # one row. Correct even when a period is missing entirely, which a
    # date-arithmetic join would silently get wrong.
    return (
        "WITH periods AS ("  # noqa: S608
        f"SELECT date_trunc('{grain}', {time_col}) AS period, "
        f"{measure_sql} AS value FROM {source} "
        f"WHERE {time_col} IS NOT NULL GROUP BY 1"
        "), compared AS ("
        "SELECT period, value, lag(value) OVER (ORDER BY period) AS previous "
        "FROM periods"
        ") SELECT period, value, previous, "
        "(value - previous) / NULLIF(previous, 0) AS change "
        "FROM compared ORDER BY period DESC LIMIT 1"
    )
