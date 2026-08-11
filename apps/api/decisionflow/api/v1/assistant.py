"""Ask questions of a dataset in plain English."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from decisionflow.api.deps import (
    TenantPrincipal,
    TenantPrincipalDep,
    TenantSessionDep,
    require_role,
)
from decisionflow.core import ratelimit
from decisionflow.db.models.tenancy import Role
from decisionflow.schemas.assistant import (
    AnswerOut,
    AskRequest,
    ConversationOut,
    MessageOut,
    SummaryOut,
)
from decisionflow.services import assistant as assistant_service
from decisionflow.services import ingestion as ingestion_service

router = APIRouter(prefix="/datasets/{dataset_id}", tags=["assistant"])

# Asking a question is a read of the data, so viewers may do it. It also spends
# money on model calls, which is why it is rate limited below.
AnalystPrincipal = Annotated[TenantPrincipal, Depends(require_role(Role.VIEWER))]

# Two model calls per question, against a quota-limited key. This protects the
# budget as much as the service.
ASK_LIMIT = ratelimit.RateLimit(limit=20, window_seconds=300)


@router.post("/ask", response_model=AnswerOut)
async def ask(
    dataset_id: uuid.UUID,
    payload: AskRequest,
    actor: AnalystPrincipal,
    session: TenantSessionDep,
) -> AnswerOut:
    """Answer a natural-language question from this dataset's cleaned data."""
    await ratelimit.enforce("ask:org", str(actor.org_id), ASK_LIMIT)

    conversation, message, answer = await assistant_service.ask(
        session,
        org_id=actor.org_id,
        user_id=actor.user_id,
        dataset_id=dataset_id,
        question=payload.question,
        conversation_id=payload.conversation_id,
    )

    return AnswerOut(
        conversation_id=conversation.id,
        message=MessageOut.model_validate(message),
        answer=answer.answer,
        sql=answer.sql,
        rows=answer.rows,
        row_count=answer.row_count,
        answerable=answer.answerable,
        attempts=answer.attempts,
        corrections=answer.corrections,
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    dataset_id: uuid.UUID, tenant: TenantPrincipalDep, session: TenantSessionDep
) -> list[ConversationOut]:
    await ingestion_service.get_dataset(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    conversations = await assistant_service.list_conversations(
        session, org_id=tenant.org_id, dataset_id=dataset_id
    )
    return [ConversationOut.model_validate(item) for item in conversations]


@router.get("/conversations/{conversation_id}", response_model=list[MessageOut])
async def conversation_messages(
    dataset_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tenant: TenantPrincipalDep,
    session: TenantSessionDep,
) -> list[MessageOut]:
    await assistant_service.get_conversation(
        session, org_id=tenant.org_id, conversation_id=conversation_id
    )
    messages = await assistant_service.list_messages(
        session, org_id=tenant.org_id, conversation_id=conversation_id
    )
    return [MessageOut.model_validate(message) for message in messages]


@router.post("/summary", response_model=SummaryOut)
async def dataset_summary(
    dataset_id: uuid.UUID, actor: AnalystPrincipal, session: TenantSessionDep
) -> SummaryOut:
    """A short executive summary over this dataset's headline metrics."""
    await ratelimit.enforce("summary:org", str(actor.org_id), ASK_LIMIT)

    result = await assistant_service.summarise_dataset(
        session, org_id=actor.org_id, dataset_id=dataset_id
    )
    return SummaryOut(**result)
