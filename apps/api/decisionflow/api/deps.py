"""Shared FastAPI dependencies: authentication, tenancy, and authorization.

The access token is treated as the authority on identity *and* workspace role,
so the hot path costs no database round trip. That is safe because the token
is short-lived and every operation that changes a role also revokes the user's
refresh tokens — so a stale role cannot outlive one access-token TTL.
Endpoints that genuinely need the live user row load it explicitly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from decisionflow.core.errors import AuthenticationError, PermissionDeniedError
from decisionflow.core.security import decode_token
from decisionflow.db.models.tenancy import Role, User
from decisionflow.db.session import TenantContext, tenant_session, untenanted_session

# auto_error=False so a missing header raises our own AuthenticationError,
# keeping the error envelope identical across every failure mode.
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller and their active workspace."""

    user_id: uuid.UUID
    org_id: uuid.UUID | None
    role: Role | None

    def require_org(self) -> uuid.UUID:
        if self.org_id is None:
            raise PermissionDeniedError(
                "No active workspace. Select one before calling this endpoint."
            )
        return self.org_id


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
async def get_session() -> AsyncIterator[AsyncSession]:
    """A session with no tenant context — identity operations only."""
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


async def get_current_user(principal: PrincipalDep, session: SessionDep) -> User:
    """Load the live user row. Use only where freshness actually matters."""
    user = await session.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------
async def get_tenant_session(principal: PrincipalDep) -> AsyncIterator[AsyncSession]:
    """A session scoped to the caller's active workspace, with RLS engaged."""
    org_id = principal.require_org()
    context = TenantContext(org_id=org_id, user_id=principal.user_id)
    async with tenant_session(context) as session:
        yield session


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------
def require_role(minimum: Role) -> Callable[[Principal], Principal]:
    """Dependency factory enforcing a minimum workspace role.

    Usage: `_: Annotated[Principal, Depends(require_role(Role.ADMIN))]`
    """

    async def dependency(principal: PrincipalDep) -> Principal:
        principal.require_org()
        if principal.role is None or not principal.role.satisfies(minimum):
            raise PermissionDeniedError(
                f"This action requires the {minimum.value} role or higher."
            )
        return principal

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
