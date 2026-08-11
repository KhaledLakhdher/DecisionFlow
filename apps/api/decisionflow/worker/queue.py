"""Enqueueing side of the job queue, used by the API.

Separate from the worker so importing "how to enqueue" does not drag in the
worker's startup hooks, cron schedule, and every job's dependencies.
"""

from __future__ import annotations

import uuid
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from decisionflow.core.config import settings
from decisionflow.core.logging import get_logger

log = get_logger(__name__)

_pool: ArqRedis | None = None


async def get_queue() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(
            RedisSettings(
                host=settings.redis_host,
                port=settings.redis_port,
                database=settings.redis_db,
            )
        )
    return _pool


async def close_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_ingestion(org_id: uuid.UUID, dataset_id: uuid.UUID) -> str | None:
    """Queue a dataset for ingestion. Returns the job id, or None if queueing failed.

    A queueing failure is deliberately not fatal: the upload itself succeeded
    and the bytes are safe in object storage, so the recoverable outcome is a
    dataset sitting in `uploaded` that can be retried. Failing the request
    would tell the user their upload was lost when it was not.
    """
    try:
        queue = await get_queue()
        job = await queue.enqueue_job("ingest_dataset", str(org_id), str(dataset_id))
    except Exception as exc:
        log.error("queue.enqueue_failed", dataset_id=str(dataset_id), error=str(exc))
        return None

    return job.job_id if job else None


async def job_status(job_id: str) -> dict[str, Any] | None:
    """Best-effort status lookup for a queued job."""
    from arq.jobs import Job

    try:
        queue = await get_queue()
        job = Job(job_id, queue)
        status = await job.status()
    except Exception as exc:
        log.warning("queue.status_failed", job_id=job_id, error=str(exc))
        return None

    return {"job_id": job_id, "status": status.value if status else None}
