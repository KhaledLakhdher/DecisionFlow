"""The agent fleet: briefings and routed questions."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from decisionflow.api.deps import (
    TenantPrincipal,
    TenantPrincipalDep,
    TenantSessionDep,
    require_role,
)
from decisionflow.core import ratelimit
from decisionflow.db.models.tenancy import Role
from decisionflow.schemas.fleet import (
    BriefingOut,
    BriefingRequest,
    BriefingSummaryOut,
    FleetCapabilityOut,
)
from decisionflow.services import fleet as fleet_service
from decisionflow.services import ingestion as ingestion_service

router = APIRouter(prefix="/datasets/{dataset_id}", tags=["fleet"])

# Reading the data, so viewers may ask. It spends real money, hence the limit.
AnalystPrincipal = Annotated[TenantPrincipal, Depends(require_role(Role.VIEWER))]

# Far tighter than a single question: one briefing is a planning call, up to
# four specialists, and a synthesis call. Ten of those in fifteen minutes is
# already a generous allowance for a workspace.
BRIEFING_LIMIT = ratelimit.RateLimit(limit=10, window_seconds=900)


@router.post("/briefing", response_model=BriefingOut)
async def create_briefing(
    dataset_id: uuid.UUID,
    payload: BriefingRequest,
    actor: AnalystPrincipal,
    session: TenantSessionDep,
) -> BriefingOut:
    """Run the fleet over a dataset and return the brief it produced."""
    await ratelimit.enforce("briefing:org", str(actor.org_id), BRIEFING_LIMIT)

    run = await fleet_service.run_briefing(
        session,
        org_id=actor.org_id,
        user_id=actor.user_id,
        dataset_id=dataset_id,
        question=payload.question,
    )
    return BriefingOut.model_validate(run)


@router.get("/briefings", response_model=list[BriefingSummaryOut])
async def list_briefings(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> list[BriefingSummaryOut]:
    await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    runs = await fleet_service.list_briefings(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return [BriefingSummaryOut.model_validate(run) for run in runs]


@router.get("/briefings/{run_id}", response_model=BriefingOut)
async def get_briefing(
    dataset_id: uuid.UUID,
    run_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
) -> BriefingOut:
    run = await fleet_service.get_briefing(
        session, org_id=tenant.org_id, run_id=run_id
    )
    return BriefingOut.model_validate(run)


@router.get("/fleet", response_model=FleetCapabilityOut)
async def fleet_capabilities(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> FleetCapabilityOut:
    """Which specialists this dataset can support, before spending anything."""
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    columns = list(dataset.columns)
    return FleetCapabilityOut(
        specialists=[
            item.value
            for item in fleet_service.available_specialists(columns, has_question=False)
        ],
        with_question=[
            item.value
            for item in fleet_service.available_specialists(columns, has_question=True)
        ],
    )
