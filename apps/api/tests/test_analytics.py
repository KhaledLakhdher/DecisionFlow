"""Semantic classification and automatic KPIs.

The fixture is a small but complete sales export: an order key, a customer
key, a monetary measure, a quantity, a cost, a date, and two dimensions. That
shape is what lets the engine derive AOV, margin and repeat rate without being
told anything.
"""

from __future__ import annotations

import io
import uuid

from httpx import AsyncClient

from decisionflow.data.metrics import build_kpi_specs
from decisionflow.data.profiling import ColumnProfile
from decisionflow.data.semantics import (
    SemanticRole,
    SemanticTag,
    classify_column,
    classify_dataset,
)
from decisionflow.db.models.ingestion import ColumnType
from decisionflow.db.session import TenantContext, tenant_session
from decisionflow.services import analytics as analytics_service
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import pipeline as pipeline_service
from tests.conftest import auth_header, register_account

SALES_CSV = b"""order_id,customer_id,Order Date,Revenue,Units,Unit Cost,Region,Category
1001,C-001,2026-01-15,1200.00,3,600.00,North,Electronics
1002,C-002,2026-01-20,800.00,2,400.00,South,Furniture
1003,C-001,2026-02-05,1500.00,5,700.00,North,Electronics
1004,C-003,2026-02-18,400.00,1,250.00,East,Furniture
1005,C-002,2026-03-02,2000.00,4,900.00,South,Electronics
1006,C-004,2026-03-15,600.00,2,300.00,North,Furniture
"""


async def _prepare(client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID) -> str:
    """Upload, ingest, clean and analyse — the whole pipeline, synchronously."""
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("sales.csv", io.BytesIO(SALES_CSV), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    dataset_id = uuid.UUID(response.json()["dataset"]["id"])

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        await ingestion_service.ingest_dataset(session, dataset_id=dataset_id)
        await pipeline_service.clean_dataset(session, dataset_id=dataset_id)
        await analytics_service.analyse_dataset(session, dataset_id=dataset_id)

    return str(dataset_id)


# --------------------------------------------------------------------------
# Classification — pure
# --------------------------------------------------------------------------
def test_numeric_id_is_an_identifier_not_a_measure() -> None:
    """The case that matters most: summing order_id is confident nonsense."""
    profile = ColumnProfile(row_count=100, distinct_count=100, distinct_fraction=1.0)
    result = classify_column(
        column="order_id", column_type=ColumnType.INTEGER, profile=profile
    )

    assert result.role is SemanticRole.IDENTIFIER
    assert result.has(SemanticTag.ORDER_KEY)


def test_unnamed_unique_integer_is_still_an_identifier() -> None:
    """Near-unique integers are keys even when the name gives nothing away."""
    profile = ColumnProfile(row_count=100, distinct_count=100, distinct_fraction=1.0)
    result = classify_column(column="reference", column_type=ColumnType.INTEGER, profile=profile)

    assert result.role is SemanticRole.IDENTIFIER


def test_revenue_is_a_monetary_measure() -> None:
    profile = ColumnProfile(row_count=100, distinct_count=80, distinct_fraction=0.8)
    result = classify_column(column="revenue", column_type=ColumnType.DECIMAL, profile=profile)

    assert result.role is SemanticRole.MEASURE
    assert result.has(SemanticTag.MONETARY)


def test_cost_is_tagged_apart_from_revenue() -> None:
    """Both are money; conflating them would report cost as income."""
    profile = ColumnProfile(row_count=100, distinct_count=50, distinct_fraction=0.5)
    result = classify_column(column="unit_cost", column_type=ColumnType.DECIMAL, profile=profile)

    assert result.has(SemanticTag.COST)
    assert not result.has(SemanticTag.MONETARY)


def test_low_cardinality_text_is_a_dimension() -> None:
    profile = ColumnProfile(row_count=100, distinct_count=4, distinct_fraction=0.04)
    result = classify_column(column="region", column_type=ColumnType.STRING, profile=profile)

    assert result.role is SemanticRole.DIMENSION
    assert result.has(SemanticTag.GEOGRAPHY)


def test_high_cardinality_text_is_an_identifier() -> None:
    """Grouping by a near-unique column yields one row per record."""
    profile = ColumnProfile(row_count=100, distinct_count=98, distinct_fraction=0.98)
    result = classify_column(column="notes", column_type=ColumnType.STRING, profile=profile)

    assert result.role is SemanticRole.IDENTIFIER


def test_small_samples_are_judged_on_absolute_cardinality() -> None:
    """Three regions across six rows is a category, not a key.

    The distinct-fraction test is meaningless at this size — it reads 0.5,
    identical to a genuinely unique column in a larger table.
    """
    profile = ColumnProfile(row_count=6, distinct_count=3, distinct_fraction=0.5)
    result = classify_column(column="region", column_type=ColumnType.STRING, profile=profile)

    assert result.role is SemanticRole.DIMENSION


def test_dates_become_the_time_axis() -> None:
    profile = ColumnProfile(row_count=100, distinct_count=30, distinct_fraction=0.3)
    result = classify_column(column="order_date", column_type=ColumnType.DATE, profile=profile)

    assert result.role is SemanticRole.TIME


def test_empty_and_constant_columns_are_ignored() -> None:
    empty = classify_column(
        column="notes",
        column_type=ColumnType.STRING,
        profile=ColumnProfile(row_count=10, null_count=10, null_fraction=1.0, is_empty=True),
    )
    constant = classify_column(
        column="country",
        column_type=ColumnType.STRING,
        profile=ColumnProfile(row_count=10, distinct_count=1, is_constant=True),
    )

    assert empty.role is SemanticRole.IGNORED
    assert constant.role is SemanticRole.IGNORED


def test_dataset_level_shortcuts_resolve() -> None:
    semantics = classify_dataset(
        [
            ("order_id", ColumnType.INTEGER, ColumnProfile(row_count=10, distinct_fraction=1.0)),
            ("customer_id", ColumnType.STRING, ColumnProfile(row_count=10, distinct_fraction=0.9)),
            ("revenue", ColumnType.DECIMAL, ColumnProfile(row_count=10, distinct_fraction=0.8)),
            ("order_date", ColumnType.DATE, ColumnProfile(row_count=10, distinct_fraction=0.5)),
            ("region", ColumnType.STRING, ColumnProfile(row_count=10, distinct_count=3,
                                                        distinct_fraction=0.3)),
        ]
    )

    assert semantics.revenue_column is not None
    assert semantics.revenue_column.column == "revenue"
    assert semantics.order_key is not None and semantics.order_key.column == "order_id"
    assert semantics.customer_key is not None
    assert semantics.time_column is not None and semantics.time_column.column == "order_date"
    assert [d.column for d in semantics.dimensions] == ["region"]


# --------------------------------------------------------------------------
# KPI selection — pure
# --------------------------------------------------------------------------
def test_kpis_are_only_emitted_when_their_columns_exist() -> None:
    """A metric invented from absent columns is a confident wrong number."""
    bare = classify_dataset(
        [("note", ColumnType.STRING, ColumnProfile(row_count=10, distinct_count=5,
                                                   distinct_fraction=0.5))]
    )
    keys = {spec.key for spec in build_kpi_specs(bare, schema="clean", table="t")}

    assert keys == {"row_count"}, "no revenue, no customer, no order -> nothing to compute"


def test_average_order_value_requires_both_halves() -> None:
    revenue_only = classify_dataset(
        [("revenue", ColumnType.DECIMAL, ColumnProfile(row_count=10, distinct_fraction=0.8))]
    )
    keys = {spec.key for spec in build_kpi_specs(revenue_only, schema="clean", table="t")}

    assert "total_revenue" in keys
    assert "average_order_value" not in keys, "AOV needs an order key as well"


def test_every_kpi_carries_its_sql() -> None:
    semantics = classify_dataset(
        [
            ("revenue", ColumnType.DECIMAL, ColumnProfile(row_count=10, distinct_fraction=0.8)),
            ("order_id", ColumnType.INTEGER, ColumnProfile(row_count=10, distinct_fraction=1.0)),
        ]
    )
    for spec in build_kpi_specs(semantics, schema="clean", table="sales"):
        assert spec.sql.strip().upper().startswith(("SELECT", "WITH"))
        assert "clean.sales" in spec.sql


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
async def test_semantics_endpoint_describes_the_dataset(
    client: AsyncClient, unique_email
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    body = (await client.get(f"/api/v1/datasets/{dataset_id}/semantics", headers=headers)).json()

    assert body["revenue_column"] == "revenue"
    assert body["time_column"] == "order_date"
    assert body["customer_key"] == "customer_id"
    assert set(body["dimensions"]) >= {"region", "category"}
    # order_id is numeric but must never be treated as something to sum.
    assert "order_id" in body["identifiers"]
    assert "order_id" not in body["measures"]


async def test_kpis_are_computed_correctly(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    kpis = (await client.get(f"/api/v1/datasets/{dataset_id}/kpis", headers=headers)).json()
    by_key = {kpi["key"]: kpi for kpi in kpis}

    assert by_key["row_count"]["value"] == 6
    assert by_key["total_revenue"]["value"] == 6500.0
    assert by_key["total_units"]["value"] == 17
    assert by_key["unique_customers"]["value"] == 4
    assert by_key["order_count"]["value"] == 6

    # 6500 / 6 orders
    assert round(by_key["average_order_value"]["value"], 2) == 1083.33
    # 6500 / 4 customers
    assert by_key["revenue_per_customer"]["value"] == 1625.0
    # C-001 and C-002 appear twice, of four customers
    assert by_key["repeat_customer_rate"]["value"] == 0.5
    # (6500 - 3150) / 6500
    assert round(by_key["gross_margin"]["value"], 4) == 0.5154


async def test_kpis_expose_their_sql(client: AsyncClient, unique_email) -> None:
    """A number the user distrusts must be checkable."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    kpis = (await client.get(f"/api/v1/datasets/{dataset_id}/kpis", headers=headers)).json()

    revenue = next(kpi for kpi in kpis if kpi["key"] == "total_revenue")
    assert "sum(revenue)" in revenue["sql"]
    assert revenue["format"] == "currency"
    assert revenue["details"]["depends_on"] == ["revenue"]


async def test_revenue_kpi_carries_period_growth(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    kpis = (await client.get(f"/api/v1/datasets/{dataset_id}/kpis", headers=headers)).json()

    growth = next(kpi for kpi in kpis if kpi["key"] == "total_revenue")["details"]["growth"]
    # March 2600 vs February 1900 -> +36.8%
    assert growth["value"] == 2600.0
    assert growth["previous"] == 1900.0
    assert round(growth["change"], 4) == 0.3684


async def test_timeseries_groups_by_month(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    body = (
        await client.get(
            f"/api/v1/datasets/{dataset_id}/timeseries?grain=month", headers=headers
        )
    ).json()

    assert body["measure"] == "revenue"
    assert body["time_column"] == "order_date"
    assert [point["value"] for point in body["points"]] == [2000.0, 1900.0, 2600.0]


async def test_breakdown_ranks_dimension_values(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    body = (
        await client.get(
            f"/api/v1/datasets/{dataset_id}/breakdown?dimension=region", headers=headers
        )
    ).json()

    assert body["dimension"] == "region"
    assert body["items"][0] == {"label": "North", "value": 3300.0}
    assert [item["label"] for item in body["items"]] == ["North", "South", "East"]


async def test_breakdown_rejects_a_non_dimension(client: AsyncClient, unique_email) -> None:
    """Grouping by an identifier yields one row per record — refuse it."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    response = await client.get(
        f"/api/v1/datasets/{dataset_id}/breakdown?dimension=order_id", headers=headers
    )

    assert response.status_code == 422


async def test_breakdown_defaults_to_a_usable_dimension(
    client: AsyncClient, unique_email
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    body = (
        await client.get(f"/api/v1/datasets/{dataset_id}/breakdown", headers=headers)
    ).json()

    assert body["dimension"] in body["available_dimensions"]
    assert body["items"]


async def test_analysis_is_repeatable(client: AsyncClient, unique_email) -> None:
    """Re-analysing replaces KPIs rather than accumulating duplicates."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _prepare(client, headers, org_id)
    first = (await client.get(f"/api/v1/datasets/{dataset_id}/kpis", headers=headers)).json()

    again = await client.post(f"/api/v1/datasets/{dataset_id}/analyse", headers=headers)
    assert again.status_code == 200

    second = (await client.get(f"/api/v1/datasets/{dataset_id}/kpis", headers=headers)).json()
    assert len(first) == len(second)
    assert {k["key"] for k in first} == {k["key"] for k in second}


async def test_kpis_are_invisible_across_workspaces(
    client: AsyncClient, unique_email
) -> None:
    tokens = await register_account(client, unique_email())
    org_id = uuid.UUID(tokens["active_org_id"])
    dataset_id = await _prepare(client, auth_header(tokens), org_id)

    outsider = await register_account(client, unique_email())
    response = await client.get(
        f"/api/v1/datasets/{dataset_id}/kpis", headers=auth_header(outsider)
    )
    assert response.status_code == 404
