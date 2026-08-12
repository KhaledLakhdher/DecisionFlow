"""Dimensional modelling: detect relationships, classify tables, build stars.

The flow is deliberately two-step. Detection writes *proposals* with a
confidence and an explanation; only a human decision promotes one into the
model. An inferred join that silently multiplies rows and inflates revenue is
the worst failure this product could produce, so nothing joins itself
automatically.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import duckdb
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from decisionflow.core.errors import NotFoundError, ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.data import relationships as detection
from decisionflow.data import star, warehouse
from decisionflow.db.models.ingestion import (
    Dataset,
    DatasetRelationship,
    DatasetStatus,
    TableRole,
)

log = get_logger(__name__)


async def _ready_datasets(session: AsyncSession, org_id: uuid.UUID) -> list[Dataset]:
    result = await session.scalars(
        select(Dataset)
        .where(
            Dataset.org_id == org_id,
            Dataset.status == DatasetStatus.READY,
            Dataset.cleaned_at.is_not(None),
        )
        .options(selectinload(Dataset.columns))
        .order_by(Dataset.created_at)
    )
    return list(result)


async def detect_relationships(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[DatasetRelationship]:
    """Scan every pair of cleaned datasets for foreign keys."""
    datasets = await _ready_datasets(session, org_id)
    if len(datasets) < 2:
        raise ValidationError(
            "Relationship detection needs at least two processed datasets. "
            "Upload a second file — customers, products, or similar — to model "
            "them together."
        )

    by_slug = {dataset.slug: dataset for dataset in datasets}
    tables = {
        dataset.slug: [
            (column.normalized_name, column.semantic_role or "")
            for column in dataset.columns
        ]
        for dataset in datasets
    }

    def _run(connection: duckdb.DuckDBPyConnection) -> list[detection.Candidate]:
        return detection.detect(
            connection, schema=warehouse.CLEAN_SCHEMA, tables=tables
        )

    async with warehouse.warehouse(org_id) as connection:
        candidates = await asyncio.to_thread(_run, connection)

    existing = {
        (rel.from_dataset_id, rel.from_column, rel.to_dataset_id, rel.to_column): rel
        for rel in await list_relationships(session, org_id=org_id)
    }

    created: list[DatasetRelationship] = []
    for candidate in candidates:
        from_dataset = by_slug[candidate.from_table]
        to_dataset = by_slug[candidate.to_table]
        key = (
            from_dataset.id,
            candidate.from_column,
            to_dataset.id,
            candidate.to_column,
        )

        if key in existing:
            # Refresh the evidence but never overturn a human decision.
            row = existing[key]
            row.confidence = candidate.confidence
            row.rationale = candidate.rationale
            continue

        row = DatasetRelationship(
            org_id=org_id,
            from_dataset_id=from_dataset.id,
            from_column=candidate.from_column,
            to_dataset_id=to_dataset.id,
            to_column=candidate.to_column,
            confidence=candidate.confidence,
            rationale=candidate.rationale,
        )
        session.add(row)
        created.append(row)

    await session.commit()
    log.info(
        "modelling.detected",
        org_id=str(org_id),
        proposed=len(created),
        datasets=len(datasets),
    )
    return await list_relationships(session, org_id=org_id)


async def list_relationships(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[DatasetRelationship]:
    result = await session.scalars(
        select(DatasetRelationship)
        .where(DatasetRelationship.org_id == org_id)
        .order_by(DatasetRelationship.confidence.desc())
    )
    return list(result)


async def decide(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    relationship_id: uuid.UUID,
    confirmed: bool,
) -> DatasetRelationship:
    """Accept or reject a proposed relationship, then rebuild the model."""
    relationship = await session.scalar(
        select(DatasetRelationship).where(
            DatasetRelationship.id == relationship_id,
            DatasetRelationship.org_id == org_id,
        )
    )
    if relationship is None:
        raise NotFoundError("Relationship not found.")

    relationship.confirmed = confirmed
    await session.commit()

    await rebuild_model(session, org_id=org_id)
    return relationship


async def rebuild_model(session: AsyncSession, *, org_id: uuid.UUID) -> dict[str, Any]:
    """Classify tables and rebuild every star view from confirmed edges."""
    datasets = await _ready_datasets(session, org_id)
    by_id = {dataset.id: dataset for dataset in datasets}

    confirmed = [
        rel
        for rel in await list_relationships(session, org_id=org_id)
        if rel.is_usable and rel.from_dataset_id in by_id and rel.to_dataset_id in by_id
    ]

    # A table others point at is a dimension; a table that points out is a
    # fact. A table doing both is still a fact — it carries the measures, and
    # snowflaking is out of scope.
    referenced = {rel.to_dataset_id for rel in confirmed}
    referencing = {rel.from_dataset_id for rel in confirmed}

    for dataset in datasets:
        if dataset.id in referencing:
            dataset.table_role = TableRole.FACT
        elif dataset.id in referenced:
            dataset.table_role = TableRole.DIMENSION
        else:
            dataset.table_role = TableRole.UNKNOWN

    definitions: list[star.StarDefinition] = []
    for dataset in datasets:
        if dataset.table_role is not TableRole.FACT:
            continue

        joins = []
        for rel in confirmed:
            if rel.from_dataset_id != dataset.id:
                continue
            dimension = by_id[rel.to_dataset_id]
            joins.append(
                star.Join(
                    dimension_table=dimension.slug,
                    fact_column=rel.from_column,
                    dimension_column=rel.to_column,
                    columns=[
                        column.normalized_name
                        for column in dimension.columns
                        # The join key already exists on the fact side.
                        if column.normalized_name != rel.to_column
                    ],
                )
            )

        definitions.append(
            star.StarDefinition(
                fact_table=dataset.slug,
                fact_columns=[c.normalized_name for c in dataset.columns],
                joins=joins,
            )
        )

    await session.commit()

    def _apply(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {star.STAR_SCHEMA}")
        for definition in definitions:
            connection.execute(star.build_star_sql(definition))

    if definitions:
        async with warehouse.warehouse(org_id, write=True) as connection:
            await asyncio.to_thread(_apply, connection)

    log.info(
        "modelling.rebuilt",
        org_id=str(org_id),
        facts=len(definitions),
        confirmed_edges=len(confirmed),
    )

    return {
        "facts": [star.describe(definition) for definition in definitions],
        "confirmed_relationships": len(confirmed),
    }


async def get_model(session: AsyncSession, *, org_id: uuid.UUID) -> dict[str, Any]:
    """The current dimensional model, for the API and for prompt grounding."""
    datasets = await _ready_datasets(session, org_id)
    by_id = {dataset.id: dataset for dataset in datasets}
    edges = await list_relationships(session, org_id=org_id)

    return {
        "tables": [
            {
                "dataset_id": str(dataset.id),
                "name": dataset.name,
                "table": dataset.slug,
                "role": dataset.table_role.value,
                "rows": dataset.clean_row_count,
            }
            for dataset in datasets
        ],
        "relationships": [
            {
                "id": str(rel.id),
                "from_table": by_id[rel.from_dataset_id].slug
                if rel.from_dataset_id in by_id
                else None,
                "from_column": rel.from_column,
                "to_table": by_id[rel.to_dataset_id].slug
                if rel.to_dataset_id in by_id
                else None,
                "to_column": rel.to_column,
                "confidence": rel.confidence,
                "confirmed": rel.confirmed,
                "rationale": rel.rationale,
            }
            for rel in edges
        ],
    }


async def star_columns(
    session: AsyncSession, *, org_id: uuid.UUID, dataset: Dataset
) -> list[tuple[str, str]]:
    """Extra columns the star view adds, as (column, description) pairs.

    The agent needs these in its prompt: a column that exists only on the
    joined view is invisible in the fact table's own schema, so without them
    the model cannot know it may group by `customers_country`.
    """
    if dataset.table_role is not TableRole.FACT:
        return []

    datasets = await _ready_datasets(session, org_id)
    by_id = {row.id: row for row in datasets}
    confirmed = [
        rel
        for rel in await list_relationships(session, org_id=org_id)
        if rel.is_usable and rel.from_dataset_id == dataset.id and rel.to_dataset_id in by_id
    ]

    columns: list[tuple[str, str]] = []
    for rel in confirmed:
        dimension = by_id[rel.to_dataset_id]
        for column in dimension.columns:
            if column.normalized_name == rel.to_column:
                continue
            columns.append(
                (
                    f"{dimension.slug}_{column.normalized_name}",
                    f"{column.effective_type.value}, from {dimension.name} "
                    f"via {rel.from_column}",
                )
            )
    return columns


async def star_table_for(
    session: AsyncSession, *, org_id: uuid.UUID, dataset: Dataset
) -> str:
    """The table an analytical query should target for this dataset.

    The star view when the dataset is a fact with confirmed joins, otherwise
    the plain cleaned table. Callers get the widest correct surface without
    having to know whether a model exists.
    """
    if dataset.table_role is not TableRole.FACT:
        return f"{warehouse.CLEAN_SCHEMA}.{dataset.slug}"

    exists = await warehouse.table_exists(org_id, star.STAR_SCHEMA, dataset.slug)
    return (
        f"{star.STAR_SCHEMA}.{dataset.slug}"
        if exists
        else f"{warehouse.CLEAN_SCHEMA}.{dataset.slug}"
    )
