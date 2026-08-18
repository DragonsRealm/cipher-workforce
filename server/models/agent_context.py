"""Durable agent conversation storage.

One row per conversation: the agent's message transcript as plain JSON
wires (the ``message_to_wire`` shape), keyed by
``(workflow_id, generation, agent_node_id)``. Every firing of an agent in a
deployment generation — chat messages, task-completion reviews, anything —
continues the same conversation. A workflow Reset admits a new generation,
which is a new key, which is a fresh conversation: no epochs, no fencing.

This is the industry-standard shape (LangGraph ``thread_id`` checkpoints,
OpenAI ``conversation_id``, Claude Code session transcripts): serialized
messages under a stable conversation key, loaded at run start, saved per
turn. See docs-internal/agent_context_flow.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentConversation(SQLModel, table=True):
    """The full message transcript for one agent in one generation."""

    __tablename__ = "agent_conversations"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "generation",
            "agent_node_id",
            name="uq_agent_conversation_key",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    workflow_id: str = Field(index=True, max_length=255)
    generation: int = Field(index=True)
    agent_node_id: str = Field(max_length=255)
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


__all__ = ["AgentConversation"]
