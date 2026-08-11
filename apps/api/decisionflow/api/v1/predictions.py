"""Forecasting, anomaly detection and churn endpoints.

All GETs: these are pure functions of the stored data. Nothing is persisted,
because a prediction goes stale the instant the dataset changes and a cached
one is a wrong one waiting to be shown.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from decisionflow.api.deps import TenantPrincipalDep, TenantSessionDep
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import predictions as prediction_service

router = APIRouter(prefix="/datasets", tags=["predictions"])

Grain = Literal["day", "week", "month", "quarter", "year"]


@router.get("/{dataset_id}/capabilities")
async def capabilities(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> dict[str, bool]:
    """Which predictions this dataset's shape can support.

    Lets the UI hide what cannot work, rather than offering a button that
    returns an error.
    """
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return prediction_service.available_predictions(list(dataset.columns))


@router.get("/{dataset_id}/forecast")
async def forecast(
    dataset_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
    horizon: Annotated[int, Query(ge=1, le=24)] = 3,
    grain: Annotated[Grain, Query()] = "month",
) -> dict[str, Any]:
    """Project the headline measure forward, with a prediction interval.

    Returns 422 when the history is too short — a refusal, not a failure. A
    forecast from three points is a straight line through noise.
    """
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return await prediction_service.forecast(
        session,
        dataset=dataset,
        columns=list(dataset.columns),
        horizon=horizon,
        grain=grain,
    )


@router.get("/{dataset_id}/anomalies")
async def anomalies(
    dataset_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
    grain: Annotated[Grain, Query()] = "month",
) -> dict[str, Any]:
    """Periods that departed materially from the trend."""
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return await prediction_service.detect_anomalies(
        session, dataset=dataset, columns=list(dataset.columns), grain=grain
    )


@router.get("/{dataset_id}/churn")
async def churn(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> dict[str, Any]:
    """Customers most likely to have lapsed, and why."""
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return await prediction_service.churn(
        session, dataset=dataset, columns=list(dataset.columns)
    )
