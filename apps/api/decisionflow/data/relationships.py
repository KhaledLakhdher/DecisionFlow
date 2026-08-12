"""Relationship detection between datasets.

A CSV carries no foreign keys, so the joins that make a star schema have to be
inferred. The inference must be conservative: a wrong join does not error, it
silently multiplies rows and reports inflated revenue. That is the worst
failure mode in the whole product — a confident wrong number — so detection
*proposes* and a human confirms.

Three signals, in order of authority:

1. **Value containment.** What share of the many side's values exist on the one
   side? This is the real evidence; names can lie, data cannot.
2. **Uniqueness of the target.** A foreign key must point at something unique.
   If the target column repeats, the join is many-to-many and would fan out
   rows — rejected outright rather than scored down.
3. **Name similarity.** `customer_id → customer_id`, or `customer_id → id` on a
   table called `customers`. Only ever a tie-breaker, never sufficient alone:
   two unrelated tables both having `id` is the most common false positive
   there is.

Candidates are pruned using the semantic layer — only columns already
classified as identifiers are considered. That turns an O(columns²) scan across
every pair of tables into a handful of comparisons, and removes whole classes
of nonsense (matching a revenue column against a quantity column because the
numbers happen to overlap).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

from decisionflow.core.logging import get_logger
from decisionflow.data.semantics import SemanticRole
from decisionflow.data.warehouse import validate_identifier

log = get_logger(__name__)

# Below this share of resolving values the columns are unrelated. Deliberately
# high: a partial overlap usually means coincidence (small integer ranges
# overlap constantly), not a broken foreign key.
MIN_CONTAINMENT = 0.80
# A target with fewer distinct values than this is not a key — matching against
# a handful of status codes would "contain" almost anything.
MIN_TARGET_DISTINCT = 2
# Ignore near-empty tables; overlap ratios are meaningless on a couple of rows.
# Deliberately low: a dimension is often tiny (three product lines, five
# regions), and excluding small tables would miss exactly the joins a star
# schema is made of.
MIN_ROWS = 3
# Below this many distinct target values, containment is weak evidence — almost
# any column falls inside a three-value set by chance — so a matching name is
# required as corroboration.
SMALL_TARGET_DISTINCT = 5

# `(^|_)` so a bare `id` matches as well as `customer_id`. Without the
# alternation, `customers.id` — half of the two conventions this is meant to
# recognise — is never seen as a key at all.
_KEY_SUFFIX = re.compile(r"(^|_)(id|key|code|ref|no|number)$", re.I)

# Columns are only compared within a type family. Without this, detection asks
# DuckDB to test `customer_id = customer_name` and gets a conversion error
# rather than a low score — types are a hard gate, not a signal.
_TYPE_FAMILIES = {
    "numeric": (
        "INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT",
        "DECIMAL", "DOUBLE", "FLOAT", "UBIGINT", "UINTEGER",
    ),
    "text": ("VARCHAR", "CHAR", "TEXT", "STRING", "UUID"),
    "temporal": ("DATE", "TIMESTAMP", "TIME"),
    "boolean": ("BOOLEAN",),
}


def type_family(duckdb_type: str) -> str:
    """Collapse a DuckDB type into a comparability class."""
    upper = duckdb_type.upper()
    for family, prefixes in _TYPE_FAMILIES.items():
        if any(upper.startswith(prefix) for prefix in prefixes):
            return family
    return "other"


@dataclass(slots=True)
class Candidate:
    """One proposed foreign key."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    containment: float
    distinct_values: int
    name_match: bool
    rationale: str

    @property
    def confidence(self) -> float:
        """Containment, nudged by a name match.

        Kept close to the measured containment on purpose. A confidence that
        drifts far from the evidence invites trusting the wrong number.
        """
        score = self.containment
        if self.name_match:
            score = min(1.0, score + 0.05)
        return round(score, 4)


def _singularise(name: str) -> str:
    """`customers` -> `customer`. Crude by design; only used for name hints."""
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def names_agree(*, from_column: str, to_table: str, to_column: str) -> bool:
    """Whether the column names support the relationship.

    Covers the two conventions that actually appear in exported data:
    `orders.customer_id -> customers.customer_id` (same name), and
    `orders.customer_id -> customers.id` (target table named for the key).
    """
    if from_column.lower() == to_column.lower():
        return True

    stem = _KEY_SUFFIX.sub("", from_column.lower())
    table_stem = _singularise(to_table.lower())

    # orders.customer_id -> customers.id
    if stem and stem == table_stem and _KEY_SUFFIX.search(to_column.lower()):
        return True
    # orders.customer_id -> customers.customer_ref
    target_stem = _KEY_SUFFIX.sub("", to_column.lower())
    return bool(stem and target_stem and stem == target_stem)


def _identifier_columns(columns: list[tuple[str, str]]) -> list[str]:
    """Column names classified as identifiers by the semantic layer."""
    return [
        name
        for name, role in columns
        if role in (SemanticRole.IDENTIFIER.value, SemanticRole.DIMENSION.value)
    ]


def _row_count(connection: duckdb.DuckDBPyConnection, schema: str, table: str) -> int:
    row = connection.execute(
        f"SELECT count(*) FROM {schema}.{table}"  # noqa: S608
    ).fetchone()
    return int(row[0]) if row else 0


def _column_types(
    connection: duckdb.DuckDBPyConnection, schema: str, table: str
) -> dict[str, str]:
    rows = connection.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchall()
    return {str(name): str(dtype) for name, dtype in rows}


def _distinct_and_total(
    connection: duckdb.DuckDBPyConnection, schema: str, table: str, column: str
) -> tuple[int, int]:
    row = connection.execute(
        f"SELECT count(DISTINCT {column}), count({column}) FROM {schema}.{table}"  # noqa: S608
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _containment(
    connection: duckdb.DuckDBPyConnection,
    *,
    schema: str,
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
) -> float:
    """Share of the source's distinct values that exist in the target.

    Distinct rather than row-weighted, so one very common value cannot carry
    the score on its own.
    """
    row = connection.execute(
        f"""
        WITH source AS (
            SELECT DISTINCT {from_column} AS v
            FROM {schema}.{from_table}
            WHERE {from_column} IS NOT NULL
        )
        SELECT
            count(*),
            count(*) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM {schema}.{to_table} t
                    WHERE t.{to_column} = source.v
                )
            )
        FROM source
        """  # noqa: S608
    ).fetchone()

    if not row or not row[0]:
        return 0.0
    return float(row[1]) / float(row[0])


def detect(
    connection: duckdb.DuckDBPyConnection,
    *,
    schema: str,
    tables: dict[str, list[tuple[str, str]]],
) -> list[Candidate]:
    """Find foreign keys among a workspace's tables.

    `tables` maps table name to its (column, semantic_role) pairs.
    """
    validate_identifier(schema)

    usable = {
        table: _identifier_columns(columns)
        for table, columns in tables.items()
        if _row_count(connection, schema, validate_identifier(table)) >= MIN_ROWS
    }

    # Distinct/total and type per candidate column, computed once rather than
    # per pair.
    stats: dict[tuple[str, str], tuple[int, int]] = {}
    families: dict[tuple[str, str], str] = {}
    for table, columns in usable.items():
        types = _column_types(connection, schema, validate_identifier(table))
        for column in columns:
            stats[(table, column)] = _distinct_and_total(
                connection, schema, validate_identifier(table), validate_identifier(column)
            )
            families[(table, column)] = type_family(types.get(column, ""))

    found: list[Candidate] = []

    for from_table, from_columns in usable.items():
        for to_table, to_columns in usable.items():
            if from_table == to_table:
                continue

            for from_column in from_columns:
                for to_column in to_columns:
                    # A hard gate, checked before any query runs: comparing a
                    # BIGINT key against a VARCHAR name is not a weak match,
                    # it is a conversion error from the engine.
                    if (
                        families[(from_table, from_column)]
                        != families[(to_table, to_column)]
                        or families[(from_table, from_column)] == "other"
                    ):
                        continue

                    to_distinct, to_total = stats[(to_table, to_column)]

                    # The target must be a genuine key. A repeating column
                    # makes the join many-to-many, which fans out rows and
                    # silently inflates every sum computed over it.
                    if to_distinct < MIN_TARGET_DISTINCT or to_distinct != to_total:
                        continue

                    containment = _containment(
                        connection,
                        schema=schema,
                        from_table=validate_identifier(from_table),
                        from_column=validate_identifier(from_column),
                        to_table=validate_identifier(to_table),
                        to_column=validate_identifier(to_column),
                    )
                    if containment < MIN_CONTAINMENT:
                        continue

                    name_match = names_agree(
                        from_column=from_column, to_table=to_table, to_column=to_column
                    )

                    # Weak statistical evidence needs corroborating names.
                    if to_distinct < SMALL_TARGET_DISTINCT and not name_match:
                        continue

                    orphans = ""
                    if containment < 1.0:
                        orphans = (
                            f" {(1 - containment) * 100:.0f}% of values have no "
                            "match, which may indicate missing reference data."
                        )

                    found.append(
                        Candidate(
                            from_table=from_table,
                            from_column=from_column,
                            to_table=to_table,
                            to_column=to_column,
                            containment=round(containment, 4),
                            distinct_values=to_distinct,
                            name_match=name_match,
                            rationale=(
                                f"{containment * 100:.0f}% of {from_table}.{from_column} "
                                f"values exist in {to_table}.{to_column}, which holds "
                                f"{to_distinct} unique values."
                                + (" Column names agree." if name_match else "")
                                + orphans
                            ),
                        )
                    )

    # Strongest evidence first, and only the best target per source column:
    # a column cannot be a foreign key into two different tables at once.
    found.sort(key=lambda c: (c.confidence, c.name_match), reverse=True)

    best: dict[tuple[str, str], Candidate] = {}
    for candidate in found:
        best.setdefault((candidate.from_table, candidate.from_column), candidate)

    return sorted(
        best.values(), key=lambda c: (c.from_table, -c.confidence, c.from_column)
    )
