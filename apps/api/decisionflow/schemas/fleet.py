"""Wire contracts for the agent fleet."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BriefingRequest(BaseModel):
    # Omit for a standing briefing over everything the dataset supports.
    # Supply one and the planner routes it to the specialists that fit.
    question: str | None = Field(default=None, max_length=1000)


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    specialist: str
    # ok | refused | failed. Surfaced rather than filtered so the UI can show a
    # refusal as the legitimate outcome it is, distinct from a breakage.
    status: str
    headline: str
    detail: dict[str, Any]
    # The query or method behind the numbers, so a reader can check a figure
    # instead of taking it on faith.
    sql: str | None
    method: str | None


class BriefingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_id: uuid.UUID
    question: str | None
    headline: str
    narrative: str
    recommendations: list[dict[str, Any]]
    plan_reasoning: str | None
    findings: list[FindingOut]
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    created_at: datetime


class BriefingSummaryOut(BaseModel):
    """A briefing without its findings, for the history list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str | None
    headline: str
    created_at: datetime


class FleetCapabilityOut(BaseModel):
    """Which specialists this dataset's shape can serve.

    Lets the UI say what a briefing will actually cover before spending the
    model calls, rather than offering everything and refusing most of it.
    """

    specialists: list[str]
    # What is available once a question is supplied — the analyst only has
    # something to do when there is something to answer.
    with_question: list[str]
