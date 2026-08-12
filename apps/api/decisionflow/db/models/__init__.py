"""SQLAlchemy models.

Imported for their side effect of registering with `Base.metadata`, which is
what Alembic autogenerate reflects against. A model that is not reachable from
this module will be silently missing from every migration.
"""

from decisionflow.db.models.ingestion import (
    ColumnType,
    Conversation,
    DataQualityIssue,
    Dataset,
    DatasetColumn,
    DatasetRelationship,
    DatasetStatus,
    DataSource,
    IngestionRun,
    IssueCode,
    IssueSeverity,
    Kpi,
    Message,
    RunStatus,
    SourceKind,
    TableRole,
)
from decisionflow.db.models.tenancy import (
    Invitation,
    Membership,
    Organization,
    RefreshToken,
    Role,
    User,
)

__all__ = [
    "ColumnType",
    "Conversation",
    "DataQualityIssue",
    "DataSource",
    "Dataset",
    "DatasetColumn",
    "DatasetRelationship",
    "DatasetStatus",
    "IngestionRun",
    "Invitation",
    "IssueCode",
    "IssueSeverity",
    "Kpi",
    "Membership",
    "Message",
    "Organization",
    "RefreshToken",
    "Role",
    "RunStatus",
    "SourceKind",
    "TableRole",
    "User",
]
