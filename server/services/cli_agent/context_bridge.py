"""Plain conversation bridge for specialized agent runtimes.

Canvas Context nodes carry policy and topology only. Specialized providers
(claude_code, codex, rlm, vertex) do not run through the native agent loop,
so this module gives them the same continuity contract the loop has: load
the stored conversation for ``(workflow_id, generation, agent_node_id)`` at
run start, render it into the prompt, and save the exchange back per turn.

The bridge is intentionally absent for legacy ``input-memory`` descriptors so
already-recorded V1 generations retain their original Simple Memory behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from services.llm.protocol import (
    Message,
    message_from_wire,
    message_to_wire,
)


def is_context(value: Any) -> bool:
    """Return whether an edge-walker descriptor is a Context reference."""

    return isinstance(value, dict) and value.get("kind") == "context"


def _wire(role: str, content: str) -> dict[str, Any]:
    """Build one validated conversation wire from plain text."""

    return dict(message_to_wire(Message(role=role, content=content)))


@dataclass
class SpecializedAgentContextBridge:
    """One conversation binding for a specialized provider run.

    ``history`` is the stored transcript at resolve time; ``record_turn``
    appends the new exchange and saves the whole conversation back, so a
    later firing (or a different provider) continues from the same plain
    JSON messages the native agent loop uses.
    """

    database: Any
    workflow_id: str
    generation: int
    agent_node_id: str
    provider: str
    history: tuple[dict[str, Any], ...] = ()

    @property
    def pool_key(self) -> tuple[str, str, int]:
        """Claude pool identity; a new generation is a new key, so Reset
        fences warm subprocesses without a lifecycle notification."""

        return (self.workflow_id, self.agent_node_id, self.generation)

    @classmethod
    async def resolve(
        cls,
        database: Any,
        descriptor: dict[str, Any],
        *,
        provider: str,
        agent_node_id: str,
    ) -> "SpecializedAgentContextBridge":
        if not is_context(descriptor):
            raise ValueError("specialized Context bridge requires a context descriptor")
        workflow_id = str(descriptor.get("workflow_id") or "")
        generation = int(descriptor.get("generation") or 0)
        if not workflow_id:
            raise ValueError("specialized Context bridge requires workflow_id")
        if generation <= 0:
            raise ValueError(
                "specialized Context bridge requires an admitted generation"
            )

        from services.agent_context import load_conversation

        try:
            wires = await load_conversation(
                database,
                workflow_id=workflow_id,
                generation=generation,
                agent_node_id=agent_node_id,
            )
        except Exception as exc:
            # A load failure must be loud: continuing would silently run the
            # agent amnesiac and burn tokens on an incomplete prompt.
            raise ValueError(
                "Conversation load failed for workflow "
                f"{workflow_id} generation {generation} agent "
                f"{agent_node_id}: {exc}"
            ) from exc
        return cls(
            database=database,
            workflow_id=workflow_id,
            generation=generation,
            agent_node_id=agent_node_id,
            provider=provider,
            history=tuple(w for w in wires if isinstance(w, dict)),
        )

    def augment_prompt(self, prompt: str) -> str:
        """Render the stored conversation into a prompt-only adapter."""

        if not self.history:
            return prompt
        lines: list[str] = []
        for wire in self.history:
            message = message_from_wire(wire)
            content = message.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            if not content:
                continue
            lines.append(f"{message.role.upper()}: {content}")
        if not lines:
            return prompt
        return (
            "## Prior conversation\n"
            "The following is the stored conversation for this agent. "
            "Treat it as prior conversation state and continue from it.\n"
            + "\n\n".join(lines)
            + "\n\n## Current request\n"
            + prompt
        )

    async def record_turn(
        self,
        prompt: str,
        response: Optional[str],
    ) -> None:
        """Append one exchange and save the conversation (best-effort)."""

        turn: list[dict[str, Any]] = [_wire("user", prompt)]
        if response:
            turn.append(_wire("assistant", response))
        messages = [*self.history, *turn]

        from services.agent_context import save_conversation

        await save_conversation(
            self.database,
            workflow_id=self.workflow_id,
            generation=self.generation,
            agent_node_id=self.agent_node_id,
            messages=messages,
        )
        self.history = tuple(messages)


__all__ = [
    "SpecializedAgentContextBridge",
    "is_context",
]
