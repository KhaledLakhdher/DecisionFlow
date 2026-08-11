"""Conversation orchestration around the analyst agent.

Persists both turns of every exchange. The assistant turn stores the SQL and
row count alongside the prose, because "where did that number come from?" is
the first thing anyone asks of an AI-produced figure, and an answer that cannot
be traced back to a query is not worth showing.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decisionflow.core.errors import NotFoundError, ValidationError
from decisionflow.core.logging import get_logger
from decisionflow.db.models.ingestion import (
    Conversation,
    Dataset,
    DatasetStatus,
    Message,
)
from decisionflow.llm import agent
from decisionflow.services import analytics as analytics_service
from decisionflow.services import ingestion as ingestion_service

log = get_logger(__name__)

MAX_QUESTION_LENGTH = 1000


def _title_from(question: str) -> str:
    condensed = " ".join(question.split())
    return condensed[:80] + ("…" if len(condensed) > 80 else "")


async def get_conversation(
    session: AsyncSession, *, org_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.org_id == org_id
        )
    )
    if conversation is None:
        raise NotFoundError("Conversation not found.")
    return conversation


async def list_conversations(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> list[Conversation]:
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.org_id == org_id, Conversation.dataset_id == dataset_id)
        .order_by(Conversation.created_at.desc())
    )
    return list(result)


async def list_messages(
    session: AsyncSession, *, org_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[Message]:
    result = await session.scalars(
        select(Message)
        .where(Message.org_id == org_id, Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(result)


async def ask(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dataset_id: uuid.UUID,
    question: str,
    conversation_id: uuid.UUID | None = None,
) -> tuple[Conversation, Message, agent.AgentAnswer]:
    """Answer a question and record the exchange."""
    question = question.strip()
    if not question:
        raise ValidationError("A question is required.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValidationError(
            f"Questions are limited to {MAX_QUESTION_LENGTH} characters."
        )

    dataset = await ingestion_service.get_dataset(
        session, org_id=org_id, dataset_id=dataset_id
    )
    if dataset.status is not DatasetStatus.READY or dataset.cleaned_at is None:
        raise ValidationError(
            "This dataset is not ready yet. Wait for ingestion and cleaning to finish."
        )

    if conversation_id is None:
        conversation = Conversation(
            org_id=org_id,
            dataset_id=dataset.id,
            title=_title_from(question),
            created_by_id=user_id,
        )
        session.add(conversation)
        await session.flush()
    else:
        conversation = await get_conversation(
            session, org_id=org_id, conversation_id=conversation_id
        )

    session.add(
        Message(
            org_id=org_id,
            conversation_id=conversation.id,
            role="user",
            content=question,
        )
    )
    await session.flush()

    semantics = analytics_service.load_semantics(dataset.columns)
    answer = await agent.ask(
        question=question,
        dataset=dataset,
        columns=list(dataset.columns),
        semantics=semantics,
    )

    assistant_message = Message(
        org_id=org_id,
        conversation_id=conversation.id,
        role="assistant",
        content=answer.answer,
        sql=answer.sql,
        row_count=answer.row_count,
        model=answer.model,
        details={
            "answerable": answer.answerable,
            "attempts": answer.attempts,
            "corrections": answer.corrections,
            "explanation": answer.explanation,
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
        },
    )
    session.add(assistant_message)
    await session.commit()

    return conversation, assistant_message, answer


async def summarise_dataset(
    session: AsyncSession, *, org_id: uuid.UUID, dataset_id: uuid.UUID
) -> dict[str, Any]:
    """An executive summary over a dataset's headline metrics."""
    dataset: Dataset = await ingestion_service.get_dataset(
        session, org_id=org_id, dataset_id=dataset_id
    )
    kpis = await analytics_service.list_kpis(
        session, org_id=org_id, dataset_id=dataset_id
    )
    if not kpis:
        raise ValidationError("This dataset has no metrics to summarise yet.")

    summary = await agent.summarise(dataset, kpis)
    return {"dataset_id": dataset.id, "summary": summary}
