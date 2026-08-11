"""The safety gate for model-generated SQL.

These are the tests that matter most in the project. The language model is
untrusted input — steered by column names and free-text questions we do not
control — and this is the only thing between its output and the engine.

Every attack below was verified to actually work against an unguarded DuckDB
connection before the guard existed. They are not hypothetical.
"""

from __future__ import annotations

import uuid

import pytest

from decisionflow.core.errors import UnsafeQueryError
from decisionflow.data import sqlguard, warehouse
from decisionflow.data.warehouse import QueryTimeoutError


# --------------------------------------------------------------------------
# What must be allowed
# --------------------------------------------------------------------------
def test_plain_select_is_allowed() -> None:
    result = sqlguard.validate("SELECT sum(revenue) FROM clean.sales")
    assert "sum(revenue)" in result.sql


def test_cte_is_allowed() -> None:
    """WITH ... SELECT is ordinary analytics SQL and parses as a SELECT."""
    result = sqlguard.validate(
        "WITH monthly AS (SELECT 1 AS v) SELECT sum(v) FROM monthly"
    )
    assert "monthly" in result.sql


def test_markdown_fences_are_stripped() -> None:
    """Models add them despite instructions; rejecting costs a retry for nothing."""
    result = sqlguard.validate("```sql\nSELECT 1\n```")
    assert result.sql.startswith("SELECT 1")


def test_trailing_semicolon_is_tolerated() -> None:
    assert sqlguard.validate("SELECT 1;").sql.startswith("SELECT 1")


# --------------------------------------------------------------------------
# Statement-level attacks
# --------------------------------------------------------------------------
def test_statement_chaining_is_rejected() -> None:
    """The classic injection shape. DuckDB parses this as two statements."""
    with pytest.raises(UnsafeQueryError, match="one statement"):
        sqlguard.validate("SELECT 1; DROP TABLE clean.sales")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE clean.sales",
        "DELETE FROM clean.sales",
        "INSERT INTO clean.sales VALUES (1)",
        "UPDATE clean.sales SET revenue = 0",
        "CREATE TABLE evil (a INT)",
        "ALTER TABLE clean.sales RENAME TO x",
    ],
)
def test_mutating_statements_are_rejected(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        sqlguard.validate(sql)


def test_unparseable_sql_is_rejected() -> None:
    with pytest.raises(UnsafeQueryError, match="not valid SQL"):
        sqlguard.validate("this is not sql")


def test_empty_query_is_rejected() -> None:
    with pytest.raises(UnsafeQueryError):
        sqlguard.validate("   ")


# --------------------------------------------------------------------------
# Filesystem attacks — these parse as ordinary SELECTs
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('C:/Windows/win.ini')",
        "SELECT * FROM read_parquet('/tmp/x.parquet')",
        "SELECT * FROM read_json('/tmp/x.json')",
        "SELECT * FROM glob('/**')",
        "SELECT read_text('/etc/hosts')",
    ],
)
def test_file_reads_are_rejected(sql: str) -> None:
    """The parser sees a normal SELECT here — only the denylist catches it early.

    Layer 1 (enable_external_access=false) blocks these at execution too; this
    rejects them sooner, with a message the agent can feed back to the model.
    """
    with pytest.raises(UnsafeQueryError, match="not permitted"):
        sqlguard.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "COPY (SELECT 1) TO '/tmp/exfiltrated.csv'",
        "ATTACH '/tmp/other.db' AS other",
        "INSTALL httpfs",
        "PRAGMA database_list",
    ],
)
def test_engine_escapes_are_rejected(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        sqlguard.validate(sql)


def test_case_and_spacing_do_not_evade_the_denylist() -> None:
    with pytest.raises(UnsafeQueryError):
        sqlguard.validate("SELECT * FROM ReAd_CsV  ('/etc/passwd')")


# --------------------------------------------------------------------------
# Table allowlist
# --------------------------------------------------------------------------
def test_unknown_tables_are_rejected() -> None:
    with pytest.raises(UnsafeQueryError, match="unknown tables"):
        sqlguard.validate(
            "SELECT * FROM clean.other_dataset", allowed_tables={"clean.sales"}
        )


def test_allowed_table_passes() -> None:
    result = sqlguard.validate(
        "SELECT * FROM clean.sales", allowed_tables={"clean.sales"}
    )
    assert result.sql.startswith("SELECT")


def test_cte_names_are_not_mistaken_for_unknown_tables() -> None:
    """CTEs are defined inside the query; flagging them would break valid SQL."""
    result = sqlguard.validate(
        "WITH monthly AS (SELECT 1 AS v FROM clean.sales) SELECT * FROM monthly",
        allowed_tables={"clean.sales"},
    )
    assert "monthly" in result.sql


# --------------------------------------------------------------------------
# Row limits
# --------------------------------------------------------------------------
def test_a_limit_is_always_applied() -> None:
    result = sqlguard.validate("SELECT * FROM clean.sales")
    assert "LIMIT" in result.sql.upper()
    assert result.limit == sqlguard.DEFAULT_ROW_LIMIT


def test_a_modest_limit_is_preserved() -> None:
    result = sqlguard.validate("SELECT * FROM clean.sales LIMIT 5")
    assert result.limit == 5
    assert result.sql.rstrip().upper().endswith("LIMIT 5")


def test_an_excessive_limit_is_capped() -> None:
    """Wrapped rather than rewritten — regex surgery on SQL ships subtle bugs."""
    result = sqlguard.validate("SELECT * FROM clean.sales LIMIT 999999")
    assert result.limit == sqlguard.MAX_ROW_LIMIT
    assert f"LIMIT {sqlguard.MAX_ROW_LIMIT}" in result.sql


# --------------------------------------------------------------------------
# The engine-level sandbox, end to end
# --------------------------------------------------------------------------
async def test_sandboxed_connection_cannot_touch_the_filesystem(tmp_path) -> None:
    """The load-bearing control, tested against a real connection.

    `read_only=True` alone does NOT prevent this — that was verified during
    design, and is why the sandbox flag exists at all.
    """
    org_id = uuid.uuid4()
    secret = tmp_path / "secret.csv"
    secret.write_text("password\nhunter2\n", encoding="utf-8")

    async with warehouse.warehouse(org_id, sandboxed=True) as connection:
        with pytest.raises(Exception, match=r"(?i)permission|not implemented|error"):
            connection.execute(f"SELECT * FROM read_csv('{secret.as_posix()}')")


async def test_sandbox_cannot_be_disabled_from_within() -> None:
    """A one-way latch: the model cannot re-enable what we switched off."""
    org_id = uuid.uuid4()

    async with warehouse.warehouse(org_id, sandboxed=True) as connection:
        with pytest.raises(Exception, match=r"(?i)cannot enable external access"):
            connection.execute("SET enable_external_access=true")


async def test_sandboxed_connection_still_runs_analytics() -> None:
    """A sandbox that blocked ordinary queries would be useless."""
    org_id = uuid.uuid4()

    async with warehouse.warehouse(org_id, write=True) as connection:
        connection.execute(
            f"CREATE OR REPLACE TABLE {warehouse.CLEAN_SCHEMA}.t AS SELECT 42 AS v"
        )

    rows = await warehouse.run_sandboxed(
        org_id, f"SELECT sum(v) AS total FROM {warehouse.CLEAN_SCHEMA}.t"
    )
    assert rows == [{"total": 42}]


async def test_a_runaway_query_is_stopped() -> None:
    """Without a deadline, one generated cross join pins a worker thread."""
    org_id = uuid.uuid4()

    with pytest.raises(QueryTimeoutError):
        await warehouse.run_sandboxed(
            org_id,
            "SELECT count(*) FROM range(100000000) a, range(100000000) b",
            timeout_seconds=2.0,
        )


async def test_sandboxed_write_is_refused() -> None:
    """Nothing legitimate needs untrusted SQL to hold a write connection."""
    org_id = uuid.uuid4()

    with pytest.raises(warehouse.WarehouseError):
        async with warehouse.warehouse(org_id, write=True, sandboxed=True):
            pass
