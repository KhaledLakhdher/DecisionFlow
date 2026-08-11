"""Helpers for declaring row-level security policies inside migrations.

Alembic's autogenerate does not see RLS policies, so they are written by hand.
Centralising the SQL here means every tenant table gets an identical, reviewed
policy rather than a slightly different hand-rolled one each time.

The policy compares `org_id` against the `app.current_org_id` setting that
`db.session` attaches to each transaction. When that setting is absent,
`current_setting(..., true)` returns NULL, the comparison is NULL, and no rows
match — so a session that forgot to establish tenant context sees nothing
rather than everything.

Both helpers return a *sequence of individual statements*. asyncpg prepares
every statement it executes and refuses to prepare more than one command at a
time, so a single semicolon-joined blob fails at runtime. Returning a list also
spares callers from splitting SQL on ";", which breaks the moment a policy
expression contains one.
"""

from __future__ import annotations

TENANT_SETTING = "app.current_org_id"

# Repeated in USING and WITH CHECK so the policy governs reads and writes
# alike: without WITH CHECK a tenant could insert rows attributed to another.
_MATCH = "{column} = NULLIF(current_setting('" + TENANT_SETTING + "', true), '')::uuid"


def policy_name(table: str) -> str:
    return f"{table}_tenant_isolation"


def enable_rls(table: str, *, org_column: str = "org_id") -> tuple[str, ...]:
    """Statements enabling FORCE RLS on `table` with the standard tenant policy.

    FORCE removes the exemption Postgres grants a table's ordinary owner. It
    does *not* — and cannot — constrain a SUPERUSER or a role holding
    BYPASSRLS; those ignore row-level security entirely.

    That matters here because the Postgres image's bootstrap role is a
    superuser, and it owns these tables. So the migration role does see every
    row. Isolation rests on the API connecting as `app_db_user`, which holds
    neither attribute. Hardening step for production: make the schema owner an
    ordinary non-superuser role, at which point FORCE starts to bite as well.

    `tests/test_rls.py` asserts both halves of this.
    """
    match = _MATCH.format(column=org_column)
    policy = policy_name(table)
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {policy} ON {table}",
        (
            f"CREATE POLICY {policy} ON {table} "
            f"USING ({match}) "
            f"WITH CHECK ({match})"
        ),
    )


def disable_rls(table: str) -> tuple[str, ...]:
    """Inverse of `enable_rls`, for a migration's downgrade path."""
    return (
        f"DROP POLICY IF EXISTS {policy_name(table)} ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    )
