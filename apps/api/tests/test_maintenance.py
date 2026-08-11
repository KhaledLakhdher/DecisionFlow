"""Scheduled maintenance jobs."""

from __future__ import annotations

import io
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from decisionflow.db.models.ingestion import (
    Dataset,
    DatasetStatus,
    IngestionRun,
    RunStatus,
)
from decisionflow.db.models.tenancy import RefreshToken
from decisionflow.db.session import (
    TenantContext,
    maintenance_session,
    tenant_session,
    untenanted_session,
)
from decisionflow.services import auth as auth_service
from decisionflow.services import ingestion as ingestion_service
from tests.conftest import current_user_id, register_account


async def _add_token(
    user_id: uuid.UUID, *, expires_at: datetime, revoked_at: datetime | None = None
) -> str:
    jti = secrets.token_urlsafe(16)
    async with untenanted_session() as session:
        session.add(
            RefreshToken(
                user_id=user_id,
                jti=jti,
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        )
        await session.commit()
    return jti


async def _jti_exists(jti: str) -> bool:
    async with untenanted_session() as session:
        found = await session.scalar(select(RefreshToken.id).where(RefreshToken.jti == jti))
    return found is not None


async def test_purge_removes_long_expired_tokens(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    user_id = await current_user_id(client, tokens)

    now = datetime.now(UTC)
    stale = await _add_token(user_id, expires_at=now - timedelta(days=90))
    recently_expired = await _add_token(user_id, expires_at=now - timedelta(days=2))
    live = await _add_token(user_id, expires_at=now + timedelta(days=7))

    async with untenanted_session() as session:
        removed = await auth_service.purge_stale_refresh_tokens(session, retain_days=30)

    assert removed >= 1
    assert not await _jti_exists(stale), "tokens past the retention window must go"
    assert await _jti_exists(live), "unexpired tokens must survive"
    assert await _jti_exists(
        recently_expired
    ), "recently expired tokens are kept as replay-detection evidence"


async def test_purge_removes_long_revoked_tokens(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    user_id = await current_user_id(client, tokens)

    now = datetime.now(UTC)
    # Still valid on paper, but revoked long ago — it can never authenticate.
    old_revoked = await _add_token(
        user_id,
        expires_at=now + timedelta(days=7),
        revoked_at=now - timedelta(days=90),
    )
    just_revoked = await _add_token(
        user_id,
        expires_at=now + timedelta(days=7),
        revoked_at=now - timedelta(hours=1),
    )

    async with untenanted_session() as session:
        await auth_service.purge_stale_refresh_tokens(session, retain_days=30)

    assert not await _jti_exists(old_revoked)
    assert await _jti_exists(just_revoked), "recent revocations are retained as leak evidence"


async def _make_run(
    org_id: uuid.UUID, dataset_id: uuid.UUID, *, started_at: datetime
) -> uuid.UUID:
    async with tenant_session(TenantContext(org_id=org_id)) as session:
        run = IngestionRun(
            org_id=org_id,
            dataset_id=dataset_id,
            status=RunStatus.RUNNING,
            started_at=started_at,
        )
        session.add(run)
        await session.commit()
        return run.id


async def test_stalled_runs_are_marked_failed(client: AsyncClient, unique_email) -> None:
    """A killed worker must not strand a dataset on a permanent spinner."""
    tokens = await register_account(client, unique_email())
    org_id = uuid.UUID(tokens["active_org_id"])
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    upload = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("stalled.csv", io.BytesIO(b"a,b\n1,2\n3,4\n"), "text/csv")},
        headers=headers,
    )
    dataset_id = uuid.UUID(upload.json()["dataset"]["id"])

    # Simulate the aftermath of a worker dying mid-job.
    async with tenant_session(TenantContext(org_id=org_id)) as session:
        dataset = await session.get(Dataset, dataset_id)
        assert dataset is not None
        dataset.status = DatasetStatus.ANALYZING
        await session.commit()

    run_id = await _make_run(
        org_id, dataset_id, started_at=datetime.now(UTC) - timedelta(hours=2)
    )

    async with maintenance_session() as session:
        recovered = await ingestion_service.recover_stalled_runs(session)
    assert recovered >= 1

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        run = await session.get(IngestionRun, run_id)
        dataset = await session.get(Dataset, dataset_id)

    assert run is not None and run.status is RunStatus.FAILED
    assert run.finished_at is not None
    assert "interrupted" in (run.error_message or "").lower()

    # The dataset must show a failure the user can act on, not a spinner.
    assert dataset is not None and dataset.status is DatasetStatus.FAILED
    assert "re-run" in (dataset.status_message or "").lower()


async def test_healthy_running_jobs_are_left_alone(
    client: AsyncClient, unique_email
) -> None:
    """Age-based, so a second worker starting cannot kill live jobs."""
    tokens = await register_account(client, unique_email())
    org_id = uuid.UUID(tokens["active_org_id"])
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    upload = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("fresh.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        headers=headers,
    )
    dataset_id = uuid.UUID(upload.json()["dataset"]["id"])

    # Started seconds ago — a job that is very much alive.
    run_id = await _make_run(org_id, dataset_id, started_at=datetime.now(UTC))

    async with maintenance_session() as session:
        await ingestion_service.recover_stalled_runs(session)

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        run = await session.get(IngestionRun, run_id)

    assert run is not None and run.status is RunStatus.RUNNING


async def test_purge_is_safe_to_run_on_a_clean_table() -> None:
    async with untenanted_session() as session:
        removed = await auth_service.purge_stale_refresh_tokens(session, retain_days=36500)
    assert removed == 0
