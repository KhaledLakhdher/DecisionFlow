"""Relationship detection and star-schema construction.

The fixture is three files that genuinely relate — orders, customers, products —
plus one trap: `orders.order_id` and `customers.customer_id` are both small
integers drawn from overlapping ranges. A detector that scores on value overlap
alone will happily join them, and the resulting many-to-many fan-out would
inflate every revenue figure in the product. That case is asserted explicitly.
"""

from __future__ import annotations

import io
import uuid

import pytest
from httpx import AsyncClient

from decisionflow.data import relationships as detection
from decisionflow.data import star
from decisionflow.data.warehouse import CLEAN_SCHEMA
from decisionflow.db.models.ingestion import TableRole
from decisionflow.db.session import TenantContext, tenant_session
from decisionflow.services import analytics as analytics_service
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import modelling as modelling_service
from decisionflow.services import pipeline as pipeline_service
from tests.conftest import auth_header, register_account

ORDERS = b"""order_id,customer_id,product_id,Order Date,Revenue,Units
1,101,5001,2026-01-05,1200.00,3
2,102,5002,2026-01-20,840.00,2
3,101,5001,2026-02-05,2150.00,6
4,103,5003,2026-02-18,430.00,1
5,104,5002,2026-03-03,1760.00,4
6,102,5001,2026-03-22,620.00,2
7,105,5003,2026-04-10,3400.00,8
8,103,5002,2026-05-06,2980.00,7
"""

CUSTOMERS = b"""customer_id,Customer Name,Country,Segment
101,Ada Lovelace,United Kingdom,Enterprise
102,Grace Hopper,United States,Enterprise
103,Alan Turing,United Kingdom,SMB
104,Katherine Johnson,United States,SMB
105,Ada Byron,Ireland,Enterprise
"""

PRODUCTS = b"""product_id,Product Name,Category
5001,Laptop Pro,Electronics
5002,Desk Chair,Furniture
5003,Monitor 4K,Electronics
"""


async def _upload_and_process(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID,
    content: bytes, filename: str,
) -> uuid.UUID:
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": (filename, io.BytesIO(content), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    dataset_id = uuid.UUID(response.json()["dataset"]["id"])

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        await ingestion_service.ingest_dataset(session, dataset_id=dataset_id)
        await pipeline_service.clean_dataset(session, dataset_id=dataset_id)
        await analytics_service.analyse_dataset(session, dataset_id=dataset_id)

    return dataset_id


async def _workspace(client: AsyncClient, unique_email) -> tuple[dict[str, str], uuid.UUID]:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    await _upload_and_process(client, headers, org_id, ORDERS, "orders.csv")
    await _upload_and_process(client, headers, org_id, CUSTOMERS, "customers.csv")
    await _upload_and_process(client, headers, org_id, PRODUCTS, "products.csv")

    return headers, org_id


# --------------------------------------------------------------------------
# Name heuristics — pure
# --------------------------------------------------------------------------
def test_identical_names_agree() -> None:
    assert detection.names_agree(
        from_column="customer_id", to_table="customers", to_column="customer_id"
    )


def test_table_named_for_the_key_agrees() -> None:
    """orders.customer_id -> customers.id is the other common convention."""
    assert detection.names_agree(
        from_column="customer_id", to_table="customers", to_column="id"
    )


def test_unrelated_names_do_not_agree() -> None:
    assert not detection.names_agree(
        from_column="product_id", to_table="customers", to_column="customer_id"
    )


# --------------------------------------------------------------------------
# Star SQL — pure
# --------------------------------------------------------------------------
def test_dimension_columns_are_prefixed() -> None:
    """Unprefixed, a dimension's `country` would shadow the fact's own."""
    definition = star.StarDefinition(
        fact_table="orders",
        fact_columns=["order_id", "revenue"],
        joins=[
            star.Join(
                dimension_table="customers",
                fact_column="customer_id",
                dimension_column="customer_id",
                columns=["country", "segment"],
            )
        ],
    )
    sql = star.build_star_sql(definition)

    assert "customers_country" in sql
    assert "customers_segment" in sql
    assert f"{CLEAN_SCHEMA}.orders f" in sql


def test_star_uses_left_join() -> None:
    """An INNER join would silently drop facts with a missing key."""
    definition = star.StarDefinition(
        fact_table="orders",
        fact_columns=["order_id"],
        joins=[
            star.Join(
                dimension_table="customers",
                fact_column="customer_id",
                dimension_column="customer_id",
                columns=["country"],
            )
        ],
    )
    sql = star.build_star_sql(definition)

    assert "LEFT JOIN" in sql
    assert "INNER JOIN" not in sql


# --------------------------------------------------------------------------
# Detection, end to end
# --------------------------------------------------------------------------
async def test_detects_the_real_foreign_keys(client: AsyncClient, unique_email) -> None:
    headers, _ = await _workspace(client, unique_email)

    response = await client.post("/api/v1/model/detect", headers=headers)
    assert response.status_code == 200, response.text

    model = (await client.get("/api/v1/model", headers=headers)).json()
    edges = {
        (rel["from_table"], rel["from_column"], rel["to_table"], rel["to_column"])
        for rel in model["relationships"]
    }

    assert ("orders", "customer_id", "customers", "customer_id") in edges
    assert ("orders", "product_id", "products", "product_id") in edges


async def test_does_not_invent_a_join_between_unrelated_keys(
    client: AsyncClient, unique_email
) -> None:
    """The trap: order_id and customer_id are both small overlapping integers.

    Joining them would fan rows out and inflate every total. Nothing may
    propose orders.order_id -> customers.customer_id.
    """
    headers, _ = await _workspace(client, unique_email)
    await client.post("/api/v1/model/detect", headers=headers)

    model = (await client.get("/api/v1/model", headers=headers)).json()
    for rel in model["relationships"]:
        assert not (
            rel["from_column"] == "order_id" and rel["to_table"] == "customers"
        ), f"spurious join proposed: {rel}"


async def test_proposals_start_unconfirmed(client: AsyncClient, unique_email) -> None:
    """Detection proposes; it never joins on its own authority."""
    headers, _ = await _workspace(client, unique_email)
    await client.post("/api/v1/model/detect", headers=headers)

    model = (await client.get("/api/v1/model", headers=headers)).json()
    assert model["relationships"]
    assert all(rel["confirmed"] is None for rel in model["relationships"])
    # And no table has been given a role yet.
    assert all(table["role"] == "unknown" for table in model["tables"])


async def test_every_proposal_explains_itself(client: AsyncClient, unique_email) -> None:
    headers, _ = await _workspace(client, unique_email)
    await client.post("/api/v1/model/detect", headers=headers)

    model = (await client.get("/api/v1/model", headers=headers)).json()
    for rel in model["relationships"]:
        assert "%" in rel["rationale"]
        assert 0.0 <= rel["confidence"] <= 1.0


async def test_detection_needs_two_datasets(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])
    await _upload_and_process(client, headers, org_id, ORDERS, "orders.csv")

    response = await client.post("/api/v1/model/detect", headers=headers)
    assert response.status_code == 422
    assert "two processed datasets" in response.json()["error"]["message"]


# --------------------------------------------------------------------------
# Confirmation and the star view
# --------------------------------------------------------------------------
async def _confirm_all(client: AsyncClient, headers: dict[str, str]) -> None:
    await client.post("/api/v1/model/detect", headers=headers)
    model = (await client.get("/api/v1/model", headers=headers)).json()

    for rel in model["relationships"]:
        response = await client.patch(
            f"/api/v1/model/relationships/{rel['id']}",
            json={"confirmed": True},
            headers=headers,
        )
        assert response.status_code == 200, response.text


async def test_confirming_assigns_fact_and_dimension_roles(
    client: AsyncClient, unique_email
) -> None:
    headers, _ = await _workspace(client, unique_email)
    await _confirm_all(client, headers)

    model = (await client.get("/api/v1/model", headers=headers)).json()
    roles = {table["table"]: table["role"] for table in model["tables"]}

    assert roles["orders"] == TableRole.FACT.value
    assert roles["customers"] == TableRole.DIMENSION.value
    assert roles["products"] == TableRole.DIMENSION.value


async def test_star_view_joins_the_dimensions(client: AsyncClient, unique_email) -> None:
    """The payoff: dimension attributes become queryable from the fact."""
    from decisionflow.data import warehouse

    headers, org_id = await _workspace(client, unique_email)
    await _confirm_all(client, headers)

    rows = await warehouse.fetch_all(
        org_id,
        f"SELECT customers_country, sum(revenue) AS revenue "
        f"FROM {star.STAR_SCHEMA}.orders "
        f"GROUP BY 1 ORDER BY 2 DESC",
    )

    by_country = {row["customers_country"]: float(row["revenue"]) for row in rows}

    # UK  = orders 1 (1200) + 3 (2150) + 4 (430) + 8 (2980) = 6760
    # US  = orders 2 (840) + 5 (1760) + 6 (620)             = 3220
    # IE  = order 7                                          = 3400
    assert by_country["United Kingdom"] == pytest.approx(6760.0)
    assert by_country["United States"] == pytest.approx(3220.0)
    assert by_country["Ireland"] == pytest.approx(3400.0)

    # And the parts must reconcile with the whole — the check that would catch
    # a join quietly dropping or duplicating rows.
    assert sum(by_country.values()) == pytest.approx(13380.0)


async def test_the_star_view_does_not_duplicate_fact_rows(
    client: AsyncClient, unique_email
) -> None:
    """The failure mode that matters: a bad join multiplies rows silently."""
    from decisionflow.data import warehouse

    headers, org_id = await _workspace(client, unique_email)
    await _confirm_all(client, headers)

    plain = await warehouse.fetch_all(
        org_id, f"SELECT count(*) AS n, sum(revenue) AS total FROM {CLEAN_SCHEMA}.orders"
    )
    joined = await warehouse.fetch_all(
        org_id, f"SELECT count(*) AS n, sum(revenue) AS total FROM {star.STAR_SCHEMA}.orders"
    )

    assert joined[0]["n"] == plain[0]["n"], "join must not add rows"
    assert float(joined[0]["total"]) == pytest.approx(float(plain[0]["total"]))


async def test_rejecting_a_relationship_removes_it_from_the_model(
    client: AsyncClient, unique_email
) -> None:
    headers, _ = await _workspace(client, unique_email)
    await client.post("/api/v1/model/detect", headers=headers)

    model = (await client.get("/api/v1/model", headers=headers)).json()
    target = model["relationships"][0]

    await client.patch(
        f"/api/v1/model/relationships/{target['id']}",
        json={"confirmed": False},
        headers=headers,
    )

    refreshed = (await client.get("/api/v1/model", headers=headers)).json()
    rejected = next(r for r in refreshed["relationships"] if r["id"] == target["id"])
    assert rejected["confirmed"] is False


async def test_re_detection_preserves_human_decisions(
    client: AsyncClient, unique_email
) -> None:
    """A second scan must not overturn what a person already decided."""
    headers, _ = await _workspace(client, unique_email)
    await client.post("/api/v1/model/detect", headers=headers)

    model = (await client.get("/api/v1/model", headers=headers)).json()
    target = model["relationships"][0]
    await client.patch(
        f"/api/v1/model/relationships/{target['id']}",
        json={"confirmed": False},
        headers=headers,
    )

    await client.post("/api/v1/model/detect", headers=headers)

    refreshed = (await client.get("/api/v1/model", headers=headers)).json()
    still_rejected = next(r for r in refreshed["relationships"] if r["id"] == target["id"])
    assert still_rejected["confirmed"] is False


async def test_the_agent_is_pointed_at_the_star_view(
    client: AsyncClient, unique_email
) -> None:
    """The join is useless if the analyst still queries the bare fact table."""
    headers, org_id = await _workspace(client, unique_email)
    await _confirm_all(client, headers)

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        datasets = await ingestion_service.list_datasets(session, org_id=org_id)
        orders = next(d for d in datasets if d.slug == "orders")
        customers = next(d for d in datasets if d.slug == "customers")

        fact_table = await modelling_service.star_table_for(
            session, org_id=org_id, dataset=orders
        )
        dimension_table = await modelling_service.star_table_for(
            session, org_id=org_id, dataset=customers
        )
        joined = await modelling_service.star_columns(
            session, org_id=org_id, dataset=orders
        )

    assert fact_table == f"{star.STAR_SCHEMA}.orders"
    # A dimension has no star view of its own; it is queried directly.
    assert dimension_table == f"{CLEAN_SCHEMA}.customers"

    # The prompt must name the joined columns, or the model cannot know it may
    # group by a country that lives in a different uploaded file.
    names = {name for name, _ in joined}
    assert "customers_country" in names
    assert "products_category" in names
    # The join key is already on the fact side; duplicating it invites
    # ambiguity in generated SQL.
    assert "customers_customer_id" not in names


async def test_the_model_is_invisible_across_workspaces(
    client: AsyncClient, unique_email
) -> None:
    headers, _ = await _workspace(client, unique_email)
    await client.post("/api/v1/model/detect", headers=headers)

    outsider = await register_account(client, unique_email())
    model = (await client.get("/api/v1/model", headers=auth_header(outsider))).json()

    assert model["tables"] == []
    assert model["relationships"] == []
