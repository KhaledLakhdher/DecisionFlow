"""Semantic classification: working out what each column *means*.

Generating KPIs without configuration requires answering a question the schema
alone cannot: is this number something to add up, or an identifier that happens
to be numeric? `order_id` and `revenue` are both integers to a database, and
summing the first is nonsense.

Two signals are combined, and the order matters:

  * **Statistics** — cardinality, null rate, type. Reliable but ambiguous:
    they distinguish a category from an identifier, but not revenue from
    quantity.
  * **Name patterns** — `revenue`, `qty`, `customer_id`. Ambiguous but
    meaningful: they carry the intent that statistics cannot see.

Statistics decide the *role* (can this be aggregated?), names decide the *tag*
(what kind of quantity is it?). Doing it the other way around means a column
called `total_orders_id` gets summed.

Everything here is a heuristic and will occasionally be wrong, which is why
roles are stored on the column and exposed through the API — a human can
correct them, and later modules read the stored value rather than re-deriving.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field

from decisionflow.data.profiling import ColumnProfile
from decisionflow.db.models.ingestion import ColumnType

# A text column with more distinct values than this is an identifier, not a
# category — too many groups to chart or reason about.
MAX_DIMENSION_CARDINALITY = 1000
# Above this share of unique values a column is a key rather than a category.
MAX_DIMENSION_FRACTION = 0.5
# ...but only once there are enough rows for the ratio to mean anything. Three
# distinct regions across six rows is a 0.5 distinct fraction and obviously a
# category; the same fraction across six thousand rows is not. Absolute
# cardinality is the reliable signal on small samples, so the ratio test is
# skipped below this many rows.
MIN_ROWS_FOR_FRACTION = 50
# Above this share of nulls a column cannot support a trustworthy aggregate.
UNUSABLE_NULL_FRACTION = 0.95


class SemanticRole(enum.StrEnum):
    """What a column can be used for in analysis."""

    MEASURE = "measure"        # numeric, meaningful to sum or average
    DIMENSION = "dimension"    # categorical, meaningful to group by
    TIME = "time"              # the time axis
    IDENTIFIER = "identifier"  # keys — countable (distinct), never summable
    IGNORED = "ignored"        # empty, constant, or otherwise useless


class SemanticTag(enum.StrEnum):
    """What kind of thing a column holds.

    Drives KPI selection: `MONETARY` is what revenue is computed from,
    `CUSTOMER_KEY` is what "unique customers" counts.
    """

    MONETARY = "monetary"
    QUANTITY = "quantity"
    COST = "cost"
    DISCOUNT = "discount"
    RATING = "rating"
    CUSTOMER_KEY = "customer_key"
    PRODUCT_KEY = "product_key"
    ORDER_KEY = "order_key"
    GEOGRAPHY = "geography"
    CATEGORY = "category"
    STATUS = "status"
    EVENT_DATE = "event_date"


# Matched against the normalised column name. Ordered: the first match wins,
# so more specific patterns must come first — `unit_cost` is a cost, not a
# monetary total, and `discount_amount` is a discount before it is money.
_NAME_PATTERNS: tuple[tuple[re.Pattern[str], SemanticTag], ...] = (
    (re.compile(r"(^|_)(discount|markdown|rebate)"), SemanticTag.DISCOUNT),
    (re.compile(r"(^|_)(cost|cogs|expense|spend)"), SemanticTag.COST),
    (re.compile(r"(^|_)(rating|score|nps|satisfaction)"), SemanticTag.RATING),
    (
        re.compile(
            r"(^|_)(revenue|sales|amount|total|price|subtotal|gross|net|"
            r"turnover|value|payment|charge)"
        ),
        SemanticTag.MONETARY,
    ),
    (re.compile(r"(^|_)(qty|quantity|units|count|volume|items)"), SemanticTag.QUANTITY),
    (re.compile(r"(^|_)(customer|client|account|user|buyer|member)"), SemanticTag.CUSTOMER_KEY),
    (re.compile(r"(^|_)(product|sku|item|article)"), SemanticTag.PRODUCT_KEY),
    (re.compile(r"(^|_)(order|invoice|transaction|purchase|booking)"), SemanticTag.ORDER_KEY),
    (
        re.compile(r"(^|_)(country|region|city|state|province|territory|zone|market|store)"),
        SemanticTag.GEOGRAPHY,
    ),
    (re.compile(r"(^|_)(category|segment|type|group|class|channel|brand)"), SemanticTag.CATEGORY),
    (re.compile(r"(^|_)(status|state|stage|phase)"), SemanticTag.STATUS),
    (re.compile(r"(^|_)(date|time|day|month|year|created|updated|timestamp)"),
     SemanticTag.EVENT_DATE),
)

# Names that mean "this is a key" regardless of anything else.
_KEY_SUFFIX = re.compile(r"(^|_)(id|key|code|ref|number|no)$")


@dataclass(slots=True)
class ColumnSemantics:
    column: str
    role: SemanticRole
    tags: list[SemanticTag] = field(default_factory=list)
    # Why this classification was chosen — surfaced in the API so a wrong guess
    # is diagnosable rather than mysterious.
    rationale: str = ""

    @property
    def tag_values(self) -> list[str]:
        return [tag.value for tag in self.tags]

    def has(self, tag: SemanticTag) -> bool:
        return tag in self.tags


def _match_tags(column: str) -> list[SemanticTag]:
    for pattern, tag in _NAME_PATTERNS:
        if pattern.search(column):
            return [tag]
    return []


def classify_column(
    *, column: str, column_type: ColumnType, profile: ColumnProfile
) -> ColumnSemantics:
    """Assign a role and tags to one column."""
    tags = _match_tags(column)
    looks_like_key = bool(_KEY_SUFFIX.search(column))

    # --- unusable ---------------------------------------------------------
    if profile.is_empty or profile.null_fraction >= UNUSABLE_NULL_FRACTION:
        return ColumnSemantics(
            column=column,
            role=SemanticRole.IGNORED,
            tags=tags,
            rationale="Column is empty or almost entirely null.",
        )
    if profile.is_constant:
        return ColumnSemantics(
            column=column,
            role=SemanticRole.IGNORED,
            tags=tags,
            rationale="Column holds a single value, so it cannot explain variation.",
        )

    # --- time -------------------------------------------------------------
    if column_type.is_temporal:
        return ColumnSemantics(
            column=column,
            role=SemanticRole.TIME,
            tags=[SemanticTag.EVENT_DATE],
            rationale="Temporal column, usable as the time axis.",
        )

    # --- numeric ----------------------------------------------------------
    if column_type.is_numeric:
        # A numeric column named like a key is a key. This is the case that
        # matters most: summing order_id produces a confident, meaningless
        # number, and nothing downstream would catch it.
        if looks_like_key or any(
            tag in (SemanticTag.CUSTOMER_KEY, SemanticTag.PRODUCT_KEY, SemanticTag.ORDER_KEY)
            for tag in tags
        ):
            return ColumnSemantics(
                column=column,
                role=SemanticRole.IDENTIFIER,
                tags=tags,
                rationale="Numeric, but named as an identifier — counted, never summed.",
            )

        # Integers that are almost entirely unique are keys even without a
        # telling name: a genuine measure repeats values.
        if (
            column_type is ColumnType.INTEGER
            and profile.distinct_fraction >= 0.99
            and profile.row_count >= MIN_ROWS_FOR_FRACTION
        ):
            return ColumnSemantics(
                column=column,
                role=SemanticRole.IDENTIFIER,
                tags=tags,
                rationale="Integer column with near-unique values, characteristic of a key.",
            )

        return ColumnSemantics(
            column=column,
            role=SemanticRole.MEASURE,
            tags=tags or [SemanticTag.QUANTITY],
            rationale="Numeric column suitable for aggregation.",
        )

    # --- boolean ----------------------------------------------------------
    if column_type is ColumnType.BOOLEAN:
        return ColumnSemantics(
            column=column,
            role=SemanticRole.DIMENSION,
            tags=tags or [SemanticTag.STATUS],
            rationale="Boolean flag, usable as a two-valued grouping.",
        )

    # --- text -------------------------------------------------------------
    if looks_like_key:
        return ColumnSemantics(
            column=column,
            role=SemanticRole.IDENTIFIER,
            tags=tags,
            rationale="Named as an identifier.",
        )

    if profile.distinct_count > MAX_DIMENSION_CARDINALITY:
        return ColumnSemantics(
            column=column,
            role=SemanticRole.IDENTIFIER,
            tags=tags,
            rationale=(
                f"Text column with {profile.distinct_count} distinct values — "
                "too many to group by usefully."
            ),
        )

    if (
        profile.row_count >= MIN_ROWS_FOR_FRACTION
        and profile.distinct_fraction >= MAX_DIMENSION_FRACTION
    ):
        return ColumnSemantics(
            column=column,
            role=SemanticRole.IDENTIFIER,
            tags=tags,
            rationale="Text column of mostly unique values, characteristic of a key.",
        )

    return ColumnSemantics(
        column=column,
        role=SemanticRole.DIMENSION,
        tags=tags or [SemanticTag.CATEGORY],
        rationale=f"Text column with {profile.distinct_count} distinct values.",
    )


@dataclass(slots=True)
class DatasetSemantics:
    """The classified shape of a dataset, and the shortcuts KPIs need."""

    columns: list[ColumnSemantics]

    def by_role(self, role: SemanticRole) -> list[ColumnSemantics]:
        return [column for column in self.columns if column.role is role]

    def first_with_tag(
        self, tag: SemanticTag, *, role: SemanticRole | None = None
    ) -> ColumnSemantics | None:
        for column in self.columns:
            if column.has(tag) and (role is None or column.role is role):
                return column
        return None

    @property
    def time_column(self) -> ColumnSemantics | None:
        """The time axis.

        The earliest temporal column wins when there are several, on the
        assumption that the event date precedes bookkeeping columns like
        `updated_at` — which describe the record, not the business event.
        """
        temporal = self.by_role(SemanticRole.TIME)
        return temporal[0] if temporal else None

    @property
    def revenue_column(self) -> ColumnSemantics | None:
        """The measure that means money.

        Cost and discount columns are also monetary in the everyday sense but
        are tagged separately, so they cannot be mistaken for revenue.
        """
        return self.first_with_tag(SemanticTag.MONETARY, role=SemanticRole.MEASURE)

    @property
    def quantity_column(self) -> ColumnSemantics | None:
        return self.first_with_tag(SemanticTag.QUANTITY, role=SemanticRole.MEASURE)

    @property
    def cost_column(self) -> ColumnSemantics | None:
        return self.first_with_tag(SemanticTag.COST, role=SemanticRole.MEASURE)

    @property
    def customer_key(self) -> ColumnSemantics | None:
        return self.first_with_tag(SemanticTag.CUSTOMER_KEY)

    @property
    def order_key(self) -> ColumnSemantics | None:
        return self.first_with_tag(SemanticTag.ORDER_KEY)

    @property
    def product_key(self) -> ColumnSemantics | None:
        return self.first_with_tag(SemanticTag.PRODUCT_KEY)

    @property
    def measures(self) -> list[ColumnSemantics]:
        return self.by_role(SemanticRole.MEASURE)

    @property
    def dimensions(self) -> list[ColumnSemantics]:
        return self.by_role(SemanticRole.DIMENSION)


def classify_dataset(
    columns: list[tuple[str, ColumnType, ColumnProfile]],
) -> DatasetSemantics:
    return DatasetSemantics(
        columns=[
            classify_column(column=name, column_type=column_type, profile=profile)
            for name, column_type, profile in columns
        ]
    )
