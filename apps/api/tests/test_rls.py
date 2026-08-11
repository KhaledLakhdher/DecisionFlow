"""Row-level security: the control that makes tenant isolation structural.

RLS fails silently — a wrong policy returns the wrong rows rather than raising.
These tests are the only thing standing between "we enabled RLS" and "RLS
works".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from decisionflow.core.config import settings
from decisionflow.db.rls import enable_rls
from decisionflow.db.session import TenantContext, tenant_session, untenanted_session

ORG_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
ORG_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
TABLE = "rls_probe"


@pytest.fixture
async def probe_table() -> AsyncIterator[None]:
    """A throwaway tenant-scoped table built with the real RLS helper.

    Seeded before RLS is switched on, because FORCE would otherwise require
    tenant context even for this setup connection.
    """
    engine = create_async_engine(settings.migration_database_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        await conn.execute(
            text(
                f"CREATE TABLE {TABLE} ("
                " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                " org_id uuid NOT NULL,"
                " label text NOT NULL)"
            )
        )
        await conn.execute(
            text(
                f"INSERT INTO {TABLE} (org_id, label)"
                " VALUES (:a, 'alpha-1'), (:a, 'alpha-2'), (:b, 'beta-1')"
            ),
            {"a": str(ORG_A), "b": str(ORG_B)},
        )
        for statement in enable_rls(TABLE):
            await conn.execute(text(statement))
        await conn.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO "{settings.app_db_user}"')
        )

    yield

    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    await engine.dispose()


async def _labels(session: AsyncSession) -> list[str]:
    result = await session.execute(text(f"SELECT label FROM {TABLE} ORDER BY label"))
    return [row[0] for row in result]


async def test_each_tenant_sees_only_its_own_rows(probe_table: None) -> None:
    async with tenant_session(TenantContext(org_id=ORG_A)) as session:
        assert await _labels(session) == ["alpha-1", "alpha-2"]

    async with tenant_session(TenantContext(org_id=ORG_B)) as session:
        assert await _labels(session) == ["beta-1"]


async def test_tenant_context_survives_commit_and_rollback(probe_table: None) -> None:
    """The subtle failure mode this whole design hinges on.

    `set_config(is_local := true)` is scoped to the transaction, so it is
    discarded on every commit. Without the `after_begin` listener re-applying
    it, the second read here would return nothing at all.
    """
    async with tenant_session(TenantContext(org_id=ORG_A)) as session:
        assert await _labels(session) == ["alpha-1", "alpha-2"]

        await session.commit()
        assert await _labels(session) == ["alpha-1", "alpha-2"]

        await session.rollback()
        assert await _labels(session) == ["alpha-1", "alpha-2"]


async def test_missing_tenant_context_fails_closed(probe_table: None) -> None:
    """No context must mean no rows — never all rows."""
    async with untenanted_session() as session:
        assert await _labels(session) == []


async def test_cannot_write_rows_for_another_tenant(probe_table: None) -> None:
    async with tenant_session(TenantContext(org_id=ORG_A)) as session:
        with pytest.raises(Exception, match=r"(?i)policy"):
            await session.execute(
                text(f"INSERT INTO {TABLE} (org_id, label) VALUES (:b, 'smuggled')"),
                {"b": str(ORG_B)},
            )
            await session.commit()
        await session.rollback()


async def test_cannot_update_another_tenants_rows(probe_table: None) -> None:
    async with tenant_session(TenantContext(org_id=ORG_A)) as session:
        result = await session.execute(
            text(f"UPDATE {TABLE} SET label = 'hijacked' WHERE label = 'beta-1'")
        )
        await session.commit()
        assert result.rowcount == 0


# Tables that carry `org_id` but are deliberately outside RLS, because the
# queries against them necessarily run before a tenant context exists:
#
#   memberships — login must answer "which workspaces does this user belong
#     to?", which is cross-tenant by definition and happens before any
#     workspace is selected.
#   invitations — accept-invite looks a row up by token hash alone; the
#     recipient is not yet a member of anything.
#
# Access to both is enforced in the service layer. Anything *not* on this list
# that grows an `org_id` column must be protected by a policy.
RLS_EXEMPT_TABLES = frozenset({"memberships", "invitations"})


async def test_every_org_scoped_table_is_protected() -> None:
    """Any table with an `org_id` column must have RLS enabled and forced.

    Derived from the live schema rather than a hand-maintained list, so adding
    a tenant table and forgetting its policy fails here instead of shipping a
    silent cross-tenant leak. `FORCE` is required too — without it the policy
    is skipped for the table's owner.
    """
    engine = create_async_engine(settings.migration_database_url)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_attribute a ON a.attrelid = c.oid
                    WHERE c.relnamespace = 'public'::regnamespace
                      AND c.relkind = 'r'
                      AND a.attname = 'org_id'
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY c.relname
                    """
                )
            )
        ).all()
    await engine.dispose()

    assert rows, "expected at least one org-scoped table"

    unprotected = [
        name
        for name, enabled, forced in rows
        if name not in RLS_EXEMPT_TABLES and not (enabled and forced)
    ]
    assert not unprotected, (
        f"org-scoped tables missing FORCE RLS: {unprotected}. "
        "Add a policy in the migration, or justify an entry in RLS_EXEMPT_TABLES."
    )

    # Guard the exemption list itself: if a table stops carrying org_id, or is
    # renamed, the stale entry should be noticed rather than silently widening
    # the exemption for some future table that reuses the name.
    present = {name for name, _, _ in rows}
    stale = RLS_EXEMPT_TABLES - present
    assert not stale, f"RLS_EXEMPT_TABLES lists tables with no org_id column: {stale}"


async def test_runtime_role_cannot_bypass_rls() -> None:
    """The API's role must hold neither SUPERUSER nor BYPASSRLS.

    Either attribute would make every policy above decorative. Note this is
    what carries the isolation guarantee — FORCE ROW LEVEL SECURITY cannot
    constrain such a role, so the check belongs on the role itself.
    """
    engine = create_async_engine(settings.migration_database_url)
    async with engine.connect() as conn:
        row: Any = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :name"),
                {"name": settings.app_db_user},
            )
        ).one()
    await engine.dispose()

    assert row[0] is False, "runtime role must not be a superuser"
    assert row[1] is False, "runtime role must not hold BYPASSRLS"
