"""Wire contracts for datasets and ingestion."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from decisionflow.db.models.ingestion import (
    ColumnType,
    DatasetStatus,
    IssueCode,
    IssueSeverity,
    RunStatus,
    SourceKind,
)


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DataSourceOut(_ORMModel):
    id: uuid.UUID
    name: str
    kind: SourceKind
    created_at: datetime


class DatasetColumnOut(_ORMModel):
    id: uuid.UUID
    position: int
    name: str
    normalized_name: str
    data_type: ColumnType
    # Set when cleaning changed the type — e.g. "$1,234.56" text that became a
    # number. `effective_type` is the one analysis should use.
    cleaned_type: ColumnType | None
    effective_type: ColumnType
    source_type: str | None
    nullable: bool
    sample_values: list[Any]
    profile: dict[str, Any]


class DatasetOut(_ORMModel):
    """Summary form, used in listings."""

    id: uuid.UUID
    name: str
    slug: str
    status: DatasetStatus
    status_message: str | None
    original_filename: str
    size_bytes: int
    row_count: int | None
    column_count: int | None
    ingested_at: datetime | None
    created_at: datetime

    # Clean layer. `row_count` deliberately keeps the raw figure so the gap
    # between the two shows how many rows cleaning removed.
    clean_row_count: int | None
    cleaned_at: datetime | None
    quality_score: int | None


class DatasetDetailOut(DatasetOut):
    """Detail form, adding the schema and the audit trail of what was changed."""

    columns: list[DatasetColumnOut]
    checksum: str
    cleaning_actions: list[Any]


class QualityIssueOut(_ORMModel):
    id: uuid.UUID
    code: IssueCode
    severity: IssueSeverity
    column_name: str | None
    message: str
    details: dict[str, Any]


class QualityReportOut(BaseModel):
    """Everything known about a dataset's trustworthiness."""

    dataset_id: uuid.UUID
    quality_score: int | None
    raw_row_count: int | None
    clean_row_count: int | None
    rows_removed: int | None
    issue_counts: dict[str, int]
    issues: list[QualityIssueOut]
    cleaning_actions: list[Any]


class KpiOut(_ORMModel):
    key: str
    label: str
    description: str | None
    # Serialised as a float for JSON, computed and stored as NUMERIC. Money is
    # never held as binary floating point in the database.
    value: float | None
    format: str
    higher_is_better: bool
    # The query behind the number, so a figure the user distrusts is checkable
    # rather than something they have to take on faith.
    sql: str
    details: dict[str, Any]


class SemanticColumnOut(BaseModel):
    column: str
    role: str | None
    tags: list[str]
    rationale: str | None
    data_type: ColumnType


class SemanticsOut(BaseModel):
    """How the system understands this dataset's shape."""

    dataset_id: uuid.UUID
    columns: list[SemanticColumnOut]
    measures: list[str]
    dimensions: list[str]
    identifiers: list[str]
    time_column: str | None
    revenue_column: str | None
    customer_key: str | None


class TimeseriesPoint(BaseModel):
    period: str
    value: float | None


class TimeseriesOut(BaseModel):
    dataset_id: uuid.UUID
    grain: str
    measure: str
    time_column: str | None
    points: list[TimeseriesPoint]
    sql: str


class BreakdownItem(BaseModel):
    label: str
    value: float | None


class BreakdownOut(BaseModel):
    dataset_id: uuid.UUID
    dimension: str
    measure: str
    available_dimensions: list[str]
    items: list[BreakdownItem]
    sql: str


class CleanRequest(BaseModel):
    """Re-run cleaning with different choices.

    `deduplicate` is exposed because removing duplicate rows is the one default
    that can be genuinely wrong — two identical transactions on the same day
    may both be real.
    """

    deduplicate: bool = True


class IngestionRunOut(_ORMModel):
    id: uuid.UUID
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    rows_ingested: int | None
    error_message: str | None
    duration_seconds: float | None


class DatasetPreviewOut(BaseModel):
    """A page of actual rows."""

    dataset_id: uuid.UUID
    layer: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int | None
    limit: int
    offset: int


class UploadAcceptedOut(BaseModel):
    """Returned by the upload endpoint.

    Ingestion is asynchronous, so this reports where to look rather than the
    result: the dataset exists and its bytes are stored, but its schema and row
    count are not known yet.
    """

    dataset: DatasetOut
    job_id: str | None
    message: str = "Upload accepted. Ingestion is running in the background."
