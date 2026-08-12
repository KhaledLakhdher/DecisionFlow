"""Dimensional modelling endpoints.

Detection proposes; a person decides. Nothing here joins tables automatically,
because a wrong join does not fail — it multiplies rows and reports inflated
totals that look entirely plausible.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from decisionflow.api.deps import (
    TenantPrincipal,
    TenantPrincipalDep,
    TenantSessionDep,
    require_role,
)
from decisionflow.db.models.tenancy import Role
from decisionflow.services import modelling as modelling_service

router = APIRouter(prefix="/model", tags=["modelling"])

AnalystPrincipal = Annotated[TenantPrincipal, Depends(require_role(Role.ANALYST))]


class DecisionRequest(BaseModel):
    confirmed: bool


@router.get("")
async def get_model(
    tenant: TenantPrincipalDep, session: TenantSessionDep
) -> dict[str, Any]:
    """The current dimensional model: tables, roles, and relationships."""
    return await modelling_service.get_model(session, org_id=tenant.org_id)


@router.post("/detect", status_code=status.HTTP_200_OK)
async def detect(actor: AnalystPrincipal, session: TenantSessionDep) -> dict[str, Any]:
    """Scan cleaned datasets for foreign keys.

    Returns proposals with a confidence and a plain-language rationale. None of
    them affect queries until confirmed.
    """
    relationships = await modelling_service.detect_relationships(
        session, org_id=actor.org_id
    )
    return {
        "proposed": len(relationships),
        "relationships": [
            {
                "id": str(rel.id),
                "from_column": rel.from_column,
                "to_column": rel.to_column,
                "confidence": rel.confidence,
                "confirmed": rel.confirmed,
                "rationale": rel.rationale,
            }
            for rel in relationships
        ],
    }


@router.patch("/relationships/{relationship_id}")
async def decide(
    relationship_id: uuid.UUID,
    payload: DecisionRequest,
    actor: AnalystPrincipal,
    session: TenantSessionDep,
) -> dict[str, Any]:
    """Confirm or reject a proposed relationship, rebuilding the model."""
    relationship = await modelling_service.decide(
        session,
        org_id=actor.org_id,
        relationship_id=relationship_id,
        confirmed=payload.confirmed,
    )
    return {
        "id": str(relationship.id),
        "confirmed": relationship.confirmed,
        "model": await modelling_service.get_model(session, org_id=actor.org_id),
    }


@router.post("/rebuild")
async def rebuild(actor: AnalystPrincipal, session: TenantSessionDep) -> dict[str, Any]:
    """Re-classify tables and rebuild star views from confirmed relationships."""
    return await modelling_service.rebuild_model(session, org_id=actor.org_id)
