"""Wire contracts for the AI analyst."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    # Omit to start a new thread; supply to continue an existing one.
    conversation_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    # Kept on the wire so the UI can show "how this was calculated". An
    # AI-produced figure with no visible provenance should not be trusted, and
    # this is what makes it checkable.
    sql: str | None
    row_count: int | None
    model: str | None
    details: dict[str, Any]
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_id: uuid.UUID
    title: str
    created_at: datetime


class AnswerOut(BaseModel):
    """The full result of one question."""

    conversation_id: uuid.UUID
    message: MessageOut
    answer: str
    sql: str | None
    rows: list[dict[str, Any]]
    row_count: int
    # False when the model declined because the data cannot support the
    # question — a correct outcome, not an error, and the UI should present it
    # differently from a failure.
    answerable: bool
    attempts: int
    corrections: list[str]


class SummaryOut(BaseModel):
    dataset_id: uuid.UUID
    summary: str
