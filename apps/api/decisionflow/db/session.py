"""Async engine, session factory, and tenant-scoped sessions.

Tenant isolation is enforced twice, on purpose:

  1. The application layer filters by `org_id` in every repository query.
  2. Postgres RLS policies reject rows whose `org_id` does not match the
     `app.current_org_id` setting on the connection.

Layer 2 exists because layer 1 is one forgotten `.where()` away from leaking
another customer's data. The API connects as a non-owner role precisely so
those policies actually bind.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import SessionTransaction

from decisionflow.core.config import settings

# Key under which the tenant context is stashed in `Session.info`.
_TENANT_INFO_KEY = "decisionflow_tenant"


@dataclass(frozen=True, slots=True)
class TenantContext:
    org_id: uuid.UUID
    user_id: uuid.UUID | None = None


def create_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,  # survive Postgres restarts and idle-connection reaping
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
    )


engine: AsyncEngine = create_engine()

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # allow reading attributes off an object after commit
    autoflush=False,
)


@event.listens_for(SyncSession, "after_begin")
def _apply_tenant_context(
    session: SyncSession,
    transaction: SessionTransaction,
    connection: Connection,
) -> None:
    """Re-apply the RLS settings at the start of every transaction.

    `set_config(..., is_local := true)` is scoped to the current transaction,
    so it is discarded by each commit or rollback. Setting it once when the
    session opens would silently lose tenant scoping on the second transaction
    — and because the policies compare against NULL, that fails closed: the
    caller would see zero rows rather than someone else's. This hook keeps the
    context attached for the life of the session instead.
    """
    tenant: TenantContext | None = session.info.get(_TENANT_INFO_KEY)
    if tenant is None:
        return

    connection.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(tenant.org_id)},
    )
    if tenant.user_id is not None:
        connection.execute(
            text("SELECT set_config('app.current_user_id', :user_id, true)"),
            {"user_id": str(tenant.user_id)},
        )


@asynccontextmanager
async def tenant_session(tenant: TenantContext) -> AsyncIterator[AsyncSession]:
    """A session bound to one organization, with RLS enforced.

    Used by the worker and any background code. Request handlers get the same
    thing through the `get_tenant_session` dependency.
    """
    session = SessionFactory()
    session.info[_TENANT_INFO_KEY] = tenant
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def untenanted_session() -> AsyncIterator[AsyncSession]:
    """A session with no tenant context.

    Only for identity operations that necessarily precede knowing the org —
    login, registration, refresh-token exchange. Every org-scoped table's RLS
    policy compares `org_id` against an unset setting here, which evaluates to
    NULL and therefore returns no rows. Reaching for this session to read
    tenant data will simply come back empty, which is the intended failure
    mode.
    """
    session = SessionFactory()
    try:
        yield session
    finally:
        await session.close()


async def dispose_engine() -> None:
    await engine.dispose()
