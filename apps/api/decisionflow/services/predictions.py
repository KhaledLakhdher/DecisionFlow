"""Prediction services: forecasting, anomalies, churn.

Nothing here is persisted. Predictions are cheap to recompute and go stale the
moment the data changes, so a cached forecast is a wrong forecast waiting to be
shown. Cache when there is evidence of a cost problem, not before.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from decisionflow.core.errors import ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.data import warehouse
from decisionflow.data.semantics import SemanticRole
from decisionflow.data.warehouse import validate_identifier
from decisionflow.db.models.ingestion import Dataset, DatasetColumn
from decisionflow.ml import anomalies as anomaly_engine
from decisionflow.ml import churn as churn_engine
from decisionflow.ml import forecasting
from decisionflow.services import analytics as analytics_service

log = get_logger(__name__)


async def _revenue_series(
    dataset: Dataset, columns: list[DatasetColumn], grain: str
) -> tuple[list[Any], list[float | None], str]:
    """The time series every temporal model here is built on."""
    result = await analytics_service.timeseries(
        dataset=dataset, columns=columns, grain=grain
    )
    periods = [point["period"] for point in result["points"]]
    values = [point["value"] for point in result["points"]]
    return periods, values, result["measure"]


async def forecast(
    session: AsyncSession,
    *,
    dataset: Dataset,
    columns: list[DatasetColumn],
    horizon: int = 3,
    grain: str = "month",
) -> dict[str, Any]:
    """Project the headline measure forward."""
    periods, values, measure = await _revenue_series(dataset, columns, grain)

    try:
        result = await asyncio.to_thread(
            forecasting.forecast_series, periods, values, horizon=horizon
        )
    except forecasting.InsufficientDataError as exc:
        # A refusal is a legitimate outcome, not a server fault.
        raise ValidationError(str(exc)) from exc

    log.info(
        "predictions.forecast",
        dataset_id=str(dataset.id),
        method=result.method.value,
        history=result.history_points,
    )

    return {
        "measure": measure,
        "grain": grain,
        "method": result.method.value,
        "confidence": result.confidence,
        "rationale": result.rationale,
        "history_points": result.history_points,
        "noise_ratio": result.noise_ratio,
        "history": [
            {"period": str(period), "value": value}
            for period, value in zip(periods, values, strict=True)
        ],
        "points": [
            {
                "period": point.period,
                "value": point.value,
                "lower": point.lower,
                "upper": point.upper,
            }
            for point in result.points
        ],
    }


async def detect_anomalies(
    session: AsyncSession,
    *,
    dataset: Dataset,
    columns: list[DatasetColumn],
    grain: str = "month",
) -> dict[str, Any]:
    """Periods that depart materially from the trend."""
    periods, values, measure = await _revenue_series(dataset, columns, grain)
    found = await asyncio.to_thread(anomaly_engine.detect, periods, values)

    return {
        "measure": measure,
        "grain": grain,
        "periods_examined": len([value for value in values if value is not None]),
        "anomalies": [
            {
                "period": item.period,
                "value": item.value,
                "expected": item.expected,
                "deviation": item.deviation,
                "deviation_ratio": item.deviation_ratio,
                "direction": item.direction,
                "severity": item.severity,
                "message": item.message,
            }
            for item in found
        ],
    }


async def churn(
    session: AsyncSession, *, dataset: Dataset, columns: list[DatasetColumn]
) -> dict[str, Any]:
    """Score customers on their likelihood of having lapsed."""
    semantics = analytics_service.load_semantics(columns)

    customer = semantics.customer_key
    time_column = semantics.time_column
    if customer is None:
        raise ValidationError(
            "Churn analysis needs a customer identifier, and none was detected "
            "in this dataset."
        )
    if time_column is None:
        raise ValidationError(
            "Churn analysis needs a date column, and none was detected in this "
            "dataset."
        )

    revenue = semantics.revenue_column
    wanted = [customer.column, time_column.column]
    if revenue is not None:
        wanted.append(revenue.column)

    projection = ", ".join(validate_identifier(name) for name in wanted)
    table = f"{warehouse.CLEAN_SCHEMA}.{validate_identifier(dataset.slug)}"
    rows = await warehouse.fetch_all(
        dataset.org_id,
        f"SELECT {projection} FROM {table} "  # noqa: S608
        f"WHERE {validate_identifier(customer.column)} IS NOT NULL",
    )

    try:
        model = await asyncio.to_thread(
            churn_engine.fit,
            rows,
            customer_key=customer.column,
            date_key=time_column.column,
            value_key=revenue.column if revenue else None,
        )
    except churn_engine.InsufficientDataError as exc:
        raise ValidationError(str(exc)) from exc

    log.info(
        "predictions.churn",
        dataset_id=str(dataset.id),
        customers=model.customers,
        lapsed=model.lapsed,
    )

    return {
        "definition": model.definition,
        "cutoff_days": model.cutoff_days,
        "customers": model.customers,
        "lapsed": model.lapsed,
        "lapsed_rate": model.lapsed / model.customers if model.customers else 0.0,
        "accuracy": model.accuracy,
        "drivers": [
            {
                "feature": name,
                "label": churn_engine.FEATURE_LABELS.get(name, name),
                "coefficient": coefficient,
                "direction": "increases risk" if coefficient > 0 else "reduces risk",
            }
            for name, coefficient in sorted(
                model.drivers.items(), key=lambda item: abs(item[1]), reverse=True
            )
        ],
        "at_risk": [
            {
                "customer": item.customer,
                "risk": item.risk,
                "band": item.band,
                "recency_days": item.recency_days,
                "frequency": item.frequency,
                "monetary": item.monetary,
                "reason": item.reason,
            }
            for item in model.at_risk
        ],
    }


def available_predictions(columns: list[DatasetColumn]) -> dict[str, bool]:
    """Which prediction features this dataset's shape can support.

    Lets the UI hide what cannot work rather than offering a button that
    returns an error.
    """
    semantics = analytics_service.load_semantics(columns)
    return {
        "forecast": semantics.time_column is not None,
        "anomalies": semantics.time_column is not None,
        "churn": semantics.customer_key is not None and semantics.time_column is not None,
        "dimensions": len(semantics.by_role(SemanticRole.DIMENSION)) > 0,
    }
