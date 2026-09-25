"""R11-MP26: read and delete your own Ask conversations.

A conversation is started and continued by the Ask routes themselves (a first
question starts one; `conversation_id` on the request continues it). These
routes only list, read and delete the caller's own -- someone else's answers
404, exactly like one that does not exist.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.conversations import ConversationNotFound, conversation_summary, owned_conversation
from aida.db import get_session
from aida.events import record_audit
from aida.models import AskConversation
from aida.schemas import ApiModel
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["conversations"])

#: Whoever may Ask may keep conversations.
_ASKERS = ("PlatformAdmin", "Analyst", "AgentDeveloper")


class ConversationSummary(ApiModel):
    id: UUID
    datasource_id: UUID
    title: str
    turn_count: int
    created_at: datetime
    last_turn_at: datetime


class ConversationTurnRead(ApiModel):
    turn: int
    agent_run_id: UUID | None
    #: As stored: identifying values replaced by tokens (R11-MP21).
    question: str
    asked_at: datetime


class ConversationRead(ConversationSummary):
    turns: list[ConversationTurnRead]


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_my_conversations(
    datasource_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    context: SecurityContext = Depends(require_roles(*_ASKERS)),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationSummary]:
    """The caller's own conversations, most recent first."""
    statement = select(AskConversation).where(
        AskConversation.organization_id == context.organization_id,
        AskConversation.principal_type == context.principal_type,
        AskConversation.principal_id == context.principal_id,
    )
    if datasource_id is not None:
        statement = statement.where(AskConversation.datasource_id == datasource_id)
    rows = await session.scalars(
        statement.order_by(AskConversation.last_turn_at.desc()).limit(limit)
    )
    return [ConversationSummary(**conversation_summary(row)) for row in rows]


async def _mine(
    session: AsyncSession, conversation_id: UUID, context: SecurityContext
) -> AskConversation:
    try:
        return await owned_conversation(session, conversation_id, context=context)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def read_my_conversation(
    conversation_id: UUID,
    context: SecurityContext = Depends(require_roles(*_ASKERS)),
    session: AsyncSession = Depends(get_session),
) -> ConversationRead:
    conversation = await _mine(session, conversation_id, context)
    return ConversationRead(
        **conversation_summary(conversation),
        turns=[
            ConversationTurnRead(
                turn=int(turn["turn"]),
                agent_run_id=UUID(str(turn["agent_run_id"])) if turn.get("agent_run_id") else None,
                question=str(turn["question"]),
                asked_at=datetime.fromisoformat(str(turn["asked_at"])),
            )
            for turn in conversation.turns
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_conversation(
    conversation_id: UUID,
    context: SecurityContext = Depends(require_roles(*_ASKERS)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Delete the thread. Its runs stay: they are the platform's audit record, and
    they never held the question text."""
    conversation = await _mine(session, conversation_id, context)
    turns = len(conversation.turns)
    await session.delete(conversation)
    record_audit(
        session,
        replace(context, organization_id=conversation.organization_id),
        action="conversation.delete",
        resource_type="ask_conversation",
        resource_id=str(conversation_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"turns": turns},
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
