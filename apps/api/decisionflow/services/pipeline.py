"""The raw → clean pipeline.

    profile(raw) → plan → clean → profile(clean) → validate → persist

Runs entirely against tables already in DuckDB, never against the uploaded
file. That is what makes it independently re-runnable: changing a threshold and
re-cleaning costs one SQL pass, not a re-download and re-parse. It is also why
`raw` is never modified — it is the reproducible starting point.

Profiles are taken twice on purpose. The raw profile decides what to fix and is
what the quality issues describe (the user should see the problems that were
*found*). The clean profile is what analysis and the LLM consume, because it
describes the table they will actually query.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import duckdb
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from decisionflow.core.errors import NotFoundError, ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.data import warehouse
from decisionflow.data.cleaning import CleaningPlan, build_clean_sql, plan_cleaning
from decisionflow.data.profiling import ColumnProfile, profile_column
from decisionflow.data.validation import QualityIssue, check_column, check_dataset, quality_score
from decisionflow.db.models.ingestion import (
    ColumnType,
    DataQualityIssue,
    Dataset,
    DatasetColumn,
    DatasetStatus,
)

log = get_logger(__name__)


def _count_duplicates(
    connection: duckdb.DuckDBPyConnection, *, schema: str, table: str
) -> int:
    """Rows that are exact copies of another row.

    Total minus distinct, via a subquery. DuckDB rejects `count(DISTINCT *)`
    ("STAR expression is only allowed as the root element"), and phrasing it
    this way still avoids enumerating every column in the SQL text — which
    matters for wide tables and keeps customer column names out of the query.
    """
    warehouse.validate_identifier(schema)
    warehouse.validate_identifier(table)
    row = connection.execute(
        f"SELECT (SELECT count(*) FROM {schema}.{table}) - "  # noqa: S608
        f"(SELECT count(*) FROM (SELECT DISTINCT * FROM {schema}.{table}))"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _profile_table(
    connection: duckdb.DuckDBPyConnection,
    *,
    schema: str,
    table: str,
    columns: Sequence[tuple[str, ColumnType]],
) -> tuple[dict[str, ColumnProfile], int]:
    """Profile every column of a table, plus its row count."""
    warehouse.validate_identifier(schema)
    warehouse.validate_identifier(table)

    row = connection.execute(
        f"SELECT count(*) FROM {schema}.{table}"  # noqa: S608
    ).fetchone()
    row_count = int(row[0]) if row else 0

    profiles = {
        name: profile_column(
            connection,
            schema=schema,
            table=table,
            column=name,
            column_type=column_type,
            row_count=row_count,
        )
        for name, column_type in columns
    }
    return profiles, row_count


def _run_pipeline_sql(
    connection: duckdb.DuckDBPyConnection,
    *,
    table: str,
    columns: Sequence[tuple[str, ColumnType]],
    deduplicate: bool,
) -> tuple[dict[str, ColumnProfile], int, CleaningPlan, dict[str, ColumnProfile], int, int]:
    """The whole DuckDB-side pipeline, in one thread hop.

    Batched into a single callable rather than awaiting between steps because
    each hop off the event loop and back costs more than the queries do on a
    small table, and the warehouse lock is held throughout regardless.
    """
    raw_profiles, raw_rows = _profile_table(
        connection, schema=warehouse.RAW_SCHEMA, table=table, columns=columns
    )
    duplicates = _count_duplicates(connection, schema=warehouse.RAW_SCHEMA, table=table)

    plan = plan_cleaning(
        [(name, column_type, raw_profiles[name]) for name, column_type in columns],
        duplicate_rows=duplicates,
        deduplicate=deduplicate,
    )

    connection.execute(
        build_clean_sql(
            plan,
            source_schema=warehouse.RAW_SCHEMA,
            target_schema=warehouse.CLEAN_SCHEMA,
            table=table,
        )
    )

    clean_columns = [(plan_item.column, plan_item.resulting_type) for plan_item in plan.columns]
    clean_profiles, clean_rows = _profile_table(
        connection, schema=warehouse.CLEAN_SCHEMA, table=table, columns=clean_columns
    )

    return raw_profiles, raw_rows, plan, clean_profiles, clean_rows, duplicates


async def clean_dataset(
    session: AsyncSession, *, dataset_id: uuid.UUID, deduplicate: bool = True
) -> Dataset:
    """Profile, clean and validate a dataset that already has a raw table."""
    dataset = await session.scalar(
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .options(selectinload(Dataset.columns))
    )
    if dataset is None:
        raise NotFoundError("Dataset not found.")

    if dataset.status not in (DatasetStatus.READY, DatasetStatus.ANALYZING):
        raise ValidationError(
            f"This dataset has not been ingested yet (status: {dataset.status.value})."
        )

    columns: list[tuple[str, ColumnType]] = [
        (column.normalized_name, column.data_type) for column in dataset.columns
    ]
    if not columns:
        raise ValidationError("This dataset has no detected columns to clean.")

    async with warehouse.warehouse(dataset.org_id, write=True) as connection:
        (
            raw_profiles,
            raw_rows,
            plan,
            clean_profiles,
            clean_rows,
            duplicates,
        ) = await asyncio.to_thread(
            _run_pipeline_sql,
            connection,
            table=dataset.slug,
            columns=columns,
            deduplicate=deduplicate,
        )

    # --- findings, from the *raw* profile: the problems as they were found ---
    issues: list[QualityIssue] = check_dataset(
        row_count=raw_rows, duplicate_rows=duplicates if deduplicate else 0
    )
    for name, column_type in columns:
        issues.extend(
            check_column(column=name, column_type=column_type, profile=raw_profiles[name])
        )

    plan_by_column = {item.column: item for item in plan.columns}
    for column in dataset.columns:
        item = plan_by_column.get(column.normalized_name)
        # The clean profile is what analysis will query, so that is what is stored.
        column.profile = clean_profiles[column.normalized_name].to_dict()
        if item is not None and item.resulting_type is not column.data_type:
            column.cleaned_type = item.resulting_type

    # Replaced wholesale: these describe the current state, and a stale
    # resolved issue is worse than no issue at all.
    await session.execute(
        delete(DataQualityIssue).where(DataQualityIssue.dataset_id == dataset.id)
    )
    session.add_all(
        [
            DataQualityIssue(
                org_id=dataset.org_id,
                dataset_id=dataset.id,
                code=issue.code,
                severity=issue.severity,
                column_name=issue.column_name,
                message=issue.message,
                details=issue.details,
            )
            for issue in issues
        ]
    )

    dataset.clean_row_count = clean_rows
    dataset.cleaned_at = datetime.now(UTC)
    dataset.cleaning_actions = plan.to_actions()
    dataset.quality_score = quality_score(issues, column_count=len(columns))
    dataset.status = DatasetStatus.READY

    await session.commit()

    log.info(
        "pipeline.cleaned",
        dataset_id=str(dataset.id),
        raw_rows=raw_rows,
        clean_rows=clean_rows,
        duplicates_removed=raw_rows - clean_rows,
        issues=len(issues),
        quality_score=dataset.quality_score,
    )
    return dataset


async def list_issues(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> list[DataQualityIssue]:
    result = await session.scalars(
        select(DataQualityIssue)
        .where(
            DataQualityIssue.org_id == org_id,
            DataQualityIssue.dataset_id == dataset_id,
        )
        .order_by(DataQualityIssue.severity, DataQualityIssue.column_name)
    )
    return list(result)


async def get_columns(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> list[DatasetColumn]:
    result = await session.scalars(
        select(DatasetColumn)
        .where(DatasetColumn.org_id == org_id, DatasetColumn.dataset_id == dataset_id)
        .order_by(DatasetColumn.position)
    )
    return list(result)
