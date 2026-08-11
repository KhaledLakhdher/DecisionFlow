"""Background jobs.

Kept apart from `worker.main` so the API can import a job's name to enqueue it
without pulling in the worker's startup and cron wiring.
"""

from __future__ import annotations

import uuid
from typing import Any

from decisionflow.core.logging import get_logger
from decisionflow.db.session import (
    TenantContext,
    maintenance_session,
    tenant_session,
    untenanted_session,
)
from decisionflow.services import analytics as analytics_service
from decisionflow.services import auth as auth_service
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import pipeline as pipeline_service

log = get_logger(__name__)


async def ingest_dataset(ctx: dict[str, Any], org_id: str, dataset_id: str) -> dict[str, Any]:
    """Detect schema and load a dataset's raw table.

    `org_id` is passed explicitly and used to open a tenant-scoped session.
    A job has no request and therefore no ambient tenant context; an
    untenanted session would see zero rows under RLS and the job would fail
    with a confusing "not found" rather than a permissions error.
    """
    org_uuid = uuid.UUID(org_id)
    dataset_uuid = uuid.UUID(dataset_id)

    async with tenant_session(TenantContext(org_id=org_uuid)) as session:
        run = await ingestion_service.ingest_dataset(session, dataset_id=dataset_uuid)

        # Cleaning is a separate, independently re-runnable step, but a user
        # who uploads a file expects usable data at the end of it — not a raw
        # table and a second button to press.
        dataset = await pipeline_service.clean_dataset(session, dataset_id=dataset_uuid)

        # Classification runs on the *cleaned* types, which is why it comes
        # after cleaning rather than alongside schema detection: a revenue
        # column that arrived as "$1,249.99" is only a measure once coerced.
        await analytics_service.analyse_dataset(session, dataset_id=dataset_uuid)

        return {
            "run_id": str(run.id),
            "status": run.status.value,
            "rows_ingested": run.rows_ingested,
            "clean_rows": dataset.clean_row_count,
            "quality_score": dataset.quality_score,
        }


async def recover_stalled_ingestions(ctx: dict[str, Any]) -> int:
    """Release datasets stranded by an interrupted worker.

    Uses a cross-tenant session by necessity: a stalled run belongs to an
    organization nobody is signed in as, so there is no request context to
    borrow tenant scope from.
    """
    async with maintenance_session() as session:
        return await ingestion_service.recover_stalled_runs(session)


async def purge_refresh_tokens(ctx: dict[str, Any]) -> int:
    """Drop refresh tokens that can no longer authenticate anything.

    Every login appends a row here and nothing else removes one, so without
    this the table grows for the lifetime of the product.
    """
    async with untenanted_session() as session:
        removed = await auth_service.purge_stale_refresh_tokens(session)

    if removed:
        log.info("worker.refresh_tokens_purged", removed=removed)
    return removed
