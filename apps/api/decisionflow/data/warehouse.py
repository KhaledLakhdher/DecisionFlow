"""Per-workspace DuckDB analytical store.

One database file per workspace. Physical separation is the strongest tenant
boundary available for analytical data: no application bug can make a query
against one workspace's file return another's rows, because the bytes are not
in the file. It also keeps a noisy tenant's table scans off everyone else's.

Layers, following the medallion pattern:

    raw    — exactly what was uploaded, types inferred but values untouched
    clean  — validated and standardised (Module 2)

Keeping `raw` immutable means every downstream artefact is reproducible from
it, so a bad transform is a re-run rather than a request for another upload.

## Concurrency

DuckDB permits *either* one read-write process *or* several read-only ones —
never both at once. The API reads (previews, ad-hoc queries) while the worker
writes (ingestion), in separate processes, so that constraint has to be
managed rather than hoped past.

Postgres advisory locks do it: writers take an exclusive lock, readers a shared
one, keyed on the workspace. Postgres is already a hard dependency, and unlike
a file lock it works across hosts. The transaction-scoped variants are used
deliberately — `pg_advisory_xact_lock` releases when the transaction ends, so a
crashed worker cannot strand a workspace behind a lock it never unlocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import text

from decisionflow.core.config import settings
from decisionflow.core.errors import DecisionFlowError
from decisionflow.core.logging import get_logger
from decisionflow.db.session import engine

log = get_logger(__name__)

RAW_SCHEMA = "raw"
CLEAN_SCHEMA = "clean"

# DuckDB identifiers we generate ourselves. Anything reaching a table or column
# name is validated against this rather than quoted-and-hoped.
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class WarehouseError(DecisionFlowError):
    status_code = 500
    code = "warehouse_error"
    message = "The analytical store is unavailable."


class QueryTimeoutError(DecisionFlowError):
    status_code = 408
    code = "query_timeout"
    message = "The query took too long and was stopped."


def validate_identifier(name: str) -> str:
    """Reject anything that is not a plain lowercase SQL identifier.

    Table and column names are interpolated into DuckDB DDL — they cannot be
    bound as parameters. Validating against a strict allowlist is what keeps
    that from being an injection point; quoting alone is easy to get subtly
    wrong.
    """
    if not _IDENTIFIER_RE.match(name) or len(name) > 120:
        raise WarehouseError(f"Unsafe SQL identifier: {name!r}")
    return name


def warehouse_path(org_id: uuid.UUID) -> Path:
    return settings.duckdb_root / f"{org_id}.duckdb"


def _lock_key(org_id: uuid.UUID) -> int:
    """A signed 64-bit advisory-lock key derived from the workspace id.

    Advisory locks are keyed by bigint, so the UUID is truncated. Collisions
    between workspaces would only ever cost throughput, never correctness —
    two unrelated workspaces would serialise against each other.
    """
    return int.from_bytes(org_id.bytes[:8], "big", signed=True)


def _connect(
    path: Path, *, read_only: bool, external_access: bool = True
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection.

    `external_access=False` severs the engine's access to the host filesystem.
    It is required whenever the SQL being run was not written by us — see
    `sandboxed` below. It is *not* the default, because ingestion legitimately
    needs to read uploaded files.
    """
    # Annotated to DuckDB's own value union; a bare dict[str, bool] is
    # invariant and will not satisfy it.
    config: dict[str, str | bool | int | float | list[str]] = (
        {} if external_access else {"enable_external_access": False}
    )
    connection = duckdb.connect(str(path), read_only=read_only, config=config)
    if not read_only:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}")
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {CLEAN_SCHEMA}")
    return connection


def _ensure_exists(org_id: uuid.UUID) -> Path:
    """Create the workspace database if it is not there yet.

    Opening read-only fails outright on a missing file, so a workspace whose
    first action is a read still needs the file to exist.
    """
    path = warehouse_path(org_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        connection = _connect(path, read_only=False)
        connection.close()
    return path


@asynccontextmanager
async def warehouse(
    org_id: uuid.UUID, *, write: bool = False, sandboxed: bool = False
) -> AsyncIterator[duckdb.DuckDBPyConnection]:
    """Open the workspace store under the appropriate advisory lock.

    Connections are short-lived on purpose: holding one open across requests
    would block every writer for the lifetime of the process.

    Set `sandboxed=True` for any SQL this codebase did not write — model
    output, or anything derived from a user's question. It disables the
    engine's filesystem access, which `read_only` alone does *not* do: a
    read-only DuckDB connection will still happily `read_csv('/etc/passwd')`
    or `COPY (...) TO` a file on disk. Verified against DuckDB 1.5.
    """
    path = await asyncio.to_thread(_ensure_exists, org_id)
    key = _lock_key(org_id)
    lock_fn = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"

    if sandboxed and write:
        # Nothing legitimate needs both, and allowing it would let untrusted
        # SQL mutate the workspace.
        raise WarehouseError("A sandboxed connection cannot be opened for writing.")

    async with engine.connect() as lock_conn, lock_conn.begin():
        # Held until this transaction ends, including on a crash — which is
        # precisely why the transaction-scoped variant is used.
        await lock_conn.execute(text(f"SELECT {lock_fn}(:key)"), {"key": key})

        connection = await asyncio.to_thread(
            _connect, path, read_only=not write, external_access=not sandboxed
        )
        try:
            yield connection
        finally:
            await asyncio.to_thread(connection.close)


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------
def _rows_to_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [description[0] for description in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


async def fetch_all(
    org_id: uuid.UUID, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Run a read-only query and return rows as dictionaries."""

    def _run(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        return _rows_to_dicts(connection.execute(sql, params or []))

    async with warehouse(org_id) as connection:
        return await asyncio.to_thread(_run, connection)


async def table_exists(org_id: uuid.UUID, schema: str, table: str) -> bool:
    validate_identifier(schema)
    validate_identifier(table)

    def _run(connection: duckdb.DuckDBPyConnection) -> bool:
        result = connection.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
        return result is not None

    async with warehouse(org_id) as connection:
        return await asyncio.to_thread(_run, connection)


async def row_count(org_id: uuid.UUID, schema: str, table: str) -> int:
    validate_identifier(schema)
    validate_identifier(table)

    def _run(connection: duckdb.DuckDBPyConnection) -> int:
        # Suppression is safe here: schema and table passed validate_identifier
        # above, which allows only [a-z_][a-z0-9_]*. SQL has no parameter form
        # for identifiers, so interpolation is the only option.
        row = connection.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()  # noqa: S608
        return int(row[0]) if row else 0

    async with warehouse(org_id) as connection:
        return await asyncio.to_thread(_run, connection)


async def preview(
    org_id: uuid.UUID, schema: str, table: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """First rows of a table, for the UI's data preview."""
    validate_identifier(schema)
    validate_identifier(table)
    limit = max(1, min(limit, 1000))

    def _run(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        return _rows_to_dicts(
            # Identifiers validated above; the actual values stay parameterised.
            connection.execute(
                f"SELECT * FROM {schema}.{table} LIMIT ? OFFSET ?",  # noqa: S608
                [limit, max(0, offset)],
            )
        )

    async with warehouse(org_id) as connection:
        return await asyncio.to_thread(_run, connection)


async def run_sandboxed(
    org_id: uuid.UUID, sql: str, *, timeout_seconds: float = 20.0
) -> list[dict[str, Any]]:
    """Execute untrusted SQL with the engine's filesystem access severed.

    The timeout matters as much as the sandbox: a model can easily produce a
    cross join over two large tables, and without a deadline that pins a worker
    thread indefinitely. `interrupt()` is DuckDB's supported way to cancel an
    in-flight query from another thread.
    """

    def _run(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        return _rows_to_dicts(connection.execute(sql))

    async with warehouse(org_id, sandboxed=True) as connection:
        task = asyncio.create_task(asyncio.to_thread(_run, connection))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except TimeoutError:
            # Ask DuckDB to abandon the query, then let the thread unwind.
            await asyncio.to_thread(connection.interrupt)
            with contextlib.suppress(Exception):
                await task
            raise QueryTimeoutError(
                f"The query took longer than {timeout_seconds:.0f} seconds and was stopped."
            ) from None


async def drop_table(org_id: uuid.UUID, schema: str, table: str) -> None:
    validate_identifier(schema)
    validate_identifier(table)

    def _run(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(f"DROP TABLE IF EXISTS {schema}.{table}")

    async with warehouse(org_id, write=True) as connection:
        await asyncio.to_thread(_run, connection)


async def drop_workspace(org_id: uuid.UUID) -> None:
    """Delete a workspace's entire analytical store.

    Called when an organization is deleted — the Postgres rows go by cascade,
    and this removes the file they described.
    """

    def _remove() -> None:
        warehouse_path(org_id).unlink(missing_ok=True)

    await asyncio.to_thread(_remove)
    log.info("warehouse.dropped", org_id=str(org_id))
