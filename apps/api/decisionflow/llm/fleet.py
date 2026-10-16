"""The agent fleet: an executive that routes, specialists that report.

    question → plan → specialists (in parallel) → brief

The division of labour is the whole design. Specialists compute — SQL,
regression, decomposition — and every number they return is derived from the
data. The executive only decides *who to ask* and, at the end, *how to say
it*. No figure in a brief originates with a language model.

That constraint is what makes the fleet safe to put in front of an owner who
will act on what it says. A model asked to both analyse and narrate will fill
gaps in the analysis with fluent prose, and the reader cannot tell which
sentences are grounded.

This module holds the roster metadata and the two model calls. What each
specialist actually *does* lives in `services.fleet`, because specialists call
services and the LLM layer must not depend on them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from decisionflow.core.logging import get_logger
from decisionflow.llm import prompts, provider

log = get_logger(__name__)

# A hard ceiling on a single run regardless of what the planner asks for.
# Each specialist is at minimum a warehouse scan and at most several model
# calls, and an unbounded plan is an unbounded bill.
MAX_SPECIALISTS = 4


class Specialist(enum.StrEnum):
    """The roster. Values are the names the planner chooses between."""

    ANALYST = "analyst"
    INVESTIGATOR = "investigator"
    SENTINEL = "sentinel"
    FORECASTER = "forecaster"
    RETENTION = "retention"


# Written for the planner, not for a human reader — these are the only
# description of each specialist the model ever sees, so they state what the
# specialist returns rather than how it works.
ROSTER: dict[Specialist, str] = {
    Specialist.ANALYST: (
        "Answers a specific factual question about the data by querying it "
        "directly. Use for 'how much', 'how many', 'which', 'list' questions."
    ),
    Specialist.INVESTIGATOR: (
        "Explains why the headline measure moved between two periods, by "
        "decomposing the change across every dimension and finding where it "
        "is concentrated. Use for 'why' questions about a rise or fall."
    ),
    Specialist.SENTINEL: (
        "Finds periods that depart materially from the trend. Use when asking "
        "what looks unusual, or what went wrong and when."
    ),
    Specialist.FORECASTER: (
        "Projects the headline measure forward, with an uncertainty range. "
        "Use for 'what will', 'next quarter', 'are we on track' questions."
    ),
    Specialist.RETENTION: (
        "Scores customers on how likely they are to have lapsed, and reports "
        "what drives the risk. Use for churn, retention, and at-risk-customer "
        "questions."
    ),
}


class FindingStatus(enum.StrEnum):
    OK = "ok"
    # The specialist ran and correctly declined — too few points to forecast,
    # too few customers to model. Distinct from an error: nothing is broken,
    # and the brief should say so rather than omit it silently.
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(slots=True)
class Finding:
    """One specialist's report."""

    specialist: Specialist
    status: FindingStatus
    headline: str
    detail: dict[str, Any] = field(default_factory=dict)
    # How this was derived. SQL for the query-driven specialists, a method name
    # for the statistical ones. Carried so the UI can show provenance next to
    # any number, exactly as Module 6 does for a single answer.
    sql: str | None = None
    method: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def for_prompt(self) -> dict[str, Any]:
        """The compact form handed to the synthesiser."""
        return {
            "specialist": self.specialist.value,
            "status": self.status.value,
            "headline": self.headline,
            "detail": self.detail,
        }


@dataclass(slots=True)
class Plan:
    specialists: list[Specialist]
    reasoning: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(slots=True)
class Brief:
    headline: str
    narrative: str
    recommendations: list[dict[str, str]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


def _plan_schema(available: list[Specialist]) -> dict[str, Any]:
    """Structured-output schema constrained to the specialists on offer.

    The enum is what stops the planner inventing a specialist that does not
    exist, which no amount of prompt instruction reliably prevents.
    """
    return {
        "type": "object",
        "properties": {
            "specialists": {
                "type": "array",
                "items": {"type": "string", "enum": [item.value for item in available]},
            },
            "reasoning": {"type": "string"},
        },
        "required": ["specialists"],
    }


_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "narrative": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "rationale": {"type": "string"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["action", "rationale"],
            },
        },
    },
    "required": ["headline", "narrative"],
}


async def plan(*, question: str, available: list[Specialist]) -> Plan:
    """Choose which specialists to run for a question.

    Falls back to running everything available if the model returns nothing
    usable. A fleet that answers nothing because its router misfired is worse
    than one that spends a little more than it needed to.
    """
    if not available:
        return Plan(specialists=[], reasoning="No specialist applies to this dataset.")

    completion = await provider.complete(
        prompts.build_plan_prompt(
            question=question,
            roster=[(item.value, ROSTER[item]) for item in available],
        ),
        system_instruction=prompts.PLANNER_SYSTEM_INSTRUCTION,
        response_schema=_plan_schema(available),
    )
    payload = provider.parse_json(completion.text)

    allowed = set(available)
    chosen: list[Specialist] = []
    for name in payload.get("specialists") or []:
        try:
            specialist = Specialist(str(name))
        except ValueError:
            continue
        if specialist in allowed and specialist not in chosen:
            chosen.append(specialist)

    if not chosen:
        log.info("fleet.plan_empty", question=question[:120])
        chosen = list(available)

    if len(chosen) > MAX_SPECIALISTS:
        log.info("fleet.plan_truncated", requested=len(chosen), cap=MAX_SPECIALISTS)
        chosen = chosen[:MAX_SPECIALISTS]

    return Plan(
        specialists=chosen,
        reasoning=str(payload.get("reasoning") or "").strip(),
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )


async def synthesise(
    *, dataset: Any, findings: list[Finding], question: str | None = None
) -> Brief:
    """Turn specialist findings into a brief with recommendations."""
    completion = await provider.complete_with_fallback(
        prompts.build_brief_prompt(
            dataset=dataset,
            findings=[finding.for_prompt() for finding in findings],
            question=question,
        ),
        system_instruction=prompts.BRIEF_SYSTEM_INSTRUCTION,
        response_schema=_BRIEF_SCHEMA,
    )
    payload = provider.parse_json(completion.text)

    recommendations: list[dict[str, str]] = []
    for item in payload.get("recommendations") or []:
        if not isinstance(item, dict) or not item.get("action"):
            continue
        recommendations.append(
            {
                "action": str(item["action"]),
                "rationale": str(item.get("rationale") or ""),
                "priority": str(item.get("priority") or "medium"),
            }
        )

    return Brief(
        headline=str(payload.get("headline") or "").strip(),
        narrative=str(payload.get("narrative") or "").strip(),
        recommendations=recommendations,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )
