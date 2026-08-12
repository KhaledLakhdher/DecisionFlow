"""Star view construction.

Turns a confirmed set of relationships into one wide view per fact table:
the fact joined to each of its dimensions, with dimension columns prefixed by
their table name.

The prefix is not cosmetic. `orders` and `customers` both plausibly have a
`name` or a `country`, and an unprefixed join makes one silently shadow the
other — so `customers.country` becomes `customers_country`, and a question
about "revenue by customer country" has an unambiguous column to reach for.

LEFT JOIN throughout, never INNER. A fact row whose foreign key is missing from
the dimension is still a real transaction, and dropping it would quietly reduce
reported revenue — the kind of error nobody notices until the totals are
queried twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decisionflow.data.warehouse import CLEAN_SCHEMA, validate_identifier

STAR_SCHEMA = "star"


@dataclass(slots=True)
class Join:
    """One fact-to-dimension edge."""

    dimension_table: str
    fact_column: str
    dimension_column: str
    # Dimension columns to expose, excluding the join key (already on the fact).
    columns: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StarDefinition:
    fact_table: str
    fact_columns: list[str]
    joins: list[Join] = field(default_factory=list)

    @property
    def view_name(self) -> str:
        return self.fact_table


def build_star_sql(definition: StarDefinition) -> str:
    """Compile a star definition into CREATE OR REPLACE VIEW.

    A view rather than a table: it costs nothing to keep current, and a
    materialised copy would go stale the moment either side is re-cleaned.
    """
    fact = validate_identifier(definition.fact_table)
    validate_identifier(STAR_SCHEMA)

    projection = [f"f.{validate_identifier(column)}" for column in definition.fact_columns]
    clauses: list[str] = []

    for index, join in enumerate(definition.joins):
        alias = f"d{index}"
        dimension = validate_identifier(join.dimension_table)
        fact_key = validate_identifier(join.fact_column)
        dim_key = validate_identifier(join.dimension_column)

        for column in join.columns:
            safe = validate_identifier(column)
            # Prefixed so a dimension column cannot shadow a fact column of the
            # same name — `country` vs `customers_country`.
            projection.append(f"{alias}.{safe} AS {dimension}_{safe}")

        clauses.append(
            f"LEFT JOIN {CLEAN_SCHEMA}.{dimension} {alias} "
            f"ON f.{fact_key} = {alias}.{dim_key}"
        )

    return (
        f"CREATE OR REPLACE VIEW {STAR_SCHEMA}.{fact} AS "  # noqa: S608
        f"SELECT {', '.join(projection)} "
        f"FROM {CLEAN_SCHEMA}.{fact} f "
        + " ".join(clauses)
    )


def describe(definition: StarDefinition) -> str:
    """Human-readable summary, used in the API and in LLM prompts."""
    if not definition.joins:
        return f"{definition.fact_table} (no dimensions joined)"

    parts = ", ".join(
        f"{join.dimension_table} on {join.fact_column}" for join in definition.joins
    )
    return f"{definition.fact_table} joined to {parts}"
