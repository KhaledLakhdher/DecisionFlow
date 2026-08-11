"""Test configuration.

Tests run against a real Postgres — RLS policies, unique constraints, partial
indexes and enum types are the things most worth testing, and none of them
exist in SQLite or a mock. To keep that honest without trashing the developer's
database, the suite provisions a dedicated `decisionflow_test` database, runs
the real migrations into it, and drops it afterwards.

The environment override below must happen before anything imports
`decisionflow`, because settings are read once at import time. conftest is the
first module pytest loads, which makes this the only reliable place for it.
"""

from __future__ import annotations

import os

TEST_DB_NAME = "decisionflow_test"
os.environ["POSTGRES_DB"] = TEST_DB_NAME

# Isolate the job queue and rate-limit counters too. Uploading through the API
# genuinely enqueues an ARQ job, and without this a developer's running worker
# drains those jobs and fails every one — the datasets live in a test database
# that gets dropped at the end of the session.
os.environ["REDIS_DB"] = "15"

import asyncio  # noqa: E402
import secrets  # noqa: E402
import uuid  # noqa: E402
from collections.abc import AsyncIterator, Iterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from alembic import command  # noqa: E402
from decisionflow.core.config import API_ROOT, settings  # noqa: E402

PASSWORD = "correct-horse-battery-staple"


def _maintenance_dsn() -> str:
    """DSN pointing at the always-present `postgres` database.

    CREATE/DROP DATABASE cannot run while connected to the target, so
    provisioning is done from here.
    """
    password = settings.postgres_password.get_secret_value()
    return (
        f"postgresql+asyncpg://{settings.postgres_user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )


async def _recreate_database() -> None:
    engine = create_async_engine(_maintenance_dsn(), isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        # Terminate leftovers from an interrupted run, or DROP will block.
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": TEST_DB_NAME},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await engine.dispose()


async def _provision_database() -> None:
    """Install extensions and grant the runtime role access.

    Mirrors infra/postgres/init/01-init.sh, which only ever runs against the
    development database. The default privileges must be set *before* the
    migrations run, so tables created by Alembic are reachable by the app role.
    """
    engine = create_async_engine(settings.migration_database_url, isolation_level="AUTOCOMMIT")
    app_user = settings.app_db_user
    async with engine.connect() as conn:
        for statement in (
            "CREATE EXTENSION IF NOT EXISTS vector",
            'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            f'GRANT CONNECT ON DATABASE "{TEST_DB_NAME}" TO "{app_user}"',
            f'GRANT USAGE ON SCHEMA public TO "{app_user}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_user}"',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT USAGE, SELECT ON SEQUENCES TO "{app_user}"',
        ):
            await conn.execute(text(statement))
    await engine.dispose()


async def _drop_database() -> None:
    engine = create_async_engine(_maintenance_dsn(), isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": TEST_DB_NAME},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def test_database() -> Iterator[None]:
    """Provision the test database for the whole session.

    Deliberately a *synchronous* fixture: Alembic's env.py calls asyncio.run(),
    which raises if a loop is already running. Keeping this sync means no loop
    is active when the migrations execute.
    """
    asyncio.run(_recreate_database())
    asyncio.run(_provision_database())

    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(config, "head")

    yield

    asyncio.run(_drop_database())


@pytest.fixture(autouse=True)
async def _clear_rate_limits() -> AsyncIterator[None]:
    """Reset rate-limit counters between tests.

    ASGITransport supplies no client address, so every test would otherwise
    share the "unknown" bucket and trip the per-IP limit partway through the
    run. Tests that care about throttling exercise it explicitly.
    """
    from redis.exceptions import RedisError

    from decisionflow.core.ratelimit import get_redis

    try:
        client = get_redis()
        keys = [key async for key in client.scan_iter("ratelimit:*")]
        if keys:
            await client.delete(*keys)
    except (RedisError, OSError):
        # Redis is genuinely optional — the limiter fails open, so the rest of
        # the suite is unaffected. Narrow on purpose: a broad `except` here
        # once hid a real event-loop bug behind a silently-passing fixture.
        pass
    yield


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from decisionflow.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def unique_email() -> Any:
    """Factory for collision-free addresses, so tests never depend on order."""

    def _make(prefix: str = "user") -> str:
        return f"{prefix}-{secrets.token_hex(6)}@example.com"

    return _make


async def register_account(
    client: AsyncClient, email: str, *, org_name: str = "Test Workspace"
) -> dict[str, Any]:
    """Register an account and return the token payload."""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Test User",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_header(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def current_user_id(client: AsyncClient, tokens: dict[str, Any]) -> uuid.UUID:
    """The authenticated user's id, read back from /auth/me."""
    response = await client.get("/api/v1/auth/me", headers=auth_header(tokens))
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["user"]["id"])
