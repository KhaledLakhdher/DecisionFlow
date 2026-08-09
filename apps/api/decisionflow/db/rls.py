"""Helpers for declaring row-level security policies inside migrations.

Alembic's autogenerate does not see RLS policies, so they are written by hand.
Centralising the SQL here means every tenant table gets an identical, reviewed
policy rather than a slightly different hand-rolled one each time.

The policy compares `org_id` against the `app.current_org_id` setting that
`db.session` attaches to each transaction. When that setting is absent,
`current_setting(..., true)` returns NULL, the comparison is NULL, and no rows
match — so a session that forgot to establish tenant context sees nothing
rather than everything.
"""

from __future__ import annotations

TENANT_SETTING = "app.current_org_id"


def enable_rls(table: str, *, org_column: str = "org_id") -> str:
    """SQL enabling FORCE RLS on `table` with the standard tenant policy.

    FORCE is required: without it Postgres exempts the table's owner, and the
    owner is exactly who Alembic and any psql debugging session connect as.
    """
    policy = f"{table}_tenant_isolation"
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS {policy} ON {table};
    CREATE POLICY {policy} ON {table}
        USING ({org_column} = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid)
        WITH CHECK ({org_column} = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid);
    """


def disable_rls(table: str) -> str:
    """Inverse of `enable_rls`, for a migration's downgrade path."""
    policy = f"{table}_tenant_isolation"
    return f"""
    DROP POLICY IF EXISTS {policy} ON {table};
    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """
