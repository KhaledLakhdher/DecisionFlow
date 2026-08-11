"""Profiling, cleaning and validation.

The fixture below is deliberately awful, in the specific ways real exports are
awful: currency symbols in a revenue column, "N/A" where a number should be,
dates as text, a column that is entirely empty, a constant column, an exact
duplicate row, and padded whitespace. Each defect exists to prove one rule.
"""

from __future__ import annotations

import io
import uuid

from httpx import AsyncClient

from decisionflow.data import warehouse
from decisionflow.data.cleaning import CleaningAction, build_clean_sql, plan_cleaning
from decisionflow.data.profiling import ColumnProfile
from decisionflow.data.validation import (
    QualityIssue,
    check_column,
    check_dataset,
    quality_score,
)
from decisionflow.db.models.ingestion import ColumnType, IssueCode, IssueSeverity
from decisionflow.db.session import TenantContext, tenant_session
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import pipeline as pipeline_service
from tests.conftest import auth_header, register_account

# order_id | customer | revenue ($, with a "N/A") | signup (text dates)
# | notes (entirely empty) | country (constant) | active (yes/no)
# Row 1005 is an exact duplicate of 1004.
MESSY_CSV = b"""order_id,Customer,Revenue,Signup Date,Notes,Country,Active
1001,  Ada Lovelace  ,"$1,249.99",2026-01-15,,UK,yes
1002,Grace Hopper,"$2,520.50",2026-01-16,,UK,no
1003,Alan Turing,N/A,2026-02-01,,UK,yes
1004,Katherine Johnson,$430.25,2026-02-14,,UK,no
1004,Katherine Johnson,$430.25,2026-02-14,,UK,no
1006,Ada Lovelace,$610.00,2026-03-02,,UK,yes
"""


async def _upload_and_process(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID, content: bytes = MESSY_CSV
) -> str:
    response = await client.post(
        "/api/v1/datasets/upload",
        files={"file": ("messy.csv", io.BytesIO(content), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset"]["id"]

    async with tenant_session(TenantContext(org_id=org_id)) as session:
        await ingestion_service.ingest_dataset(session, dataset_id=uuid.UUID(dataset_id))
        await pipeline_service.clean_dataset(session, dataset_id=uuid.UUID(dataset_id))

    return dataset_id


# --------------------------------------------------------------------------
# Planning — pure, no data
# --------------------------------------------------------------------------
def test_numeric_looking_text_is_planned_for_coercion() -> None:
    profile = ColumnProfile(row_count=100, numeric_like_fraction=0.99)
    plan = plan_cleaning(
        [("revenue", ColumnType.STRING, profile)], duplicate_rows=0
    )
    column = plan.columns[0]

    assert CleaningAction.CAST_TO_NUMBER in column.actions
    assert column.resulting_type is ColumnType.DECIMAL


def test_mostly_numeric_text_is_left_alone() -> None:
    """Below the threshold, coercion would null out the minority.

    Destroying 40% of a column to tidy the other 60% is not a trade worth
    making automatically.
    """
    profile = ColumnProfile(row_count=100, numeric_like_fraction=0.6)
    plan = plan_cleaning([("mixed", ColumnType.STRING, profile)], duplicate_rows=0)

    assert CleaningAction.CAST_TO_NUMBER not in plan.columns[0].actions
    assert plan.columns[0].resulting_type is ColumnType.STRING


def test_dates_win_over_numbers() -> None:
    """A year column parses as both; it is far more useful as a date."""
    profile = ColumnProfile(
        row_count=100, date_like_fraction=0.99, numeric_like_fraction=0.99
    )
    plan = plan_cleaning([("signup", ColumnType.STRING, profile)], duplicate_rows=0)

    assert plan.columns[0].resulting_type is ColumnType.DATE


def test_non_text_columns_are_never_rewritten() -> None:
    profile = ColumnProfile(row_count=100)
    plan = plan_cleaning([("amount", ColumnType.DECIMAL, profile)], duplicate_rows=0)

    assert plan.columns[0].actions == []


def test_deduplication_is_only_planned_when_duplicates_exist() -> None:
    profile = ColumnProfile(row_count=100)
    columns = [("a", ColumnType.STRING, profile)]

    assert plan_cleaning(columns, duplicate_rows=0).deduplicate is False
    assert plan_cleaning(columns, duplicate_rows=3).deduplicate is True
    # Explicitly opting out must win even when duplicates are present.
    assert plan_cleaning(columns, duplicate_rows=3, deduplicate=False).deduplicate is False


def test_generated_sql_is_a_single_statement() -> None:
    """One pass over the data, not one statement per column."""
    profile = ColumnProfile(row_count=10, numeric_like_fraction=1.0)
    plan = plan_cleaning([("revenue", ColumnType.STRING, profile)], duplicate_rows=2)

    sql = build_clean_sql(plan, source_schema="raw", target_schema="clean", table="sales")

    assert sql.count("CREATE OR REPLACE TABLE") == 1
    assert "SELECT DISTINCT" in sql
    assert "clean.sales" in sql and "raw.sales" in sql


# --------------------------------------------------------------------------
# Validation rules — pure
# --------------------------------------------------------------------------
def test_empty_column_is_an_error_and_suppresses_other_findings() -> None:
    profile = ColumnProfile(row_count=100, null_count=100, null_fraction=1.0, is_empty=True)
    issues = check_column(column="notes", column_type=ColumnType.STRING, profile=profile)

    assert len(issues) == 1, "an empty column makes every other rule noise"
    assert issues[0].code is IssueCode.EMPTY_COLUMN
    assert issues[0].severity is IssueSeverity.ERROR


def test_high_null_rate_is_a_warning() -> None:
    profile = ColumnProfile(row_count=100, null_count=70, null_fraction=0.7)
    codes = {
        issue.code
        for issue in check_column(
            column="discount", column_type=ColumnType.DECIMAL, profile=profile
        )
    }
    assert IssueCode.HIGH_NULL_RATE in codes


def test_constant_column_is_reported() -> None:
    profile = ColumnProfile(row_count=100, distinct_count=1, is_constant=True)
    codes = {
        issue.code
        for issue in check_column(
            column="country", column_type=ColumnType.STRING, profile=profile
        )
    }
    assert IssueCode.CONSTANT_COLUMN in codes


def test_identifier_like_column_is_flagged_as_high_cardinality() -> None:
    profile = ColumnProfile(row_count=100, distinct_count=100, distinct_fraction=1.0)
    codes = {
        issue.code
        for issue in check_column(
            column="order_ref", column_type=ColumnType.STRING, profile=profile
        )
    }
    assert IssueCode.HIGH_CARDINALITY in codes


def test_duplicate_rows_are_reported_at_dataset_level() -> None:
    issues = check_dataset(row_count=100, duplicate_rows=5)
    assert issues[0].code is IssueCode.DUPLICATE_ROWS
    assert issues[0].column_name is None


def test_quality_score_penalises_errors_more_than_information() -> None:
    error = [QualityIssue(IssueCode.EMPTY_COLUMN, IssueSeverity.ERROR, "x", column_name="a")]
    info = [QualityIssue(IssueCode.CONSTANT_COLUMN, IssueSeverity.INFO, "x", column_name="a")]

    assert quality_score(error, column_count=5) < quality_score(info, column_count=5)
    assert quality_score([], column_count=5) == 100


def test_column_findings_are_diluted_by_dataset_width() -> None:
    """One bad column in fifty should not score like one bad column in three."""
    issue = [QualityIssue(IssueCode.EMPTY_COLUMN, IssueSeverity.ERROR, "x", column_name="a")]

    assert quality_score(issue, column_count=50) > quality_score(issue, column_count=3)


def test_quality_score_is_bounded() -> None:
    many = [
        QualityIssue(IssueCode.EMPTY_COLUMN, IssueSeverity.ERROR, "x", column_name=f"c{i}")
        for i in range(200)
    ]
    assert quality_score(many, column_count=1) == 0


# --------------------------------------------------------------------------
# End to end, against real data
# --------------------------------------------------------------------------
async def test_currency_text_becomes_a_number(client: AsyncClient, unique_email) -> None:
    """The headline transform: "$1,249.99" is unusable until it is a number."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)

    detail = (await client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)).json()
    revenue = next(c for c in detail["columns"] if c["normalized_name"] == "revenue")

    assert revenue["data_type"] == ColumnType.STRING.value, "raw form is text"
    assert revenue["cleaned_type"] == ColumnType.DECIMAL.value
    assert revenue["effective_type"] == ColumnType.DECIMAL.value

    rows = (
        await client.get(f"/api/v1/datasets/{dataset_id}/preview?layer=clean", headers=headers)
    ).json()["rows"]
    revenues = [row["revenue"] for row in rows]

    assert 1249.99 in revenues, "currency symbol and thousands separator stripped"
    assert None in revenues, "the 'N/A' row became NULL rather than 0"


async def test_text_dates_become_dates(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    detail = (await client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)).json()

    signup = next(c for c in detail["columns"] if c["normalized_name"] == "signup_date")
    assert signup["effective_type"] == ColumnType.DATE.value


async def test_whitespace_is_trimmed(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    rows = (
        await client.get(f"/api/v1/datasets/{dataset_id}/preview?layer=clean", headers=headers)
    ).json()["rows"]

    customers = [row["customer"] for row in rows]
    assert "Ada Lovelace" in customers, "padded value should be trimmed"
    assert "  Ada Lovelace  " not in customers


async def test_duplicate_rows_are_removed(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    detail = (await client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)).json()

    assert detail["row_count"] == 6, "raw keeps every row exactly as uploaded"
    assert detail["clean_row_count"] == 5, "the duplicated 1004 row is gone"


async def test_raw_layer_is_never_modified(client: AsyncClient, unique_email) -> None:
    """Cleaning must be reproducible, which means raw stays pristine."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    raw = (
        await client.get(f"/api/v1/datasets/{dataset_id}/preview?layer=raw", headers=headers)
    ).json()

    assert raw["total_rows"] == 6
    # Still the original text, symbols and padding intact.
    assert any(str(row["revenue"]).startswith("$") for row in raw["rows"])
    assert any(row["customer"].startswith(" ") for row in raw["rows"])


async def test_quality_report_describes_the_problems(
    client: AsyncClient, unique_email
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    report = (
        await client.get(f"/api/v1/datasets/{dataset_id}/quality", headers=headers)
    ).json()

    codes = {issue["code"] for issue in report["issues"]}
    assert IssueCode.EMPTY_COLUMN.value in codes, "the notes column is entirely empty"
    assert IssueCode.CONSTANT_COLUMN.value in codes, "country is 'UK' throughout"
    assert IssueCode.DUPLICATE_ROWS.value in codes

    assert report["rows_removed"] == 1
    assert 0 <= report["quality_score"] <= 100
    assert report["cleaning_actions"], "the audit trail must not be empty"


async def test_cleaning_actions_are_recorded_for_audit(
    client: AsyncClient, unique_email
) -> None:
    """Every change must be visible; silent mutation of customer data is not acceptable."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    detail = (await client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)).json()

    actions = detail["cleaning_actions"]
    by_column = {entry["column"]: entry for entry in actions}

    assert CleaningAction.CAST_TO_NUMBER.value in by_column["revenue"]["actions"]
    assert by_column["revenue"]["reason"], "an action without a reason is not an audit trail"
    assert CleaningAction.DEDUPLICATE_ROWS.value in by_column[None]["actions"]


async def test_profile_statistics_are_computed(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    detail = (await client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)).json()

    revenue = next(c for c in detail["columns"] if c["normalized_name"] == "revenue")
    profile = revenue["profile"]

    # Computed on the cleaned column, so these are real numbers.
    assert profile["min_value"] == 430.25
    assert profile["max_value"] == 2520.5
    assert profile["null_count"] == 1, "the 'N/A' value"
    assert profile["mean_value"] is not None


async def test_recleaning_without_dedupe_keeps_duplicates(
    client: AsyncClient, unique_email
) -> None:
    """The one default that can be genuinely wrong is user-overridable."""
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)

    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/clean",
        json={"deduplicate": False},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["clean_row_count"] == 6, "duplicates retained on request"


async def test_cleaning_is_idempotent(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)

    first = (await client.get(f"/api/v1/datasets/{dataset_id}/quality", headers=headers)).json()
    await client.post(
        f"/api/v1/datasets/{dataset_id}/clean", json={"deduplicate": True}, headers=headers
    )
    second = (await client.get(f"/api/v1/datasets/{dataset_id}/quality", headers=headers)).json()

    assert first["quality_score"] == second["quality_score"]
    assert len(first["issues"]) == len(second["issues"]), "issues replaced, not accumulated"


async def test_clean_table_is_dropped_with_the_dataset(
    client: AsyncClient, unique_email
) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)
    org_id = uuid.UUID(tokens["active_org_id"])

    dataset_id = await _upload_and_process(client, headers, org_id)
    detail = (await client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)).json()
    slug = detail["slug"]

    assert await warehouse.table_exists(org_id, warehouse.CLEAN_SCHEMA, slug)

    await client.delete(f"/api/v1/datasets/{dataset_id}", headers=headers)

    assert not await warehouse.table_exists(org_id, warehouse.CLEAN_SCHEMA, slug)
