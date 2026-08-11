"""ARQ worker: ingestion jobs and scheduled maintenance.

Run alongside the API with:

    arq decisionflow.worker.main.WorkerSettings

Jobs run without an HTTP request and therefore without ambient tenant context.
Anything touching org-scoped tables must open a `tenant_session` explicitly —
an untenanted session sees nothing, by design.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from decisionflow.core.config import settings
from decisionflow.core.logging import configure_logging, get_logger
from decisionflow.db.session import dispose_engine
from decisionflow.storage import objects
from decisionflow.worker.jobs import (
    ingest_dataset,
    purge_refresh_tokens,
    recover_stalled_ingestions,
)

log = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    settings.duckdb_root.mkdir(parents=True, exist_ok=True)
    await objects.ensure_bucket()
    log.info("worker.startup", environment=settings.environment)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    log.info("worker.shutdown")


class WorkerSettings:
    """Entry point discovered by the `arq` CLI."""

    redis_settings = RedisSettings(
        host=settings.redis_host, port=settings.redis_port, database=settings.redis_db
    )
    on_startup = startup
    on_shutdown = shutdown

    functions: ClassVar[list[Any]] = [ingest_dataset]

    cron_jobs: ClassVar[list[Any]] = [
        # 03:17 daily — off the hour, so it does not pile onto every other
        # cron job in the system at once.
        cron(purge_refresh_tokens, hour=3, minute=17),
        # Every 10 minutes. A dataset stranded by a killed worker shows a
        # permanent spinner until this runs, so the interval is the worst-case
        # time a user spends staring at one.
        cron(recover_stalled_ingestions, minute={0, 10, 20, 30, 40, 50}),
    ]

    # Ingestion is CPU- and memory-heavy; a handful at a time keeps a burst of
    # uploads from exhausting the box.
    max_jobs = 4
    job_timeout = 900  # 15 minutes for a large spreadsheet
