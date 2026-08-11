"""Data ingestion: sources, datasets, detected schema, and run history.

Every table here is tenant-owned and carries `OrgScopedMixin`, which means an
RLS policy keyed on `org_id`. These are the first tables holding actual
customer data, so they are the first where a forgotten `.where(org_id == ...)`
would leak one company's figures to another — exactly what the database-level
policies exist to make impossible.

The shape follows the medallion pattern: this module lands data in a `raw`
layer, unmodified and fully traceable back to the uploaded bytes. Cleaning and
transformation (Module 2) read from `raw` and write a `clean` layer, so a bad
transform is always re-runnable from the original without asking the customer
to upload anything again.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from decisionflow.db.base import Base, OrgScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class SourceKind(enum.StrEnum):
    """Where a dataset came from.

    Only FILE_UPLOAD is wired up today; the rest are declared so adding a
    connector is a service-layer change rather than a migration.
    """

    FILE_UPLOAD = "file_upload"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLSERVER = "sqlserver"
    REST_API = "rest_api"


class DatasetStatus(enum.StrEnum):
    """Lifecycle of a dataset.

    UPLOADED means the bytes are safely in object storage — the point after
    which an ingestion failure is recoverable by retrying rather than by
    re-uploading.
    """

    UPLOADED = "uploaded"
    ANALYZING = "analyzing"
    READY = "ready"
    FAILED = "failed"


class ColumnType(enum.StrEnum):
    """Normalised logical types.

    Deliberately coarse and engine-agnostic: Polars, DuckDB and Postgres each
    have their own type zoo, and the analytics and LLM layers only ever need to
    know "is this a number, a date, or text?". Keeping the vocabulary small is
    what lets the KPI engine reason about columns generically.
    """

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    UNKNOWN = "unknown"

    @property
    def is_numeric(self) -> bool:
        return self in (ColumnType.INTEGER, ColumnType.DECIMAL)

    @property
    def is_temporal(self) -> bool:
        return self in (ColumnType.DATE, ColumnType.TIMESTAMP)


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IssueSeverity(enum.StrEnum):
    """How much a data quality finding should worry the reader.

    ERROR means the column cannot be trusted for analysis at all; WARNING means
    results using it need a caveat; INFO is an observation worth surfacing but
    not acting on. The distinction matters because the narrative layer decides
    what to mention, and a report that flags everything equally gets ignored.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueCode(enum.StrEnum):
    """Closed vocabulary of quality findings.

    An enum rather than free text so the UI can filter, the KPI engine can skip
    untrustworthy columns, and the LLM sees a stable set of concepts instead of
    prose it has to interpret.
    """

    EMPTY_COLUMN = "empty_column"
    HIGH_NULL_RATE = "high_null_rate"
    CONSTANT_COLUMN = "constant_column"
    DUPLICATE_ROWS = "duplicate_rows"
    MIXED_TYPES = "mixed_types"
    OUTLIERS = "outliers"
    HIGH_CARDINALITY = "high_cardinality"
    EMPTY_DATASET = "empty_dataset"


source_kind_enum = Enum(
    SourceKind,
    name="source_kind",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)
dataset_status_enum = Enum(
    DatasetStatus,
    name="dataset_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)
column_type_enum = Enum(
    ColumnType,
    name="column_type",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)
run_status_enum = Enum(
    RunStatus,
    name="run_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)
issue_severity_enum = Enum(
    IssueSeverity,
    name="issue_severity",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)
issue_code_enum = Enum(
    IssueCode,
    name="issue_code",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)


class DataSource(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An origin that datasets arrive from.

    Every workspace gets an implicit "File uploads" source. Database and API
    connectors will add rows here with their connection details in `config` —
    which is why credentials must never be stored in it verbatim.
    """

    __tablename__ = "data_sources"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[SourceKind] = mapped_column(source_kind_enum, nullable=False)
    # Non-secret connection details only (host, database, options). Secrets
    # belong in a secret store keyed by source id, never inline here.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    datasets: Mapped[list[Dataset]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_data_sources_org_id_name"),
    )


class Dataset(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One uploaded table — `sales.csv` and friends."""

    __tablename__ = "datasets"

    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Identifier-safe form, used as the physical DuckDB table name. Unique per
    # workspace because it names a real table in that workspace's database.
    slug: Mapped[str] = mapped_column(String(120), nullable=False)

    status: Mapped[DatasetStatus] = mapped_column(
        dataset_status_enum, nullable=False, server_default=text("'uploaded'")
    )
    status_message: Mapped[str | None] = mapped_column(Text)

    # --- provenance: enough to reproduce the load from the original bytes ---
    original_filename: Mapped[str] = mapped_column(String(400), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # SHA-256 of the uploaded file: detects a re-upload of identical content
    # and proves what was analysed.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(600), nullable=False)

    # --- results of ingestion (the `raw` layer) ---
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    column_count: Mapped[int | None] = mapped_column(Integer)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- results of cleaning (the `clean` layer) ---
    # Kept separate from `row_count` rather than overwriting it: the gap between
    # the two is exactly how many rows cleaning removed, which is a number a
    # user is entitled to see rather than have quietly applied.
    clean_row_count: Mapped[int | None] = mapped_column(BigInteger)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0-100. A single figure the dashboard can show without the reader having
    # to interpret a list of individual findings.
    quality_score: Mapped[int | None] = mapped_column(Integer)
    # The transforms that were applied, as an auditable record. A BI tool that
    # silently alters numbers is worse than one that does nothing, so every
    # change is written down and shown.
    cleaning_actions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    source: Mapped[DataSource] = relationship(back_populates="datasets", lazy="raise")
    columns: Mapped[list[DatasetColumn]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="DatasetColumn.position",
        lazy="raise",
    )
    runs: Mapped[list[IngestionRun]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="IngestionRun.created_at.desc()",
        lazy="raise",
    )
    quality_issues: Mapped[list[DataQualityIssue]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    kpis: Mapped[list[Kpi]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_datasets_org_id_slug"),
        Index("ix_datasets_org_id_status", "org_id", "status"),
    )


class DatasetColumn(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A column detected during ingestion.

    Carries `org_id` of its own rather than relying on a join to `datasets`,
    because an RLS policy can only filter on columns of the table it guards.
    """

    __tablename__ = "dataset_columns"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # As it appeared in the file — shown to humans and to the LLM, because
    # "Total Revenue (USD)" carries meaning that `total_revenue_usd` loses.
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    # Identifier-safe form, used as the physical DuckDB column name.
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)

    data_type: Mapped[ColumnType] = mapped_column(column_type_enum, nullable=False)
    # The engine's own type string, kept for debugging a bad inference.
    source_type: Mapped[str | None] = mapped_column(String(120))
    nullable: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    # A few real values, for the schema preview and for grounding LLM prompts —
    # a model writes far better SQL when it can see that `status` holds
    # 'shipped'/'pending' rather than guessing.
    sample_values: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # Per-column statistics from the profiling pass: null counts, distinct
    # counts, min/max, mean, stddev, top values. JSONB rather than twelve
    # nullable columns because the meaningful fields differ entirely by type —
    # a mean is nonsense for a string, top values are noise for a float — and
    # the consumers (KPI engine, LLM grounding) read the whole blob anyway.
    profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Type after cleaning, when coercion changed it — e.g. a "$1,234.56" column
    # detected as STRING that became DECIMAL. Null when nothing changed.
    cleaned_type: Mapped[ColumnType | None] = mapped_column(column_type_enum)

    # What the column *means*: measure, dimension, time, identifier, ignored.
    # Stored rather than re-derived on every read, so a human correction sticks
    # and every downstream consumer sees the same answer.
    semantic_role: Mapped[str | None] = mapped_column(String(40))
    semantic_tags: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Why that classification was chosen — the heuristics will sometimes be
    # wrong, and a wrong guess should be diagnosable rather than mysterious.
    semantic_rationale: Mapped[str | None] = mapped_column(Text)

    dataset: Mapped[Dataset] = relationship(back_populates="columns", lazy="raise")

    __table_args__ = (
        UniqueConstraint("dataset_id", "position", name="uq_dataset_columns_dataset_id_position"),
    )

    @property
    def effective_type(self) -> ColumnType:
        """The type analysis should use — post-cleaning when it changed."""
        return self.cleaned_type or self.data_type


class IngestionRun(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Audit record for one ingestion attempt.

    Kept as history rather than overwritten, so "this dataset broke last
    Tuesday" is answerable. When ingestion fails, this is the only place the
    reason survives.
    """

    __tablename__ = "ingestion_runs"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[RunStatus] = mapped_column(
        run_status_enum, nullable=False, server_default=text("'running'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_ingested: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(Text)

    dataset: Mapped[Dataset] = relationship(back_populates="runs", lazy="raise")

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class Kpi(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A computed business metric.

    Persisted rather than calculated per request so a dashboard loads without
    waiting on aggregation, and so a figure can be compared against what it was
    yesterday. Recomputed wholesale whenever the data changes.

    `sql` is stored deliberately: it is how a user verifies a number they do
    not believe, and how the narrative layer explains a figure without
    re-deriving it.
    """

    __tablename__ = "kpis"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Numeric rather than float: these are money. Binary floating point cannot
    # represent 0.1, and a revenue total that disagrees with the customer's
    # own books by a cent is a support ticket.
    value: Mapped[Decimal | None] = mapped_column(Numeric(38, 6))
    format: Mapped[str] = mapped_column(String(20), nullable=False)
    higher_is_better: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    sql: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    dataset: Mapped[Dataset] = relationship(back_populates="kpis", lazy="raise")

    __table_args__ = (UniqueConstraint("dataset_id", "key", name="uq_kpis_dataset_id_key"),)


class Conversation(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A thread of questions about one dataset."""

    __tablename__ = "conversations"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Derived from the opening question rather than asked for, so a thread is
    # identifiable in a list without the user naming it.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="raise",
    )


class Message(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One turn in a conversation.

    Assistant turns keep the SQL and the row count alongside the prose. Without
    them an answer is unverifiable after the fact, and "where did that number
    come from?" is the first question anyone asks of an AI-produced figure.
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)

    sql: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(80))
    # Retry corrections, token usage, and whether the question was answerable.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages", lazy="raise"
    )


class DataQualityIssue(OrgScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A quality finding about a dataset or one of its columns.

    Replaced wholesale on each cleaning run rather than accumulated: these
    describe the current state of the data, and a resolved issue lingering in
    the list would be actively misleading.
    """

    __tablename__ = "data_quality_issues"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    code: Mapped[IssueCode] = mapped_column(issue_code_enum, nullable=False)
    severity: Mapped[IssueSeverity] = mapped_column(issue_severity_enum, nullable=False)
    # Null for findings about the table as a whole (duplicate rows, empty file).
    column_name: Mapped[str | None] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Supporting numbers — null_fraction, distinct_count, sample outliers —
    # so the UI and the narrative layer can be specific rather than vague.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    dataset: Mapped[Dataset] = relationship(back_populates="quality_issues", lazy="raise")

    __table_args__ = (
        Index("ix_data_quality_issues_dataset_id_severity", "dataset_id", "severity"),
    )
