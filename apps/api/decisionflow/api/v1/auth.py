"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from decisionflow.api.deps import CurrentUserDep, PrincipalDep, SessionDep, client_ip
from decisionflow.core import ratelimit
from decisionflow.schemas.auth import (
    AcceptInviteRequest,
    LoginRequest,
    MembershipOut,
    MeOut,
    RefreshRequest,
    RegisterRequest,
    SwitchOrgRequest,
    TokenPair,
    UserOut,
)
from decisionflow.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    session: SessionDep,
) -> TokenPair:
    """Create an account plus its first workspace, and sign the user in."""
    await ratelimit.enforce(
        "register:ip", client_ip(request) or "unknown", ratelimit.REGISTER_PER_IP
    )

    user, organization, membership = await auth_service.register(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        organization_name=payload.organization_name,
    )
    access, refresh, expires_in = await auth_service.issue_tokens(
        session,
        user=user,
        org_id=organization.id,
        role=membership.role,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip(request),
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        active_org_id=organization.id,
        active_role=membership.role,
    )


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenPair:
    # Both limits are checked before the password is verified, so a throttled
    # caller learns nothing about whether the account exists.
    source = client_ip(request) or "unknown"
    await ratelimit.enforce("login:ip", source, ratelimit.LOGIN_PER_IP)
    await ratelimit.enforce("login:account", payload.email, ratelimit.LOGIN_PER_ACCOUNT)

    user = await auth_service.authenticate(
        session, email=payload.email, password=payload.password
    )

    # A correct password clears the account counter, so someone who fumbled
    # their password twice is not locked out by their own successful login.
    await ratelimit.reset("login:account", payload.email)

    # Default to the workspace the user joined first; they can switch after.
    memberships = await auth_service.list_memberships(session, user.id)
    org_id = memberships[0].org_id if memberships else None
    role = memberships[0].role if memberships else None

    access, refresh, expires_in = await auth_service.issue_tokens(
        session,
        user=user,
        org_id=org_id,
        role=role,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip(request),
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        active_org_id=org_id,
        active_role=role,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, request: Request, session: SessionDep) -> TokenPair:
    """Exchange a refresh token for a new pair. The presented token is consumed."""
    _user, access, new_refresh, expires_in, org_id, role = await auth_service.rotate_refresh_token(
        session,
        refresh_token=payload.refresh_token,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip(request),
    )
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
        active_org_id=org_id,
        active_role=role,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, session: SessionDep) -> None:
    """Revoke one session. Idempotent by design."""
    await auth_service.revoke_refresh_token(session, refresh_token=payload.refresh_token)


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUserDep, principal: PrincipalDep, session: SessionDep) -> MeOut:
    memberships = await auth_service.list_memberships(session, user.id)
    return MeOut(
        user=UserOut.model_validate(user),
        active_org_id=principal.org_id,
        active_role=principal.role,
        memberships=[MembershipOut.model_validate(m) for m in memberships],
    )


@router.post("/switch-org", response_model=TokenPair)
async def switch_org(
    payload: SwitchOrgRequest,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
) -> TokenPair:
    """Issue a token pair scoped to a different workspace the caller belongs to."""
    membership = await auth_service.get_membership(
        session, user_id=user.id, org_id=payload.org_id
    )
    access, refresh, expires_in = await auth_service.issue_tokens(
        session,
        user=user,
        org_id=membership.org_id,
        role=membership.role,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip(request),
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        active_org_id=membership.org_id,
        active_role=membership.role,
    )


@router.post("/accept-invite", response_model=TokenPair)
async def accept_invite(
    payload: AcceptInviteRequest,
    request: Request,
    session: SessionDep,
) -> TokenPair:
    """Redeem an invitation, creating the account if this is a new user."""
    # Invite tokens are 256-bit, but throttling denies an attacker unlimited
    # guesses regardless.
    await ratelimit.enforce(
        "invite:ip", client_ip(request) or "unknown", ratelimit.INVITE_ACCEPT_PER_IP
    )

    user, membership = await auth_service.accept_invitation(
        session,
        token=payload.token,
        password=payload.password,
        full_name=payload.full_name,
    )
    access, refresh, expires_in = await auth_service.issue_tokens(
        session,
        user=user,
        org_id=membership.org_id,
        role=membership.role,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip(request),
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        active_org_id=membership.org_id,
        active_role=membership.role,
    )
