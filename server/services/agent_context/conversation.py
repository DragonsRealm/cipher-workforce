"""Plain conversation persistence: ``key → messages JSON``.

The whole contract:

- ``load_conversation``  — read the transcript at run start (missing row
  is an empty conversation, never an error).
- ``save_conversation``  — atomic upsert of the full transcript after a
  turn, serialized per key by an in-process lock. Best-effort callers
  (the LLM-step activity) must catch — persistence never fails a run.
- ``clear_conversation`` — delete rows (panel clear, workflow cleanup).
- ``list_conversations`` — metadata for the Context panel.

Key = ``(workflow_id, generation, agent_node_id)``. Generation is part of
the key, so a workflow Reset (new generation) is automatically a fresh
conversation. Messages are the ``message_to_wire`` JSON dicts — the same
shape the agent loop holds in memory and carries across continue-as-new.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlmodel import select

from core.logging import get_logger
from models.agent_context import AgentConversation
from services.agent_context.listeners import notify_conversation_saved

logger = get_logger(__name__)

_LOCKS: Dict[tuple, asyncio.Lock] = {}


def _lock(workflow_id: str, generation: int, agent_node_id: str) -> asyncio.Lock:
    key = (workflow_id, generation, agent_node_id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS.setdefault(key, asyncio.Lock())
    return lock


async def load_conversation(
    database: Any,
    *,
    workflow_id: str,
    generation: int,
    agent_node_id: str,
) -> List[Dict[str, Any]]:
    """Return the stored transcript, or ``[]`` for a new conversation."""

    async with database.get_session() as session:
        result = await session.execute(
            select(AgentConversation).where(
                AgentConversation.workflow_id == workflow_id,
                AgentConversation.generation == generation,
                AgentConversation.agent_node_id == agent_node_id,
            )
        )
        row = result.scalar_one_or_none()
        return list(row.messages or []) if row else []


def _without_ts(message: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in message.items() if key != "ts"}


def _stamp_messages(
    incoming: List[Dict[str, Any]],
    existing: List[Any],
    now_iso: str,
) -> List[Dict[str, Any]]:
    """Attach a ``ts`` to every stored message.

    Callers regenerate wires from live ``Message`` objects each turn, so an
    incoming list never carries stamps of its own. The conversation is
    append-only per turn: an incoming entry that matches the stored entry
    at the same index (ignoring ``ts``) keeps its original stamp; anything
    new or changed is stamped now. ``ts`` is a stored-view field only —
    ``message_from_wire`` ignores it, so it never reaches a provider.
    """

    stamped: List[Dict[str, Any]] = []
    for index, message in enumerate(incoming):
        entry = dict(message)
        previous = existing[index] if index < len(existing) else None
        if (
            not entry.get("ts")
            and isinstance(previous, dict)
            and previous.get("ts")
            and _without_ts(previous) == _without_ts(entry)
        ):
            entry["ts"] = previous["ts"]
        elif not entry.get("ts"):
            entry["ts"] = now_iso
        stamped.append(entry)
    return stamped


async def save_conversation(
    database: Any,
    *,
    workflow_id: str,
    generation: int,
    agent_node_id: str,
    messages: List[Dict[str, Any]],
) -> None:
    """Upsert the full transcript for the key; notifies panel listeners."""

    async with _lock(workflow_id, generation, agent_node_id):
        async with database.get_session() as session:
            result = await session.execute(
                select(AgentConversation).where(
                    AgentConversation.workflow_id == workflow_id,
                    AgentConversation.generation == generation,
                    AgentConversation.agent_node_id == agent_node_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = AgentConversation(
                    workflow_id=workflow_id,
                    generation=generation,
                    agent_node_id=agent_node_id,
                )
            row.messages = _stamp_messages(
                [dict(m) for m in messages if isinstance(m, dict)],
                list(row.messages or []),
                datetime.now(timezone.utc).isoformat(),
            )
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            await session.commit()
    await notify_conversation_saved(
        workflow_id=workflow_id,
        generation=generation,
        agent_node_id=agent_node_id,
        message_count=len(messages),
    )


async def clear_conversation(
    database: Any,
    *,
    workflow_id: str,
    generation: int | None = None,
    agent_node_id: str | None = None,
) -> int:
    """Delete matching conversations; returns the number of rows removed."""

    async with database.get_session() as session:
        query = select(AgentConversation).where(
            AgentConversation.workflow_id == workflow_id
        )
        if generation is not None:
            query = query.where(AgentConversation.generation == generation)
        if agent_node_id is not None:
            query = query.where(
                AgentConversation.agent_node_id == agent_node_id
            )
        result = await session.execute(query)
        rows = list(result.scalars())
        for row in rows:
            await session.delete(row)
        await session.commit()
        return len(rows)


async def list_conversations(
    database: Any,
    *,
    workflow_id: str,
) -> List[Dict[str, Any]]:
    """Panel metadata: one entry per stored conversation for the workflow."""

    async with database.get_session() as session:
        result = await session.execute(
            select(AgentConversation)
            .where(AgentConversation.workflow_id == workflow_id)
            .order_by(
                AgentConversation.generation.desc(),
                AgentConversation.agent_node_id,
            )
        )
        return [
            {
                "workflow_id": row.workflow_id,
                "generation": row.generation,
                "agent_node_id": row.agent_node_id,
                "message_count": len(row.messages or []),
                "updated_at": row.updated_at.isoformat()
                if row.updated_at
                else None,
            }
            for row in result.scalars()
        ]


__all__ = [
    "clear_conversation",
    "list_conversations",
    "load_conversation",
    "save_conversation",
]
