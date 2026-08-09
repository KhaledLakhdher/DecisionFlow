"""Workspace and membership management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from decisionflow.api.deps import CurrentUserDep, Principal, PrincipalDep, SessionDep, require_role
from decisionflow.core.errors import PermissionDeniedError, ValidationError
from decisionflow.db.models.tenancy import Role
from decisionflow.schemas.auth import (
    InvitationCreatedOut,
    InvitationOut,
    InviteRequest,
    MemberOut,
    OrganizationOut,
)
from decisionflow.services import auth as auth_service

router = APIRouter(prefix="/orgs", tags=["workspaces"])

AdminPrincipal = Annotated[Principal, Depends(require_role(Role.ADMIN))]


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ChangeRoleRequest(BaseModel):
    role: Role


@router.get("", response_model=list[OrganizationOut])
async def list_my_orgs(user: CurrentUserDep, session: SessionDep) -> list[OrganizationOut]:
    memberships = await auth_service.list_memberships(session, user.id)
    return [OrganizationOut.model_validate(m.organization) for m in memberships]


@router.post("", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_org(
    payload: CreateOrgRequest, user: CurrentUserDep, session: SessionDep
) -> OrganizationOut:
    organization, _membership = await auth_service.create_organization(
        session, user=user, name=payload.name
    )
    return OrganizationOut.model_validate(organization)


@router.get("/current/members", response_model=list[MemberOut])
async def list_members(principal: PrincipalDep, session: SessionDep) -> list[MemberOut]:
    org_id = principal.require_org()
    memberships = await auth_service.list_members(session, org_id=org_id)
    return [
        MemberOut(
            user_id=m.user_id,
            email=m.user.email,
            full_name=m.user.full_name,
            role=m.role,
            joined_at=m.created_at,
        )
        for m in memberships
    ]


@router.post(
    "/current/invitations",
    response_model=InvitationCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    payload: InviteRequest,
    principal: AdminPrincipal,
    user: CurrentUserDep,
    session: SessionDep,
) -> InvitationCreatedOut:
    """Invite someone to the active workspace. Admin or owner only."""
    invitation, raw_token = await auth_service.invite_member(
        session,
        org_id=principal.require_org(),
        email=payload.email,
        role=payload.role,
        invited_by=user,
    )
    return InvitationCreatedOut(
        invitation=InvitationOut.model_validate(invitation),
        invite_token=raw_token,
    )


@router.patch("/current/members/{user_id}/role", response_model=MemberOut)
async def change_member_role(
    user_id: uuid.UUID,
    payload: ChangeRoleRequest,
    principal: AdminPrincipal,
    session: SessionDep,
) -> MemberOut:
    """Change a member's role. Only an owner may grant or revoke ownership."""
    org_id = principal.require_org()

    if user_id == principal.user_id:
        # Otherwise an admin could quietly promote themselves to owner.
        raise ValidationError("You cannot change your own role.")

    if payload.role is Role.OWNER and principal.role is not Role.OWNER:
        raise PermissionDeniedError("Only an owner can grant ownership.")

    membership = await auth_service.change_member_role(
        session, org_id=org_id, target_user_id=user_id, new_role=payload.role
    )
    return MemberOut(
        user_id=membership.user_id,
        email=membership.user.email,
        full_name=membership.user.full_name,
        role=membership.role,
        joined_at=membership.created_at,
    )
