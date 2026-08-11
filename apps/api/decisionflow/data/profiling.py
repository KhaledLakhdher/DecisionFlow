"""Column profiling.

Statistics are computed by DuckDB, in SQL, rather than by pulling rows into
Python. That is the whole point of having a columnar engine: `count(DISTINCT
x)` over ten million rows is a vectorised scan the database is built for, and
the alternative — materialising the column as Python objects — is both slower
and bounded by RAM.

Everything here reads; nothing writes. The profile drives two later decisions:
which cleaning transforms are worth applying, and which columns the KPI engine
and the LLM can trust.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from decisionflow.data.warehouse import validate_identifier
from decisionflow.db.models.ingestion import ColumnType

TOP_VALUE_COUNT = 10
# Values that look like a person typed "no data" into a spreadsheet. Compared
# case-insensitively after trimming.
NULL_TOKENS = frozenset({"", "na", "n/a", "n.a.", "null", "none", "nil", "-", "--", "?", "unknown"})


@dataclass(slots=True)
class ColumnProfile:
    """Statistics for one column. Serialised into `DatasetColumn.profile`."""

    row_count: int = 0
    null_count: int = 0
    null_fraction: float = 0.0
    distinct_count: int = 0
    distinct_fraction: float = 0.0
    is_constant: bool = False
    is_empty: bool = False

    # Numeric only.
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None
    median_value: float | None = None
    stddev_value: float | None = None
    q1: float | None = None
    q3: float | None = None
    outlier_count: int | None = None

    # Temporal only.
    min_date: str | None = None
    max_date: str | None = None

    # String only.
    blank_count: int | None = None
    null_token_count: int | None = None
    # Values carrying leading or trailing whitespace. Tracked separately from
    # blanks because a padded value is not an empty one — " Ada " needs
    # trimming even though the column contains no blanks at all.
    whitespace_count: int | None = None
    numeric_like_fraction: float | None = None
    date_like_fraction: float | None = None
    boolean_like_fraction: float | None = None
    top_values: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form. NaN and infinity are not valid JSON."""
        return {key: _json_safe(value) for key, value in asdict(self).items()}


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _scalar(row: tuple[Any, ...] | None, index: int) -> Any:
    return row[index] if row is not None else None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def profile_column(
    connection: duckdb.DuckDBPyConnection,
    *,
    schema: str,
    table: str,
    column: str,
    column_type: ColumnType,
    row_count: int,
) -> ColumnProfile:
    """Profile a single column.

    Split by type because the useful statistics differ completely: a mean is
    meaningless for a customer name, and the top ten values are noise for a
    float. Computing everything for everything would be slower and produce a
    profile that is mostly nulls.
    """
    validate_identifier(schema)
    validate_identifier(table)
    validate_identifier(column)
    qualified = f"{schema}.{table}"

    base = connection.execute(
        f"SELECT count(*) - count({column}), count(DISTINCT {column}) FROM {qualified}"  # noqa: S608
    ).fetchone()
    null_count = int(_scalar(base, 0) or 0)
    distinct_count = int(_scalar(base, 1) or 0)

    profile = ColumnProfile(
        row_count=row_count,
        null_count=null_count,
        null_fraction=(null_count / row_count) if row_count else 0.0,
        distinct_count=distinct_count,
        distinct_fraction=(distinct_count / row_count) if row_count else 0.0,
        is_constant=distinct_count == 1 and row_count > 1,
        is_empty=null_count == row_count and row_count > 0,
    )

    if profile.is_empty:
        # Every other statistic is null by construction; skip the work.
        return profile

    if column_type.is_numeric:
        _profile_numeric(connection, qualified, column, profile)
    elif column_type.is_temporal:
        _profile_temporal(connection, qualified, column, profile)
    elif column_type is ColumnType.STRING:
        _profile_string(connection, qualified, column, profile, row_count)

    return profile


def _profile_numeric(
    connection: duckdb.DuckDBPyConnection, qualified: str, column: str, profile: ColumnProfile
) -> None:
    row = connection.execute(
        f"""
        SELECT min({column}), max({column}), avg({column}),
               median({column}), stddev_samp({column}),
               quantile_cont({column}, 0.25), quantile_cont({column}, 0.75)
        FROM {qualified}
        """  # noqa: S608
    ).fetchone()

    profile.min_value = _as_float(_scalar(row, 0))
    profile.max_value = _as_float(_scalar(row, 1))
    profile.mean_value = _as_float(_scalar(row, 2))
    profile.median_value = _as_float(_scalar(row, 3))
    profile.stddev_value = _as_float(_scalar(row, 4))
    profile.q1 = _as_float(_scalar(row, 5))
    profile.q3 = _as_float(_scalar(row, 6))

    # Tukey's rule: outside 1.5 IQR of the quartiles. Chosen over a
    # standard-deviation rule because business data is rarely normal — revenue
    # is heavily right-skewed, and a 3-sigma test would flag ordinary large
    # orders as anomalies.
    if profile.q1 is not None and profile.q3 is not None:
        iqr = profile.q3 - profile.q1
        if iqr > 0:
            low = profile.q1 - 1.5 * iqr
            high = profile.q3 + 1.5 * iqr
            count_row = connection.execute(
                f"SELECT count(*) FROM {qualified} "  # noqa: S608
                f"WHERE {column} IS NOT NULL AND ({column} < ? OR {column} > ?)",
                [low, high],
            ).fetchone()
            profile.outlier_count = int(_scalar(count_row, 0) or 0)
        else:
            profile.outlier_count = 0


def _profile_temporal(
    connection: duckdb.DuckDBPyConnection, qualified: str, column: str, profile: ColumnProfile
) -> None:
    row = connection.execute(
        f"SELECT min({column}), max({column}) FROM {qualified}"  # noqa: S608
    ).fetchone()
    minimum, maximum = _scalar(row, 0), _scalar(row, 1)
    profile.min_date = minimum.isoformat() if minimum is not None else None
    profile.max_date = maximum.isoformat() if maximum is not None else None


def _profile_string(
    connection: duckdb.DuckDBPyConnection,
    qualified: str,
    column: str,
    profile: ColumnProfile,
    row_count: int,
) -> None:
    """Profile a text column, including how much of it is *not really* text.

    The coercion fractions are the interesting part: a column Polars typed as
    STRING whose values are all "$1,234.56" is a number wearing a disguise, and
    knowing that is what lets the cleaning stage recover it.
    """
    token_list = ", ".join(f"'{token}'" for token in sorted(NULL_TOKENS))
    # Placeholders are excluded from the coercion denominator. They are missing
    # values wearing a costume, and counting them as "not a number" is what
    # would stop a perfectly good revenue column — "$1,249.99" with a single
    # "N/A" — from being recognised as numeric.
    real_value = f"{column} IS NOT NULL AND lower(trim({column})) NOT IN ({token_list})"

    row = connection.execute(
        f"""
        SELECT
            count(*) FILTER (WHERE trim({column}) = ''),
            count(*) FILTER (WHERE lower(trim({column})) IN ({token_list})),
            count(*) FILTER (WHERE {column} <> trim({column})),
            count(*) FILTER (
                WHERE {real_value}
                  AND TRY_CAST(regexp_replace(trim({column}), '[$€£,\\s]', '', 'g') AS DOUBLE)
                      IS NOT NULL
            ),
            count(*) FILTER (
                WHERE {real_value} AND TRY_CAST(trim({column}) AS DATE) IS NOT NULL
            ),
            count(*) FILTER (
                WHERE {real_value}
                  AND lower(trim({column})) IN
                      ('true','false','yes','no','y','n','t','f','1','0')
            ),
            count(*) FILTER (WHERE {real_value})
        FROM {qualified}
        """  # noqa: S608
    ).fetchone()

    profile.blank_count = int(_scalar(row, 0) or 0)
    profile.null_token_count = int(_scalar(row, 1) or 0)
    profile.whitespace_count = int(_scalar(row, 2) or 0)

    real_values = int(_scalar(row, 6) or 0)
    if real_values:
        profile.numeric_like_fraction = int(_scalar(row, 3) or 0) / real_values
        profile.date_like_fraction = int(_scalar(row, 4) or 0) / real_values
        profile.boolean_like_fraction = int(_scalar(row, 5) or 0) / real_values

    # Only meaningful for genuinely categorical columns. On a column of unique
    # identifiers the "top values" are ten arbitrary rows.
    if profile.distinct_count <= max(TOP_VALUE_COUNT * 5, 50):
        rows = connection.execute(
            f"""
            SELECT {column} AS value, count(*) AS frequency
            FROM {qualified}
            WHERE {column} IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC, 1
            LIMIT {TOP_VALUE_COUNT}
            """  # noqa: S608
        ).fetchall()
        profile.top_values = [
            {
                "value": str(value),
                "count": int(frequency),
                "fraction": (frequency / row_count) if row_count else 0.0,
            }
            for value, frequency in rows
        ]
