"""Identity and tenancy: organizations, users, memberships, sessions, invites.

A note on where RLS applies. These identity tables are deliberately *not*
under row-level security, because the queries against them necessarily run
before a tenant context exists — you cannot scope "find the user with this
email" to an organization during login, and "which organizations do I belong
to?" is inherently cross-tenant. Access to them is enforced in the service
layer instead.

RLS covers the tables that hold actual customer data (datasets, tables,
metrics, conversations). That is where a missing `.where(org_id == ...)` would
leak one company's numbers to another, and that is what the database-level
policies are there to make impossible.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from decisionflow.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Role(enum.StrEnum):
    """Workspace roles, ordered from most to least privileged.

    StrEnum rather than `(str, Enum)` so that formatting a role yields
    "owner" rather than "Role.OWNER" — these values reach log lines, JWT
    claims and prompt text, where the qualified form would be wrong.
    """

    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def satisfies(self, required: Role) -> bool:
        """True when this role is at least as privileged as `required`."""
        return self.rank <= required.rank


# Lower rank == more privilege, so `satisfies` is a simple comparison.
_ROLE_RANK: dict[Role, int] = {
    Role.OWNER: 0,
    Role.ADMIN: 1,
    Role.ANALYST: 2,
    Role.VIEWER: 3,
}

# `native_enum` with an explicit name so Alembic emits a real Postgres type
# rather than an unconstrained varchar.
role_enum = Enum(
    Role,
    name="workspace_role",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant. Every piece of customer data hangs off exactly one of these."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'free'"))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        lazy="raise",  # force explicit eager loading; no surprise IO in a template
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person. Global identity — one user may belong to several organizations."""

    __tablename__ = "users"

    # Normalised to lowercase by the service layer before it ever reaches here.
    # The unique constraint is therefore an exact-match one; see the functional
    # index below for the case-insensitive guarantee.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        # Belt and braces: even if a caller bypasses service-layer
        # normalisation, two users cannot differ only by capitalisation.
        Index("ix_users_email_lower", text("lower(email)"), unique=True),
    )


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Join between a user and an organization, carrying the role."""

    __tablename__ = "memberships"

    org_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[Role] = mapped_column(role_enum, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="memberships", lazy="joined")
    user: Mapped[User] = relationship(back_populates="memberships", lazy="joined")

    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_memberships_org_id_user_id"),)


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Server-side record of an issued refresh token, so sessions are revocable.

    Only the `jti` is stored — never the token itself. Reading this table
    therefore does not let an attacker mint a session.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > datetime.now(UTC)


class Invitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A pending invitation for an email address to join an organization."""

    __tablename__ = "invitations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(role_enum, nullable=False)
    # SHA-256 of the emailed token. A database leak must not yield working
    # invite links.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    __table_args__ = (
        # At most one *live* invite per address per org. Accepted invites are
        # excluded so the same person can be re-invited after leaving.
        Index(
            "uq_invitations_org_email_pending",
            "org_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL"),
        ),
    )
