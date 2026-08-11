"""Shared FastAPI dependencies: authentication, tenancy, and authorization.

Two levels of caller identity, deliberately distinct:

`Principal` is decoded from the access token alone and costs no database
round trip. It answers "who is this?" and is enough for endpoints that are not
scoped to a workspace.

`TenantPrincipal` additionally proves, against the database, that the caller is
*still* a member of the workspace they are acting in, and carries their *live*
role. Workspace-scoped endpoints use this one. The token's `org`/`role` claims
are treated as a hint about intent, never as authority — otherwise removing
someone from a workspace, or demoting them, would leave them holding a valid
token for up to one access-token TTL.

The membership lookup is a single indexed read on a unique key, and it shares
the request's existing session, so the cost is one cheap query rather than an
extra connection.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decisionflow.core.errors import AuthenticationError, PermissionDeniedError
from decisionflow.core.security import decode_token
from decisionflow.db.models.tenancy import Membership, Role, User
from decisionflow.db.session import TenantContext, tenant_session, untenanted_session

# auto_error=False so a missing header raises our own AuthenticationError,
# keeping the error envelope identical across every failure mode.
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, as asserted by their access token."""

    user_id: uuid.UUID
    org_id: uuid.UUID | None
    role: Role | None

    def require_org(self) -> uuid.UUID:
        if self.org_id is None:
            raise PermissionDeniedError(
                "No active workspace. Select one before calling this endpoint."
            )
        return self.org_id


@dataclass(frozen=True, slots=True)
class TenantPrincipal:
    """A caller whose workspace membership has been verified against the database."""

    user_id: uuid.UUID
    org_id: uuid.UUID
    role: Role


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
async def get_session() -> AsyncIterator[AsyncSession]:
    """A session with no tenant context.

    For identity and cross-workspace operations — login, registration, listing
    every workspace a user belongs to. Org-scoped tables have RLS policies that
    match nothing here, so this session cannot read tenant data.
    """
    async with untenanted_session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")

    payload = decode_token(credentials.credentials, expected_type="access")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Token subject is malformed.") from exc

    org_raw = payload.get("org")
    role_raw = payload.get("role")

    org_id: uuid.UUID | None = None
    if org_raw:
        try:
            org_id = uuid.UUID(org_raw)
        except ValueError as exc:
            raise AuthenticationError("Token workspace claim is malformed.") from exc

    role: Role | None = None
    if role_raw:
        try:
            role = Role(role_raw)
        except ValueError as exc:
            raise AuthenticationError("Token role claim is malformed.") from exc

    return Principal(user_id=user_id, org_id=org_id, role=role)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_tenant_session(principal: PrincipalDep) -> AsyncIterator[AsyncSession]:
    """A session scoped to the caller's active workspace, with RLS engaged."""
    org_id = principal.require_org()
    context = TenantContext(org_id=org_id, user_id=principal.user_id)
    async with tenant_session(context) as session:
        yield session


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


async def get_current_user(principal: PrincipalDep, session: SessionDep) -> User:
    """Load the live user row."""
    user = await session.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------
async def get_tenant_principal(
    principal: PrincipalDep,
    session: TenantSessionDep,
) -> TenantPrincipal:
    """Confirm the caller still belongs to the workspace, and read their live role.

    Shares the request's tenant session (FastAPI caches dependencies per
    request), so this adds one indexed lookup rather than a second connection.
    """
    org_id = principal.require_org()

    membership = await session.scalar(
        select(Membership).where(
            Membership.org_id == org_id,
            Membership.user_id == principal.user_id,
        )
    )
    if membership is None:
        raise PermissionDeniedError("You are no longer a member of this workspace.")

    return TenantPrincipal(user_id=principal.user_id, org_id=org_id, role=membership.role)


TenantPrincipalDep = Annotated[TenantPrincipal, Depends(get_tenant_principal)]


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------
def require_role(minimum: Role) -> Callable[[TenantPrincipal], Awaitable[TenantPrincipal]]:
    """Dependency factory enforcing a minimum workspace role.

    Checks the live role from `TenantPrincipal`, so a demotion takes effect on
    the caller's very next request rather than when their token expires.

    Usage: `actor: Annotated[TenantPrincipal, Depends(require_role(Role.ADMIN))]`
    """

    async def dependency(tenant: TenantPrincipalDep) -> TenantPrincipal:
        if not tenant.role.satisfies(minimum):
            raise PermissionDeniedError(
                f"This action requires the {minimum.value} role or higher."
            )
        return tenant

    return dependency


def client_ip(request: Request) -> str | None:
    """Best-effort client address for the session audit trail.

    X-Forwarded-For is trusted only in so far as it is recorded; it is
    attacker-controlled and must never drive an authorization decision.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
