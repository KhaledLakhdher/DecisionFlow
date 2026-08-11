"""Workspace and membership management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from decisionflow.api.deps import (
    CurrentUserDep,
    SessionDep,
    TenantPrincipal,
    TenantPrincipalDep,
    TenantSessionDep,
    require_role,
)
from decisionflow.core.errors import ValidationError
from decisionflow.db.models.tenancy import Membership, Role
from decisionflow.schemas.auth import (
    InvitationCreatedOut,
    InvitationOut,
    InviteRequest,
    MemberOut,
    OrganizationOut,
)
from decisionflow.services import auth as auth_service

router = APIRouter(prefix="/orgs", tags=["workspaces"])

AdminPrincipal = Annotated[TenantPrincipal, Depends(require_role(Role.ADMIN))]


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ChangeRoleRequest(BaseModel):
    role: Role


def _to_member_out(membership: Membership) -> MemberOut:
    """Flatten a membership and its user into the wire shape.

    Relies on `Membership.user` being loaded; the relationship is configured
    `lazy="joined"` so it always is.
    """
    return MemberOut(
        user_id=membership.user_id,
        email=membership.user.email,
        full_name=membership.user.full_name,
        role=membership.role,
        joined_at=membership.created_at,
    )


# These two are intentionally *not* workspace-scoped: listing the workspaces
# you belong to, and creating a new one, both span tenants by definition.
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
async def list_members(
    tenant: TenantPrincipalDep, session: TenantSessionDep
) -> list[MemberOut]:
    memberships = await auth_service.list_members(session, org_id=tenant.org_id)
    return [_to_member_out(m) for m in memberships]


@router.post(
    "/current/invitations",
    response_model=InvitationCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    payload: InviteRequest,
    actor: AdminPrincipal,
    user: CurrentUserDep,
    session: TenantSessionDep,
) -> InvitationCreatedOut:
    """Invite someone to the active workspace. Admin or owner only."""
    invitation, raw_token = await auth_service.invite_member(
        session,
        org_id=actor.org_id,
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
    actor: AdminPrincipal,
    session: TenantSessionDep,
) -> MemberOut:
    """Change a member's role. Only an owner may grant or revoke ownership."""
    if user_id == actor.user_id:
        # Otherwise an admin could quietly promote themselves to owner.
        raise ValidationError("You cannot change your own role.")

    membership = await auth_service.change_member_role(
        session,
        org_id=actor.org_id,
        target_user_id=user_id,
        new_role=payload.role,
        actor_role=actor.role,
    )
    return _to_member_out(membership)


@router.delete("/current/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
) -> None:
    """Remove a member, or leave the workspace by passing your own id.

    Deliberately guarded by `TenantPrincipalDep` rather than `AdminPrincipal`:
    leaving a workspace is something any member may do, so the admin
    requirement applies only when the target is someone else. The service
    enforces that distinction.
    """
    await auth_service.remove_member(
        session,
        org_id=tenant.org_id,
        target_user_id=user_id,
        actor_user_id=tenant.user_id,
        actor_role=tenant.role,
    )
