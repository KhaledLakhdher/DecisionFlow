"""Running the fleet: dispatch, parallelism, and partial failure.

Specialists are independent, so they run concurrently — a briefing that ran
five specialists in series would take five times as long for no reason. They
each open a short-lived read connection to the workspace, which DuckDB permits
concurrently under the shared advisory lock.

Every specialist is wrapped. One that refuses, errors, or times out produces a
recorded finding rather than taking the run down with it: a briefing that says
"revenue is up 12%, and there was not enough history to forecast" is useful,
and one that returns a 500 because the forecaster declined is not.

Headlines here are built by string formatting over computed values, never by a
model. They are the sentences the synthesiser is allowed to draw on, so they
have to be true by construction.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from decisionflow.core.errors import DecisionFlowError, NotFoundError, ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.db.models.ingestion import (
    BriefingFinding,
    BriefingRun,
    Dataset,
    DatasetColumn,
    DatasetStatus,
)
from decisionflow.llm import agent
from decisionflow.llm import fleet as fleet_llm
from decisionflow.llm.fleet import Finding, FindingStatus, Specialist
from decisionflow.services import analytics as analytics_service
from decisionflow.services import ingestion as ingestion_service
from decisionflow.services import investigation as investigation_service
from decisionflow.services import modelling as modelling_service
from decisionflow.services import predictions as predictions_service

log = get_logger(__name__)

MAX_QUESTION_LENGTH = 1000


def _fmt(value: float | None) -> str:
    """A number a business reader can take in at a glance."""
    if value is None:
        return "unknown"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.1f}M"
    if abs(value) >= 1_000:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{abs(value) * 100:.0f}%"


def _signed(value: float | None) -> str:
    """A delta with its direction attached, so a fall reads as a fall."""
    if value is None:
        return "unknown"
    # U+2212 MINUS SIGN rather than a hyphen: it matches the width of the "+"
    # it alternates with, so a column of deltas does not visibly wobble. Ruff
    # flags it as an ambiguous character, which is the right default for
    # identifiers and the wrong one for rendered prose.
    return f"{'+' if value >= 0 else '−'}{_fmt(abs(value))}"  # noqa: RUF001


# --------------------------------------------------------------------------
# Which specialists this dataset can support
# --------------------------------------------------------------------------
def available_specialists(
    columns: list[DatasetColumn], *, has_question: bool
) -> list[Specialist]:
    """The roster this dataset's shape can actually serve.

    Filtering here rather than letting specialists refuse at run time keeps the
    planner honest: a model offered a churn specialist will find a reason to
    use it, and then the brief is built around a refusal.
    """
    capability = predictions_service.available_predictions(columns)
    semantics = analytics_service.load_semantics(columns)

    available: list[Specialist] = []
    # The analyst answers a specific question. With nothing asked there is
    # nothing for it to query.
    if has_question:
        available.append(Specialist.ANALYST)
    if capability["forecast"] and capability["dimensions"]:
        available.append(Specialist.INVESTIGATOR)
    if capability["anomalies"]:
        available.append(Specialist.SENTINEL)
    if capability["forecast"]:
        available.append(Specialist.FORECASTER)
    if capability["churn"]:
        available.append(Specialist.RETENTION)

    log.debug(
        "fleet.available",
        specialists=[item.value for item in available],
        dimensions=len(semantics.dimensions),
    )
    return available


# --------------------------------------------------------------------------
# Specialists
# --------------------------------------------------------------------------
async def _run_analyst(context: dict[str, Any]) -> Finding:
    dataset: Dataset = context["dataset"]
    answer = await agent.ask(
        question=context["question"],
        dataset=dataset,
        columns=context["columns"],
        semantics=context["semantics"],
        table=context["table"],
        extra_columns=context["extra_columns"],
    )
    return Finding(
        specialist=Specialist.ANALYST,
        status=FindingStatus.OK if answer.answerable else FindingStatus.REFUSED,
        headline=answer.answer,
        detail={"rows": answer.rows[:20], "row_count": answer.row_count},
        sql=answer.sql,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
    )


async def _run_investigator(context: dict[str, Any]) -> Finding:
    result = await investigation_service.diagnose(
        dataset=context["dataset"], columns=context["columns"]
    )
    measure = result["measure"]
    direction = result["direction"]
    movement = (
        f"{direction} {_pct(result['change_ratio'])}"
        if result["change_ratio"] is not None
        else f"moved by {_fmt(result['change'])}"
    )
    window = f"between {result['previous_period']} and {result['current_period']}"

    top = (result["dimensions"] or [{}])[0]
    segments = {item["segment"]: item for item in top.get("segments", [])}
    riser = segments.get(top.get("riser"))
    faller = segments.get(top.get("faller"))

    if top.get("offsetting") and riser and faller:
        # Share-of-net is nonsense here — a segment can be 1000% of a small net
        # change. The absolute movements are what actually happened, and the
        # net figure alone would tell the reader nothing moved.
        headline = (
            f"{measure} {movement} {window}, but that net figure hides large "
            f"offsetting moves: {top['dimension']} {riser['segment']} "
            f"{_signed(riser['delta'])} ({_fmt(riser['previous'])} → "
            f"{_fmt(riser['current'])}) against {faller['segment']} "
            f"{_signed(faller['delta'])} ({_fmt(faller['previous'])} → "
            f"{_fmt(faller['current'])})."
        )
    elif result["concentrated"] and top.get("segments"):
        leader = top["segments"][0]
        headline = (
            f"{measure} {movement} {window}. "
            f"{_pct(leader.get('share'))} of the change came from "
            f"{top['dimension']} = {leader['segment']} "
            f"({_fmt(leader['previous'])} → {_fmt(leader['current'])})."
        )
    else:
        # The honest finding. Naming a segment here would be inventing a cause.
        headline = (
            f"{measure} {movement} {window}, but the change is broad-based — "
            f"no single {top.get('dimension', 'segment')} accounts for it."
        )

    return Finding(
        specialist=Specialist.INVESTIGATOR,
        status=FindingStatus.OK,
        headline=headline,
        detail=result,
        sql=top.get("sql"),
        method="contribution analysis",
    )


async def _run_sentinel(context: dict[str, Any]) -> Finding:
    result = await predictions_service.detect_anomalies(
        context["session"], dataset=context["dataset"], columns=context["columns"]
    )
    found = result["anomalies"]
    examined = result["periods_examined"]

    if not found:
        headline = f"No unusual periods across the {examined} examined."
    else:
        worst = max(found, key=lambda item: abs(item.get("deviation_ratio") or 0))
        headline = (
            f"{len(found)} unusual "
            f"{'period' if len(found) == 1 else 'periods'} in {examined} examined. "
            f"The largest was {worst['period']} at {_fmt(worst['value'])} "
            f"against an expected {_fmt(worst['expected'])}."
        )

    return Finding(
        specialist=Specialist.SENTINEL,
        status=FindingStatus.OK,
        headline=headline,
        detail=result,
        method="median absolute deviation",
    )


async def _run_forecaster(context: dict[str, Any]) -> Finding:
    result = await predictions_service.forecast(
        context["session"], dataset=context["dataset"], columns=context["columns"]
    )
    points = result["points"]
    first = points[0] if points else None
    headline = (
        f"{result['measure']} is projected at {_fmt(first['value'])} "
        f"for {first['period']} "
        f"(range {_fmt(first['lower'])} to {_fmt(first['upper'])}), "
        f"from {result['history_points']} periods of history."
        if first
        else "No projection could be produced."
    )
    return Finding(
        specialist=Specialist.FORECASTER,
        status=FindingStatus.OK if first else FindingStatus.REFUSED,
        headline=headline,
        detail=result,
        method=result["method"],
    )


async def _run_retention(context: dict[str, Any]) -> Finding:
    result = await predictions_service.churn(
        context["session"], dataset=context["dataset"], columns=context["columns"]
    )
    drivers = result["drivers"]
    driver = (
        f" The strongest signal is {drivers[0]['label']} ({drivers[0]['direction']})."
        if drivers
        else ""
    )
    headline = (
        f"{result['lapsed']} of {result['customers']} customers have lapsed "
        f"({_pct(result['lapsed_rate'])}), by the definition: "
        f"{result['definition']}.{driver}"
    )
    return Finding(
        specialist=Specialist.RETENTION,
        status=FindingStatus.OK,
        headline=headline,
        detail=result,
        method="logistic regression on RFM features",
    )


RUNNERS = {
    Specialist.ANALYST: _run_analyst,
    Specialist.INVESTIGATOR: _run_investigator,
    Specialist.SENTINEL: _run_sentinel,
    Specialist.FORECASTER: _run_forecaster,
    Specialist.RETENTION: _run_retention,
}


async def _dispatch(specialist: Specialist, context: dict[str, Any]) -> Finding:
    """Run one specialist, converting any failure into a recorded finding."""
    started = time.perf_counter()
    try:
        finding = await RUNNERS[specialist](context)
    except ValidationError as exc:
        # The specialist declined for a stated reason — too little history, no
        # customer key. That is a legitimate outcome and belongs in the brief.
        finding = Finding(
            specialist=specialist,
            status=FindingStatus.REFUSED,
            headline=str(exc.message if isinstance(exc, DecisionFlowError) else exc),
        )
    except Exception as exc:
        log.warning("fleet.specialist_failed", specialist=specialist.value, error=str(exc)[:200])
        finding = Finding(
            specialist=specialist,
            status=FindingStatus.FAILED,
            headline=f"The {specialist.value} could not complete: {exc}",
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    log.info(
        "fleet.specialist_done",
        specialist=specialist.value,
        status=finding.status.value,
        ms=elapsed,
    )
    return finding


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
async def run_briefing(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dataset_id: uuid.UUID,
    question: str | None = None,
) -> BriefingRun:
    """Plan, dispatch, synthesise, and record.

    With a question the planner chooses specialists. Without one this is a
    standing briefing, and every applicable specialist runs — there is no
    question to route on, and "how is the business doing?" is genuinely a
    request for all of it.
    """
    if question is not None:
        question = question.strip()
        if not question:
            question = None
        elif len(question) > MAX_QUESTION_LENGTH:
            raise ValidationError(
                f"Questions are limited to {MAX_QUESTION_LENGTH} characters."
            )

    started = time.perf_counter()
    dataset = await ingestion_service.get_dataset(
        session, org_id=org_id, dataset_id=dataset_id
    )
    if dataset.status is not DatasetStatus.READY or dataset.cleaned_at is None:
        raise ValidationError(
            "This dataset is not ready yet. Wait for ingestion and cleaning to finish."
        )

    columns = list(dataset.columns)
    available = available_specialists(columns, has_question=question is not None)
    if not available:
        raise ValidationError(
            "This dataset's shape does not support any analysis yet. It needs at "
            "least a date column or a groupable dimension."
        )

    plan_tokens: tuple[int | None, int | None]
    if question is None:
        chosen = available[: fleet_llm.MAX_SPECIALISTS]
        reasoning = "Standing briefing: every specialist this dataset supports."
        # No planning call was made, so there is nothing to charge for it.
        plan_tokens = (None, None)
    else:
        plan = await fleet_llm.plan(question=question, available=available)
        chosen = plan.specialists
        reasoning = plan.reasoning
        plan_tokens = (plan.input_tokens, plan.output_tokens)

    context: dict[str, Any] = {
        "session": session,
        "dataset": dataset,
        "columns": columns,
        "question": question,
        "semantics": analytics_service.load_semantics(columns),
        "table": await modelling_service.star_table_for(
            session, org_id=org_id, dataset=dataset
        ),
        "extra_columns": await modelling_service.star_columns(
            session, org_id=org_id, dataset=dataset
        ),
    }

    # Concurrent: each specialist is independent, and the slowest determines
    # the wait rather than the sum.
    findings = await asyncio.gather(
        *(_dispatch(specialist, context) for specialist in chosen)
    )

    brief = await fleet_llm.synthesise(
        dataset=dataset, findings=list(findings), question=question
    )

    input_tokens = sum(
        value
        for value in (
            plan_tokens[0],
            brief.input_tokens,
            *(finding.input_tokens for finding in findings),
        )
        if value
    )
    output_tokens = sum(
        value
        for value in (
            plan_tokens[1],
            brief.output_tokens,
            *(finding.output_tokens for finding in findings),
        )
        if value
    )
    duration_ms = int((time.perf_counter() - started) * 1000)

    run = BriefingRun(
        org_id=org_id,
        dataset_id=dataset.id,
        question=question,
        headline=brief.headline,
        narrative=brief.narrative,
        recommendations=brief.recommendations,
        plan_reasoning=reasoning,
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
        duration_ms=duration_ms,
        created_by_id=user_id,
    )
    session.add(run)
    await session.flush()

    session.add_all(
        [
            BriefingFinding(
                org_id=org_id,
                run_id=run.id,
                specialist=finding.specialist.value,
                status=finding.status.value,
                headline=finding.headline,
                detail=finding.detail,
                sql=finding.sql,
                method=finding.method,
            )
            for finding in findings
        ]
    )
    await session.commit()

    log.info(
        "fleet.briefing",
        dataset_id=str(dataset.id),
        specialists=[finding.specialist.value for finding in findings],
        ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return await get_briefing(session, org_id=org_id, run_id=run.id)


async def get_briefing(
    session: AsyncSession, *, org_id: uuid.UUID, run_id: uuid.UUID
) -> BriefingRun:
    run = await session.scalar(
        select(BriefingRun)
        .where(BriefingRun.id == run_id, BriefingRun.org_id == org_id)
        .options(selectinload(BriefingRun.findings))
    )
    if run is None:
        raise NotFoundError("Briefing not found.")
    return run


async def list_briefings(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID, limit: int = 20
) -> list[BriefingRun]:
    result = await session.scalars(
        select(BriefingRun)
        .where(BriefingRun.org_id == org_id, BriefingRun.dataset_id == dataset_id)
        .order_by(BriefingRun.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .options(selectinload(BriefingRun.findings))
    )
    return list(result)
