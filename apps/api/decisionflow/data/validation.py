"""Data quality rules.

Pure functions over profiles: given the statistics, what should a analyst be
told? No database access, no side effects — which makes every rule directly
testable and keeps the thresholds in one reviewable place.

The rules deliberately do not *fix* anything. Cleaning decides what is safe to
change automatically; validation reports what a human should know. A column
that is 80% empty gets flagged, not quietly dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decisionflow.data.profiling import ColumnProfile
from decisionflow.db.models.ingestion import ColumnType, IssueCode, IssueSeverity

# A column past this is too sparse to aggregate over honestly.
HIGH_NULL_THRESHOLD = 0.5
# Above this, a text column is almost certainly an identifier rather than a
# category, so grouping by it produces one row per record.
HIGH_CARDINALITY_THRESHOLD = 0.9
# A column typed as text where a meaningful minority parses as numbers is
# genuinely mixed — below the cleaning threshold, so coercion will not run.
MIXED_TYPE_LOWER = 0.5
MIXED_TYPE_UPPER = 0.95
# Outliers are normal in business data; only an unusual concentration is worth
# a mention.
OUTLIER_THRESHOLD = 0.05


@dataclass(slots=True)
class QualityIssue:
    code: IssueCode
    severity: IssueSeverity
    message: str
    column_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _percent(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def check_column(
    *, column: str, column_type: ColumnType, profile: ColumnProfile
) -> list[QualityIssue]:
    """Every finding for one column."""
    issues: list[QualityIssue] = []

    if profile.is_empty:
        issues.append(
            QualityIssue(
                code=IssueCode.EMPTY_COLUMN,
                severity=IssueSeverity.ERROR,
                column_name=column,
                message=f"Column '{column}' is entirely empty and cannot be used.",
                details={"null_count": profile.null_count},
            )
        )
        # Every other rule would be noise on top of this.
        return issues

    if profile.null_fraction >= HIGH_NULL_THRESHOLD:
        issues.append(
            QualityIssue(
                code=IssueCode.HIGH_NULL_RATE,
                severity=IssueSeverity.WARNING,
                column_name=column,
                message=(
                    f"Column '{column}' is {_percent(profile.null_fraction)} empty. "
                    "Totals and averages over it will be misleading."
                ),
                details={
                    "null_count": profile.null_count,
                    "null_fraction": round(profile.null_fraction, 4),
                },
            )
        )

    if profile.is_constant:
        issues.append(
            QualityIssue(
                code=IssueCode.CONSTANT_COLUMN,
                severity=IssueSeverity.INFO,
                column_name=column,
                message=(
                    f"Column '{column}' holds the same value in every row, "
                    "so it cannot explain any variation."
                ),
                details={"distinct_count": profile.distinct_count},
            )
        )

    if column_type is ColumnType.STRING:
        numeric_like = profile.numeric_like_fraction or 0.0
        if MIXED_TYPE_LOWER <= numeric_like < MIXED_TYPE_UPPER:
            issues.append(
                QualityIssue(
                    code=IssueCode.MIXED_TYPES,
                    severity=IssueSeverity.WARNING,
                    column_name=column,
                    message=(
                        f"Column '{column}' mixes numbers and text — "
                        f"{_percent(numeric_like)} of values are numeric. "
                        "It was left as text to avoid discarding the rest."
                    ),
                    details={"numeric_like_fraction": round(numeric_like, 4)},
                )
            )

        if (
            profile.distinct_fraction >= HIGH_CARDINALITY_THRESHOLD
            and profile.row_count > 10
        ):
            issues.append(
                QualityIssue(
                    code=IssueCode.HIGH_CARDINALITY,
                    severity=IssueSeverity.INFO,
                    column_name=column,
                    message=(
                        f"Column '{column}' is almost entirely unique values, "
                        "which usually means an identifier rather than a category."
                    ),
                    details={"distinct_fraction": round(profile.distinct_fraction, 4)},
                )
            )

    if column_type.is_numeric and profile.outlier_count:
        non_null = profile.row_count - profile.null_count
        fraction = (profile.outlier_count / non_null) if non_null else 0.0
        if fraction >= OUTLIER_THRESHOLD:
            issues.append(
                QualityIssue(
                    code=IssueCode.OUTLIERS,
                    severity=IssueSeverity.INFO,
                    column_name=column,
                    message=(
                        f"Column '{column}' has {profile.outlier_count} unusually "
                        f"extreme values ({_percent(fraction)} of rows). "
                        "They may be genuine, or data-entry errors."
                    ),
                    details={
                        "outlier_count": profile.outlier_count,
                        "q1": profile.q1,
                        "q3": profile.q3,
                    },
                )
            )

    return issues


def check_dataset(*, row_count: int, duplicate_rows: int) -> list[QualityIssue]:
    """Findings about the table as a whole."""
    issues: list[QualityIssue] = []

    if row_count == 0:
        issues.append(
            QualityIssue(
                code=IssueCode.EMPTY_DATASET,
                severity=IssueSeverity.ERROR,
                message="This dataset contains no rows.",
            )
        )
        return issues

    if duplicate_rows > 0:
        fraction = duplicate_rows / row_count
        noun = "duplicate row" if duplicate_rows == 1 else "duplicate rows"
        verb = "was" if duplicate_rows == 1 else "were"
        issues.append(
            QualityIssue(
                code=IssueCode.DUPLICATE_ROWS,
                severity=IssueSeverity.WARNING if fraction > 0.01 else IssueSeverity.INFO,
                message=(
                    f"{duplicate_rows} {noun} ({_percent(fraction)}) {verb} found "
                    "and removed from the cleaned table."
                ),
                details={
                    "duplicate_rows": duplicate_rows,
                    "duplicate_fraction": round(fraction, 4),
                },
            )
        )

    return issues


def quality_score(issues: list[QualityIssue], *, column_count: int) -> int:
    """A 0-100 summary of how trustworthy the dataset is.

    Deliberately simple and explainable: errors cost far more than warnings,
    and the penalty is scaled by how much of the dataset is affected, so one
    bad column in fifty is not treated like one bad column in three. A score
    nobody can reason about is worse than no score.
    """
    if column_count <= 0:
        return 0

    weights = {
        IssueSeverity.ERROR: 25.0,
        IssueSeverity.WARNING: 10.0,
        IssueSeverity.INFO: 2.0,
    }

    penalty = 0.0
    for issue in issues:
        weight = weights[issue.severity]
        # Column-level findings are diluted by dataset width; table-level ones
        # apply in full, because they affect every column at once.
        penalty += weight / column_count if issue.column_name else weight

    return max(0, min(100, round(100 - penalty)))
