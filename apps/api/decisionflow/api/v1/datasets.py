"""Dataset upload, inspection and deletion."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from decisionflow.api.deps import (
    TenantPrincipal,
    TenantPrincipalDep,
    TenantSessionDep,
    require_role,
)
from decisionflow.core.config import settings
from decisionflow.core.errors import ValidationError
from decisionflow.data import warehouse
from decisionflow.data.semantics import SemanticRole
from decisionflow.db.models.ingestion import ColumnType, DatasetStatus
from decisionflow.db.models.tenancy import Role
from decisionflow.schemas.datasets import (
    BreakdownOut,
    CleanRequest,
    DatasetDetailOut,
    DatasetOut,
    DatasetPreviewOut,
    IngestionRunOut,
    KpiOut,
    QualityIssueOut,
    QualityReportOut,
    SemanticColumnOut,
    SemanticsOut,
    TimeseriesOut,
    UploadAcceptedOut,
)
from decisionflow.services import analytics as analytics_service
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import pipeline as pipeline_service
from decisionflow.worker import queue

router = APIRouter(prefix="/datasets", tags=["datasets"])

AnalystPrincipal = Annotated[TenantPrincipal, Depends(require_role(Role.ANALYST))]


@router.post("/upload", response_model=UploadAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_dataset(
    actor: AnalystPrincipal,
    session: TenantSessionDep,
    file: Annotated[UploadFile, File(description="CSV, TSV or Excel file")],
) -> UploadAcceptedOut:
    """Accept a file and queue it for ingestion.

    Returns 202, not 201: the dataset row exists but its schema and row count
    are not known until the worker has run. Poll the detail endpoint, or watch
    `status`.
    """
    if not file.filename:
        raise ValidationError("A filename is required.")

    # Content-Length is a claim by the client, so it is a cheap early reject
    # rather than the real defence. The true limit is enforced below, against
    # the bytes actually received.
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise ValidationError(
            f"File exceeds the maximum upload size of "
            f"{settings.max_upload_bytes // (1024 * 1024)} MB."
        )

    dataset = await ingestion_service.register_upload(
        session,
        org_id=actor.org_id,
        user_id=actor.user_id,
        filename=file.filename,
        content_type=file.content_type,
        stream=file.file,
    )

    job_id = await queue.enqueue_ingestion(actor.org_id, dataset.id)
    message = (
        "Upload accepted. Ingestion is running in the background."
        if job_id
        else "Upload stored, but the ingestion queue is unavailable. It will need a retry."
    )

    return UploadAcceptedOut(
        dataset=DatasetOut.model_validate(dataset),
        job_id=job_id,
        message=message,
    )


@router.get("", response_model=list[DatasetOut])
async def list_datasets(
    tenant: TenantPrincipalDep, session: TenantSessionDep
) -> list[DatasetOut]:
    datasets = await ingestion_service.list_datasets(session, org_id=tenant.org_id)
    return [DatasetOut.model_validate(dataset) for dataset in datasets]


@router.get("/{dataset_id}", response_model=DatasetDetailOut)
async def get_dataset(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> DatasetDetailOut:
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return DatasetDetailOut.model_validate(dataset)


@router.get("/{dataset_id}/preview", response_model=DatasetPreviewOut)
async def preview_dataset(
    dataset_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
    layer: Annotated[Literal["clean", "raw"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DatasetPreviewOut:
    """Rows from the dataset.

    With no `layer`, serves the cleaned data when it exists and falls back to
    raw when it does not — a dataset that is ingested but not yet cleaned is a
    real transient state, and erroring on it would be unhelpful. The response
    always names the layer it actually served, so the caller never has to
    guess.

    Asking for `layer=clean` explicitly is an error when there is no clean
    table: an explicit request should not be silently answered with something
    else.
    """
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )

    if dataset.status is not DatasetStatus.READY:
        raise ValidationError(
            f"This dataset is not ready to preview (status: {dataset.status.value})."
        )

    is_cleaned = dataset.cleaned_at is not None
    if layer is None:
        resolved = "clean" if is_cleaned else "raw"
    elif layer == "clean" and not is_cleaned:
        raise ValidationError("This dataset has not been cleaned yet. Use layer=raw.")
    else:
        resolved = layer

    schema = warehouse.CLEAN_SCHEMA if resolved == "clean" else warehouse.RAW_SCHEMA
    rows = await warehouse.preview(
        tenant.org_id, schema, dataset.slug, limit=limit, offset=offset
    )
    return DatasetPreviewOut(
        dataset_id=dataset.id,
        layer=resolved,
        columns=[column.normalized_name for column in dataset.columns],
        rows=rows,
        total_rows=dataset.clean_row_count if resolved == "clean" else dataset.row_count,
        limit=limit,
        offset=offset,
    )


@router.get("/{dataset_id}/quality", response_model=QualityReportOut)
async def dataset_quality(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> QualityReportOut:
    """What is wrong with this data, and what was done about it."""
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    issues = await pipeline_service.list_issues(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.severity.value] = counts.get(issue.severity.value, 0) + 1

    rows_removed = None
    if dataset.row_count is not None and dataset.clean_row_count is not None:
        rows_removed = dataset.row_count - dataset.clean_row_count

    return QualityReportOut(
        dataset_id=dataset.id,
        quality_score=dataset.quality_score,
        raw_row_count=dataset.row_count,
        clean_row_count=dataset.clean_row_count,
        rows_removed=rows_removed,
        issue_counts=counts,
        issues=[QualityIssueOut.model_validate(issue) for issue in issues],
        cleaning_actions=dataset.cleaning_actions,
    )


@router.get("/{dataset_id}/kpis", response_model=list[KpiOut])
async def dataset_kpis(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> list[KpiOut]:
    """Automatically generated business metrics.

    Which metrics exist depends on the data's shape: revenue needs a monetary
    measure, AOV additionally needs an order key. A dataset that supports
    neither returns neither, rather than a row of zeroes.
    """
    await ingestion_service.get_dataset(session, org_id=tenant.org_id, dataset_id=dataset_id)
    kpis = await analytics_service.list_kpis(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return [KpiOut.model_validate(kpi) for kpi in kpis]


@router.get("/{dataset_id}/semantics", response_model=SemanticsOut)
async def dataset_semantics(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> SemanticsOut:
    """What the system believes each column means.

    Exposed because the classification is heuristic and will occasionally be
    wrong; a user cannot correct what they cannot see.
    """
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    semantics = analytics_service.load_semantics(dataset.columns)
    types = {column.normalized_name: column.effective_type for column in dataset.columns}

    return SemanticsOut(
        dataset_id=dataset.id,
        columns=[
            SemanticColumnOut(
                column=column.column,
                role=column.role.value,
                tags=column.tag_values,
                rationale=column.rationale,
                data_type=types.get(column.column, ColumnType.UNKNOWN),
            )
            for column in semantics.columns
        ],
        measures=[column.column for column in semantics.measures],
        dimensions=[column.column for column in semantics.dimensions],
        identifiers=[
            column.column for column in semantics.by_role(SemanticRole.IDENTIFIER)
        ],
        time_column=semantics.time_column.column if semantics.time_column else None,
        revenue_column=semantics.revenue_column.column if semantics.revenue_column else None,
        customer_key=semantics.customer_key.column if semantics.customer_key else None,
    )


@router.get("/{dataset_id}/timeseries", response_model=TimeseriesOut)
async def dataset_timeseries(
    dataset_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
    grain: Annotated[Literal["day", "week", "month", "quarter", "year"], Query()] = "month",
) -> TimeseriesOut:
    """Revenue over time, or record count when the dataset has no revenue."""
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    result = await analytics_service.timeseries(
        dataset=dataset, columns=dataset.columns, grain=grain
    )
    return TimeseriesOut(dataset_id=dataset.id, **result)


@router.get("/{dataset_id}/breakdown", response_model=BreakdownOut)
async def dataset_breakdown(
    dataset_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
    dimension: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> BreakdownOut:
    """Top values of a dimension, ranked by revenue or record count."""
    dataset = await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    result = await analytics_service.breakdown(
        dataset=dataset,
        columns=dataset.columns,
        dimension=dimension,
        limit=limit,
    )
    return BreakdownOut(dataset_id=dataset.id, **result)


@router.post("/{dataset_id}/analyse", response_model=list[KpiOut])
async def analyse_dataset(
    dataset_id: uuid.UUID, actor: AnalystPrincipal, session: TenantSessionDep
) -> list[KpiOut]:
    """Re-classify columns and recompute every KPI."""
    await ingestion_service.get_dataset(session, org_id=actor.org_id, dataset_id=dataset_id)
    await analytics_service.analyse_dataset(session, dataset_id=dataset_id)
    kpis = await analytics_service.list_kpis(
        session, org_id=actor.org_id, dataset_id=dataset_id
    )
    return [KpiOut.model_validate(kpi) for kpi in kpis]


@router.post("/{dataset_id}/clean", response_model=DatasetDetailOut)
async def clean_dataset(
    dataset_id: uuid.UUID,
    payload: CleanRequest,
    actor: AnalystPrincipal,
    session: TenantSessionDep,
) -> DatasetDetailOut:
    """Re-run cleaning against the existing raw table.

    Synchronous, unlike ingestion: this is pure SQL over data already in the
    warehouse, with no file to download or parse, so it returns in the time a
    request can afford.
    """
    await ingestion_service.get_dataset(
        session, org_id=actor.org_id, dataset_id=dataset_id
    )
    await pipeline_service.clean_dataset(
        session, dataset_id=dataset_id, deduplicate=payload.deduplicate
    )
    # Cleaning can change column types and row counts, which changes both the
    # classification and every KPI derived from it. Leaving the old numbers in
    # place would show figures that no longer match the data.
    await analytics_service.analyse_dataset(session, dataset_id=dataset_id)

    refreshed = await ingestion_service.get_dataset(
        session, org_id=actor.org_id, dataset_id=dataset_id
    )
    return DatasetDetailOut.model_validate(refreshed)


@router.get("/{dataset_id}/runs", response_model=list[IngestionRunOut])
async def list_runs(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> list[IngestionRunOut]:
    """Ingestion history — where a failure's reason survives."""
    await ingestion_service.get_dataset(session, org_id=tenant.org_id, dataset_id=dataset_id)
    runs = await ingestion_service.list_runs(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return [IngestionRunOut.model_validate(run) for run in runs]


@router.post(
    "/{dataset_id}/reingest",
    response_model=UploadAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reingest_dataset(
    dataset_id: uuid.UUID, actor: AnalystPrincipal, session: TenantSessionDep
) -> UploadAcceptedOut:
    """Re-run ingestion from the originally uploaded bytes.

    The point of keeping the upload immutable: recovering from a failed or
    outdated ingestion never requires the customer to upload anything again.
    """
    dataset = await ingestion_service.get_dataset(
        session, org_id=actor.org_id, dataset_id=dataset_id
    )
    job_id = await queue.enqueue_ingestion(actor.org_id, dataset.id)
    return UploadAcceptedOut(
        dataset=DatasetOut.model_validate(dataset),
        job_id=job_id,
        message="Re-ingestion queued." if job_id else "The ingestion queue is unavailable.",
    )


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: uuid.UUID, actor: AnalystPrincipal, session: TenantSessionDep
) -> None:
    """Delete a dataset, its raw table, and the stored file."""
    await ingestion_service.delete_dataset(
        session, org_id=actor.org_id, dataset_id=dataset_id
    )
