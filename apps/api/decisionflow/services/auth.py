"""Authentication, registration, and workspace membership.

Pure business logic: this module raises `DecisionFlowError` subclasses and
knows nothing about HTTP. The router translates.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from decisionflow.core.config import settings
from decisionflow.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from decisionflow.core.logging import get_logger
from decisionflow.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_invite_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from decisionflow.db.models.tenancy import (
    Invitation,
    Membership,
    Organization,
    RefreshToken,
    Role,
    User,
)

log = get_logger(__name__)

INVITE_TTL_DAYS = 7


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _slugify(value: str) -> str:
    """ASCII, lowercase, hyphenated. Never empty."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug[:60] or "workspace"


def _candidate_slugs(base: str, attempts: int = 8) -> Iterator[str]:
    """The bare slug first, then progressively randomised variants.

    Random suffixes rather than an incrementing counter, so a slug does not
    disclose how many organizations share a name.
    """
    root = _slugify(base)
    yield root
    for _ in range(attempts - 1):
        yield f"{root}-{secrets.token_hex(3)}"


async def _insert_organization(session: AsyncSession, name: str) -> Organization:
    """Insert an organization, resolving slug collisions optimistically.

    Checking `SELECT ... WHERE slug = ?` before inserting is a time-of-check to
    time-of-use race: two concurrent requests both see the slug free and the
    loser gets an IntegrityError surfacing as a 500. Instead we attempt the
    insert and let the unique constraint — the only authority that is actually
    race-free — tell us to try again.

    A SAVEPOINT wraps each attempt so a failed insert does not poison the
    caller's surrounding transaction.
    """
    for slug in _candidate_slugs(name):
        try:
            async with session.begin_nested():
                organization = Organization(name=name.strip(), slug=slug)
                session.add(organization)
                await session.flush()
            return organization
        except IntegrityError:
            continue
    raise ConflictError("Could not allocate a unique workspace slug.")


def _hash_invite_token(token: str) -> str:
    """SHA-256, not argon2.

    These tokens are 256 bits of entropy from a CSPRNG, so there is no
    dictionary to attack and no need for a slow KDF — only for the stored form
    to be useless if the table leaks.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# Users and registration
# --------------------------------------------------------------------------
async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(func.lower(User.email) == email.lower()))


async def register(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str,
    organization_name: str,
) -> tuple[User, Organization, Membership]:
    """Create a user, their first workspace, and an owner membership.

    One transaction: a user without a workspace, or a workspace without an
    owner, is not a state this system should ever be able to reach.
    """
    if await get_user_by_email(session, email) is not None:
        raise ConflictError("An account with that email already exists.")

    user = User(
        email=email.lower(),
        full_name=full_name.strip(),
        password_hash=hash_password(password),
    )
    session.add(user)
    organization = await _insert_organization(session, organization_name)
    await session.flush()  # populate server-generated ids

    membership = Membership(org_id=organization.id, user_id=user.id, role=Role.OWNER)
    session.add(membership)
    await session.commit()

    log.info("auth.registered", user_id=str(user.id), org_id=str(organization.id))
    return user, organization, membership


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    """Verify credentials, or raise AuthenticationError.

    The same error is raised for an unknown address and a wrong password, so
    the endpoint cannot be used to enumerate registered users. The dummy hash
    on the miss path keeps the timing of the two cases comparable.
    """
    user = await get_user_by_email(session, email)

    if user is None:
        hash_password(secrets.token_urlsafe(16))
        raise AuthenticationError("Incorrect email or password.")

    if not verify_password(password, user.password_hash):
        raise AuthenticationError("Incorrect email or password.")

    if not user.is_active:
        raise PermissionDeniedError("This account has been deactivated.")

    # Transparently upgrade hashes made under an older argon2 profile.
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return user


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
async def list_memberships(session: AsyncSession, user_id: uuid.UUID) -> list[Membership]:
    result = await session.scalars(
        select(Membership)
        .where(Membership.user_id == user_id)
        .options(selectinload(Membership.organization))
        .order_by(Membership.created_at)
    )
    return list(result)


async def get_membership(
    session: AsyncSession, *, user_id: uuid.UUID, org_id: uuid.UUID
) -> Membership:
    membership = await session.scalar(
        select(Membership)
        .where(Membership.user_id == user_id, Membership.org_id == org_id)
        .options(selectinload(Membership.organization))
    )
    if membership is None:
        # 404 rather than 403: revealing that an organization exists but is
        # closed to you is itself a disclosure.
        raise NotFoundError("Workspace not found.")
    return membership


async def issue_tokens(
    session: AsyncSession,
    *,
    user: User,
    org_id: uuid.UUID | None,
    role: Role | None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str, int]:
    """Mint an access/refresh pair and record the refresh token's jti."""
    access = create_access_token(
        user_id=user.id,
        org_id=org_id,
        role=role.value if role else None,
    )
    refresh, jti, expires_at = create_refresh_token(user_id=user.id)

    session.add(
        RefreshToken(
            user_id=user.id,
            jti=jti,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:400] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
    )
    await session.commit()

    return access, refresh, settings.access_token_ttl_minutes * 60


async def rotate_refresh_token(
    session: AsyncSession,
    *,
    refresh_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str, str, int, uuid.UUID | None, Role | None]:
    """Exchange a refresh token for a fresh pair, invalidating the old one.

    Rotation on every use means a stolen refresh token is usable at most once,
    and the theft is detectable: presenting an already-revoked token is a
    signal, which is why that case revokes the user's whole token family
    rather than just failing.
    """
    payload = decode_token(refresh_token, expected_type="refresh")
    jti = payload.get("jti")
    user_id = uuid.UUID(payload["sub"])

    record = await session.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
    if record is None:
        raise AuthenticationError("Refresh token is not recognised.")

    if record.revoked_at is not None:
        # Replay of a rotated token: assume compromise and cut every session.
        log.warning("auth.refresh_replay_detected", user_id=str(user_id), jti=jti)
        await revoke_all_user_tokens(session, user_id=user_id)
        raise AuthenticationError("Refresh token has already been used.")

    if record.expires_at <= datetime.now(UTC):
        raise AuthenticationError("Refresh token has expired.")

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.")

    record.revoked_at = datetime.now(UTC)

    org_id_raw = payload.get("org")
    org_id = uuid.UUID(org_id_raw) if org_id_raw else None
    role: Role | None = None
    if org_id is None:
        memberships = await list_memberships(session, user.id)
        if memberships:
            org_id, role = memberships[0].org_id, memberships[0].role
    else:
        # Re-read the role rather than trusting the token: it may have been
        # changed or revoked since the token was minted.
        membership = await session.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.org_id == org_id
            )
        )
        if membership is None:
            org_id = None
        else:
            role = membership.role

    access, refresh, expires_in = await issue_tokens(
        session,
        user=user,
        org_id=org_id,
        role=role,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    return user, access, refresh, expires_in, org_id, role


async def revoke_refresh_token(session: AsyncSession, *, refresh_token: str) -> None:
    """Log out one session. Silent on unknown tokens — logout is idempotent."""
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except AuthenticationError:
        return

    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.jti == payload.get("jti"))
    )
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.commit()


async def purge_stale_refresh_tokens(session: AsyncSession, *, retain_days: int = 30) -> int:
    """Delete refresh tokens that can no longer authenticate anything.

    Without this the table grows without bound — every login appends a row and
    nothing ever removes one. Expired and revoked tokens are kept for
    `retain_days` first, because a revoked token showing up again is the signal
    that a refresh token leaked, and deleting the record immediately would
    discard that evidence.

    Returns the number of rows removed.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retain_days)
    # session.execute() is typed as returning Result; a DML statement actually
    # yields a CursorResult, which is what carries rowcount.
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(RefreshToken).where(
                or_(
                    RefreshToken.expires_at < cutoff,
                    RefreshToken.revoked_at.is_not(None) & (RefreshToken.revoked_at < cutoff),
                )
            )
        ),
    )
    await session.commit()
    return result.rowcount or 0


async def revoke_all_user_tokens(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    tokens = await session.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    )
    now = datetime.now(UTC)
    for token in tokens:
        token.revoked_at = now
    await session.commit()


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------
async def create_organization(
    session: AsyncSession, *, user: User, name: str
) -> tuple[Organization, Membership]:
    organization = await _insert_organization(session, name)
    await session.flush()

    membership = Membership(org_id=organization.id, user_id=user.id, role=Role.OWNER)
    session.add(membership)
    await session.commit()
    return organization, membership


async def list_members(session: AsyncSession, *, org_id: uuid.UUID) -> list[Membership]:
    result = await session.scalars(
        select(Membership)
        .where(Membership.org_id == org_id)
        .options(selectinload(Membership.user))
        .order_by(Membership.created_at)
    )
    return list(result)


async def invite_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    role: Role,
    invited_by: User,
) -> tuple[Invitation, str]:
    """Create an invitation and return it with its single-use raw token."""
    email = email.lower()

    existing_user = await get_user_by_email(session, email)
    if existing_user is not None:
        already = await session.scalar(
            select(Membership.id).where(
                Membership.org_id == org_id, Membership.user_id == existing_user.id
            )
        )
        if already is not None:
            raise ConflictError("That person is already a member of this workspace.")

    pending = await session.scalar(
        select(Invitation).where(
            Invitation.org_id == org_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
        )
    )
    if pending is not None:
        raise ConflictError("An invitation for that address is already pending.")

    raw_token = generate_invite_token()
    invitation = Invitation(
        org_id=org_id,
        email=email,
        role=role,
        token_hash=_hash_invite_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(days=INVITE_TTL_DAYS),
        invited_by_id=invited_by.id,
    )
    session.add(invitation)
    await session.commit()

    log.info("auth.invited", org_id=str(org_id), role=role.value)
    return invitation, raw_token


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    password: str | None,
    full_name: str | None,
) -> tuple[User, Membership]:
    """Redeem an invitation, creating the user account if it does not exist."""
    invitation = await session.scalar(
        select(Invitation).where(Invitation.token_hash == _hash_invite_token(token))
    )
    if invitation is None:
        raise NotFoundError("Invitation not found.")
    if invitation.accepted_at is not None:
        raise ConflictError("This invitation has already been used.")
    if invitation.expires_at <= datetime.now(UTC):
        raise ValidationError("This invitation has expired.")

    user = await get_user_by_email(session, invitation.email)
    if user is None:
        if not password:
            raise ValidationError("A password is required to create your account.")
        user = User(
            email=invitation.email,
            full_name=(full_name or invitation.email.split("@")[0]).strip(),
            password_hash=hash_password(password),
        )
        session.add(user)
        await session.flush()

    membership = await session.scalar(
        select(Membership).where(
            Membership.org_id == invitation.org_id, Membership.user_id == user.id
        )
    )
    if membership is None:
        membership = Membership(org_id=invitation.org_id, user_id=user.id, role=invitation.role)
        session.add(membership)

    invitation.accepted_at = datetime.now(UTC)
    await session.commit()

    log.info("auth.invite_accepted", user_id=str(user.id), org_id=str(invitation.org_id))
    return user, membership


async def change_member_role(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    target_user_id: uuid.UUID,
    new_role: Role,
    actor_role: Role,
) -> Membership:
    """Change a member's role, refusing to leave the workspace ownerless.

    Ownership is only ever touched by an owner. Both directions matter: an
    admin promoting someone to owner is escalation, and an admin demoting an
    existing owner is a hostile takeover. Enforced here rather than in the
    router so the rule sits beside the last-owner invariant it interacts with.
    """
    membership = await session.scalar(
        select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == target_user_id
        )
    )
    if membership is None:
        raise NotFoundError("That person is not a member of this workspace.")

    touches_ownership = membership.role is Role.OWNER or new_role is Role.OWNER
    if touches_ownership and actor_role is not Role.OWNER:
        raise PermissionDeniedError("Only an owner can change an owner's role.")

    if membership.role is Role.OWNER and new_role is not Role.OWNER:
        owner_count = await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.org_id == org_id, Membership.role == Role.OWNER)
        )
        if (owner_count or 0) <= 1:
            raise ValidationError("A workspace must always have at least one owner.")

    membership.role = new_role
    await session.commit()

    # Workspace-scoped endpoints read the live role, so the demotion binds on
    # the target's next request. Dropping their refresh tokens additionally
    # stops a stale role claim outliving this change in any token they hold.
    await revoke_all_user_tokens(session, user_id=target_user_id)
    return membership


async def remove_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    target_user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_role: Role,
) -> None:
    """Remove someone from a workspace.

    Covers two cases with one set of invariants:

      * Removing yourself is *leaving*, and any member may do it.
      * Removing someone else requires admin, and only an owner may remove
        another owner — otherwise an admin could evict the people above them.

    In both cases the last owner cannot go, or the workspace would be left with
    nobody able to administer it.
    """
    membership = await session.scalar(
        select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == target_user_id
        )
    )
    if membership is None:
        raise NotFoundError("That person is not a member of this workspace.")

    is_self = target_user_id == actor_user_id

    if not is_self:
        if not actor_role.satisfies(Role.ADMIN):
            raise PermissionDeniedError("This action requires the admin role or higher.")
        if membership.role is Role.OWNER and actor_role is not Role.OWNER:
            raise PermissionDeniedError("Only an owner can remove another owner.")

    if membership.role is Role.OWNER:
        owner_count = await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.org_id == org_id, Membership.role == Role.OWNER)
        )
        if (owner_count or 0) <= 1:
            raise ValidationError(
                "A workspace must always have at least one owner. "
                "Transfer ownership before removing this member."
            )

    await session.delete(membership)
    await session.commit()

    # Their tokens may still name this workspace. Workspace endpoints now
    # re-check membership on every request so those tokens can no longer reach
    # tenant data, but revoking refresh tokens stops them minting new ones.
    await revoke_all_user_tokens(session, user_id=target_user_id)

    log.info(
        "auth.member_removed",
        org_id=str(org_id),
        removed_user_id=str(target_user_id),
        self_service=is_self,
    )
