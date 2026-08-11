"""Dataset ingestion.

Two phases, split deliberately:

  `register_upload` runs inside the HTTP request. It streams the file to object
  storage and creates the Dataset row, then returns. Fast and bounded.

  `ingest_dataset` runs in the worker. It reads the stored file, detects the
  schema, and loads the raw table into DuckDB. Slow and unbounded.

The split exists because parsing a 200 MB spreadsheet inside a request handler
means a request that times out, a user staring at a spinner, and a worker
process pinned on CPU while other requests queue behind it. Once the bytes are
in object storage the work is durable and retryable, which is the property that
matters.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import duckdb
import polars as pl
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from decisionflow.core.errors import ConflictError, NotFoundError, ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.data import warehouse
from decisionflow.data.schema import (
    DetectedSchema,
    detect_schema,
    read_full,
    validate_upload_name,
)
from decisionflow.db.models.ingestion import (
    Dataset,
    DatasetColumn,
    DatasetStatus,
    DataSource,
    IngestionRun,
    RunStatus,
    SourceKind,
)
from decisionflow.storage import objects

log = get_logger(__name__)

DEFAULT_SOURCE_NAME = "File uploads"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _slugify_dataset(name: str) -> str:
    """Identifier-safe base name for the physical DuckDB table."""
    stem = Path(name).stem
    slug = _NON_ALNUM.sub("_", stem.lower()).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}" if slug else "dataset"
    return slug[:100]


async def _unique_slug(session: AsyncSession, org_id: uuid.UUID, base: str) -> str:
    """A slug free within this workspace.

    Unlike the workspace-slug case, a numeric suffix is fine here: this names a
    table the customer already owns, so `sales_2` discloses nothing they cannot
    see anyway, and it is far more legible than a random hex suffix.
    """
    slug = base
    for attempt in range(2, 200):
        exists = await session.scalar(
            select(Dataset.id).where(Dataset.org_id == org_id, Dataset.slug == slug)
        )
        if exists is None:
            return slug
        slug = f"{base}_{attempt}"
    raise ConflictError("Could not allocate a unique dataset name.")


async def get_or_create_upload_source(session: AsyncSession, org_id: uuid.UUID) -> DataSource:
    """The implicit "File uploads" source every workspace has.

    Created lazily on first upload rather than at workspace creation, so a
    workspace that never uploads anything carries no empty scaffolding.
    """
    source = await session.scalar(
        select(DataSource).where(
            DataSource.org_id == org_id, DataSource.kind == SourceKind.FILE_UPLOAD
        )
    )
    if source is not None:
        return source

    source = DataSource(org_id=org_id, name=DEFAULT_SOURCE_NAME, kind=SourceKind.FILE_UPLOAD)
    session.add(source)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # A concurrent upload created it first; take theirs.
        await session.rollback()
        source = await session.scalar(
            select(DataSource).where(
                DataSource.org_id == org_id, DataSource.kind == SourceKind.FILE_UPLOAD
            )
        )
        if source is None:  # pragma: no cover - only on a genuine race loss
            raise
    return source


# --------------------------------------------------------------------------
# Phase 1 — request path
# --------------------------------------------------------------------------
async def register_upload(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
    content_type: str | None,
    stream: IO[bytes],
) -> Dataset:
    """Store an uploaded file and register the dataset awaiting ingestion."""
    validate_upload_name(filename)

    source = await get_or_create_upload_source(session, org_id)
    display_name = Path(filename).stem[:200] or "dataset"
    slug = await _unique_slug(session, org_id, _slugify_dataset(filename))

    dataset = Dataset(
        org_id=org_id,
        source_id=source.id,
        name=display_name,
        slug=slug,
        status=DatasetStatus.UPLOADED,
        original_filename=filename[:400],
        content_type=content_type,
        size_bytes=0,
        checksum="",
        storage_key="",
        created_by_id=user_id,
    )
    session.add(dataset)
    await session.flush()  # need the id to build the storage key

    stored = await objects.put_object(
        objects.build_key(org_id, dataset.id, filename), stream
    )

    if stored.size_bytes == 0:
        raise ValidationError("The uploaded file is empty.")

    dataset.storage_key = stored.key
    dataset.size_bytes = stored.size_bytes
    dataset.checksum = stored.checksum
    await session.commit()

    log.info(
        "ingestion.upload_registered",
        dataset_id=str(dataset.id),
        org_id=str(org_id),
        size_bytes=stored.size_bytes,
    )
    return dataset


# --------------------------------------------------------------------------
# Phase 2 — worker path
# --------------------------------------------------------------------------
def _load_raw_table(
    connection: duckdb.DuckDBPyConnection,
    *,
    frame: pl.DataFrame,
    table: str,
    detected: DetectedSchema,
) -> int:
    """Load a dataframe into `raw.<table>`.

    Columns are renamed *in Polars* rather than aliased in SQL. That is the
    point of doing it this way: original headers are arbitrary customer text
    ("Total Revenue (USD)", or a stray double quote), and building DDL out of
    them means hand-escaping identifiers correctly every time. Renaming first
    means the only identifiers that ever reach SQL are ones we generated and
    validated.

    The frame is handed over as Arrow, which DuckDB reads without copying.
    """
    warehouse.validate_identifier(table)

    renamed = frame.rename(
        {column.name: warehouse.validate_identifier(column.normalized_name)
         for column in detected.columns}
    )

    # Referenced by name in the SQL below; DuckDB reads straight from the
    # Arrow buffer.
    arrow_table = renamed.to_arrow()
    connection.register("incoming_frame", arrow_table)
    try:
        # Suppressions below are safe: `table` passed validate_identifier and
        # RAW_SCHEMA is a module constant. Column names were normalised in
        # Polars, so no customer-supplied text reaches this SQL at all.
        connection.execute(f"DROP TABLE IF EXISTS {warehouse.RAW_SCHEMA}.{table}")
        connection.execute(
            f"CREATE TABLE {warehouse.RAW_SCHEMA}.{table} AS SELECT * FROM incoming_frame"  # noqa: S608
        )
        row = connection.execute(
            f"SELECT count(*) FROM {warehouse.RAW_SCHEMA}.{table}"  # noqa: S608
        ).fetchone()
    finally:
        connection.unregister("incoming_frame")

    return int(row[0]) if row else 0


async def ingest_dataset(session: AsyncSession, *, dataset_id: uuid.UUID) -> IngestionRun:
    """Detect the schema and materialise the raw table. Runs in the worker.

    Failures are recorded on both the run and the dataset rather than raised:
    the caller is a background job with nobody to report to, so the status is
    the only channel the user ever sees.
    """
    dataset = await session.scalar(
        select(Dataset).where(Dataset.id == dataset_id).options(selectinload(Dataset.columns))
    )
    if dataset is None:
        raise NotFoundError("Dataset not found.")

    run = IngestionRun(org_id=dataset.org_id, dataset_id=dataset.id, status=RunStatus.RUNNING)
    session.add(run)
    dataset.status = DatasetStatus.ANALYZING
    dataset.status_message = None
    await session.commit()

    try:
        with tempfile.TemporaryDirectory(prefix="df-ingest-") as tmpdir:
            local_path = Path(tmpdir) / Path(dataset.original_filename).name
            await objects.download_to(dataset.storage_key, str(local_path))

            # Parsing is CPU-bound and releases the GIL inside Polars; keeping
            # it off the event loop matters because the worker also serves
            # other jobs.
            frame = await asyncio.to_thread(read_full, local_path)
            detected = detect_schema(frame)

            async with warehouse.warehouse(dataset.org_id, write=True) as connection:
                rows = await asyncio.to_thread(
                    _load_raw_table,
                    connection,
                    frame=frame,
                    table=dataset.slug,
                    detected=detected,
                )

        # Replace the previous schema wholesale; a re-ingest may have changed it.
        await session.execute(
            delete(DatasetColumn).where(DatasetColumn.dataset_id == dataset.id)
        )
        session.add_all(
            [
                DatasetColumn(
                    org_id=dataset.org_id,
                    dataset_id=dataset.id,
                    position=column.position,
                    name=column.name,
                    normalized_name=column.normalized_name,
                    data_type=column.data_type,
                    source_type=column.source_type,
                    nullable=column.nullable,
                    sample_values=column.sample_values,
                )
                for column in detected.columns
            ]
        )

        now = datetime.now(UTC)
        dataset.status = DatasetStatus.READY
        dataset.row_count = rows
        dataset.column_count = len(detected.columns)
        dataset.ingested_at = now

        run.status = RunStatus.SUCCEEDED
        run.rows_ingested = rows
        run.finished_at = now
        await session.commit()

        log.info(
            "ingestion.succeeded",
            dataset_id=str(dataset.id),
            rows=rows,
            columns=len(detected.columns),
        )

    except Exception as exc:
        await session.rollback()

        # Re-fetch: the rollback detached whatever state we had staged.
        dataset = await session.get(Dataset, dataset_id)
        run_row = await session.get(IngestionRun, run.id)
        message = str(exc)[:2000]

        if dataset is not None:
            dataset.status = DatasetStatus.FAILED
            dataset.status_message = message
        if run_row is not None:
            run_row.status = RunStatus.FAILED
            run_row.error_message = message
            run_row.finished_at = datetime.now(UTC)
        await session.commit()

        log.error("ingestion.failed", dataset_id=str(dataset_id), error=message)
        raise

    return run


# --------------------------------------------------------------------------
# Reads and deletion
# --------------------------------------------------------------------------
async def list_datasets(session: AsyncSession, *, org_id: uuid.UUID) -> list[Dataset]:
    result = await session.scalars(
        select(Dataset).where(Dataset.org_id == org_id).order_by(Dataset.created_at.desc())
    )
    return list(result)


async def get_dataset(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> Dataset:
    dataset = await session.scalar(
        select(Dataset)
        .where(Dataset.id == dataset_id, Dataset.org_id == org_id)
        .options(selectinload(Dataset.columns))
    )
    if dataset is None:
        raise NotFoundError("Dataset not found.")
    return dataset


async def list_runs(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> list[IngestionRun]:
    result = await session.scalars(
        select(IngestionRun)
        .where(IngestionRun.org_id == org_id, IngestionRun.dataset_id == dataset_id)
        .order_by(IngestionRun.created_at.desc())
    )
    return list(result)


async def count_datasets(session: AsyncSession, *, org_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Dataset).where(Dataset.org_id == org_id)
        )
    ) or 0


async def recover_stalled_runs(
    session: AsyncSession, *, stale_after_seconds: int = 1800
) -> int:
    """Mark abandoned ingestion runs as failed.

    A worker that is killed mid-job — a deploy, an OOM, a laptop closing —
    leaves its run stuck on RUNNING and its dataset stuck on ANALYZING. Nothing
    else ever revisits them, so the dataset is stranded: the UI shows a
    permanent spinner and offers no way out. This is what makes that
    recoverable.

    Age-based rather than "fail everything on startup", because a second worker
    starting must not kill the jobs a healthy first worker is still running.
    Anything older than the job timeout is dead by definition.

    Requires a session that spans tenants — see `maintenance_session`.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)

    stalled = list(
        await session.scalars(
            select(IngestionRun).where(
                IngestionRun.status == RunStatus.RUNNING,
                IngestionRun.started_at < cutoff,
            )
        )
    )
    if not stalled:
        return 0

    message = (
        "Processing was interrupted, most likely because the worker stopped. "
        "The uploaded file is intact — re-run ingestion to try again."
    )
    now = datetime.now(UTC)

    for run in stalled:
        run.status = RunStatus.FAILED
        run.finished_at = now
        run.error_message = message

        dataset = await session.get(Dataset, run.dataset_id)
        if dataset is not None and dataset.status is DatasetStatus.ANALYZING:
            dataset.status = DatasetStatus.FAILED
            dataset.status_message = message

    await session.commit()
    log.warning("ingestion.stalled_runs_recovered", count=len(stalled))
    return len(stalled)


async def delete_dataset(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> None:
    """Remove a dataset and everything derived from it."""
    dataset = await get_dataset(session, org_id=org_id, dataset_id=dataset_id)
    storage_key = dataset.storage_key
    slug = dataset.slug

    await session.delete(dataset)  # columns and runs go by cascade
    await session.commit()

    # Best-effort cleanup of the derived artefacts. Ordering matters: the row
    # is gone first, so a failure here leaves recoverable orphans rather than a
    # dataset the user can see but no longer use.
    #
    # Both layers, not just raw — the clean table is equally derived, and
    # leaving it behind means a re-upload under the same name inherits stale
    # data from a dataset the user believes they deleted.
    for schema in (warehouse.RAW_SCHEMA, warehouse.CLEAN_SCHEMA):
        await warehouse.drop_table(org_id, schema, slug)
    if storage_key:
        await objects.delete_objects([storage_key])

    log.info("ingestion.dataset_deleted", dataset_id=str(dataset_id), org_id=str(org_id))
