"""Declarative base and the mixins shared by every model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names. Without this, Alembic autogenerate produces
# migrations that cannot drop the unnamed constraints Postgres invented, and
# every RLS policy or index we reference by name becomes a guess.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """UUID primary keys.

    Chosen over bigserial because IDs appear in URLs and API payloads: a
    sequential integer leaks tenant volume and invites enumeration. Generated
    database-side so an INSERT never needs a round trip to learn the ID.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Server-generated audit timestamps.

    `server_default`/`onupdate` at the database level rather than in Python, so
    rows written by migrations, the worker, or a psql session are stamped the
    same way rows written by the API are.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgScopedMixin:
    """Marks a table as tenant-owned.

    Every table carrying this mixin gets an RLS policy keyed on `org_id`
    (see the `enable_rls` helper in the migration). The index is not optional:
    every tenant-scoped query filters on this column, so without it each one
    degrades to a sequential scan as the table grows.
    """

    org_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
