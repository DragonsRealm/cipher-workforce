"""Contract for the specialized-provider conversation bridge.

Specialized providers (claude_code, codex, rlm, vertex) do not run through
the native agent loop, so the bridge gives them the same continuity
contract: load the stored conversation at run start, render it into the
prompt, save the exchange back per turn.
"""

from __future__ import annotations

from typing import Any

import pytest

import services.agent_context as agent_context
from services.cli_agent.context_bridge import (
    SpecializedAgentContextBridge,
    is_context,
)
from services.llm.protocol import Message, message_to_wire


def _wire(role: str, content: str) -> dict:
    return dict(message_to_wire(Message(role=role, content=content)))


_DESCRIPTOR = {
    "kind": "context",
    "node_id": "wf:context:1",
    "context_node_id": "wf:context:1",
    "workflow_id": "workflow-1",
    "generation": 2,
    "execution_id": "execution-1",
}


def test_is_context_requires_the_kind_discriminator():
    assert is_context(_DESCRIPTOR)
    assert not is_context({"node_id": "memory-1"})
    assert not is_context(None)
    assert not is_context("context")


@pytest.mark.asyncio
async def test_resolve_rejects_generation_zero():
    with pytest.raises(ValueError, match="admitted generation"):
        await SpecializedAgentContextBridge.resolve(
            object(),
            {**_DESCRIPTOR, "generation": 0},
            provider="claude_code",
            agent_node_id="agent-1",
        )


@pytest.mark.asyncio
async def test_resolve_rejects_non_context_descriptor():
    with pytest.raises(ValueError, match="context descriptor"):
        await SpecializedAgentContextBridge.resolve(
            object(),
            {"node_id": "memory-1"},
            provider="claude_code",
            agent_node_id="agent-1",
        )


@pytest.mark.asyncio
async def test_resolve_loads_the_stored_conversation(monkeypatch):
    stored = [_wire("user", "first prompt"), _wire("assistant", "first answer")]
    observed_keys: list[dict] = []

    async def fake_load(database: Any, **key: Any) -> list:
        observed_keys.append(key)
        return list(stored)

    monkeypatch.setattr(agent_context, "load_conversation", fake_load)
    bridge = await SpecializedAgentContextBridge.resolve(
        object(),
        _DESCRIPTOR,
        provider="claude_code",
        agent_node_id="agent-1",
    )
    assert observed_keys == [
        {
            "workflow_id": "workflow-1",
            "generation": 2,
            "agent_node_id": "agent-1",
        }
    ]
    assert list(bridge.history) == stored


@pytest.mark.asyncio
async def test_resolve_load_failure_is_loud(monkeypatch):
    async def broken_load(database: Any, **key: Any) -> list:
        raise RuntimeError("db locked")

    monkeypatch.setattr(agent_context, "load_conversation", broken_load)
    with pytest.raises(ValueError, match="Conversation load failed"):
        await SpecializedAgentContextBridge.resolve(
            object(),
            _DESCRIPTOR,
            provider="claude_code",
            agent_node_id="agent-1",
        )


def test_pool_key_is_the_conversation_key():
    bridge = SpecializedAgentContextBridge(
        database=object(),
        workflow_id="workflow-1",
        generation=3,
        agent_node_id="agent-1",
        provider="claude_code",
    )
    assert bridge.pool_key == ("workflow-1", "agent-1", 3)


def test_augment_prompt_renders_stored_history():
    bridge = SpecializedAgentContextBridge(
        database=object(),
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="agent-1",
        provider="claude_code",
        history=(
            _wire("user", "first prompt"),
            _wire("assistant", "first answer"),
        ),
    )
    rendered = bridge.augment_prompt("continue")
    assert "USER: first prompt" in rendered
    assert "ASSISTANT: first answer" in rendered
    assert rendered.strip().endswith("continue")
    # Prior context precedes the current request.
    assert rendered.index("first answer") < rendered.index("## Current request")


def test_augment_prompt_without_history_is_the_identity():
    bridge = SpecializedAgentContextBridge(
        database=object(),
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="agent-1",
        provider="claude_code",
    )
    assert bridge.augment_prompt("continue") == "continue"


@pytest.mark.asyncio
async def test_record_turn_appends_and_saves_the_whole_conversation(
    monkeypatch,
):
    saved: list[dict] = []

    async def fake_save(database: Any, **kwargs: Any) -> None:
        saved.append(kwargs)

    monkeypatch.setattr(agent_context, "save_conversation", fake_save)
    bridge = SpecializedAgentContextBridge(
        database=object(),
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="agent-1",
        provider="claude_code",
        history=(_wire("user", "first"), _wire("assistant", "one")),
    )
    await bridge.record_turn("second", "two")
    assert len(saved) == 1
    assert saved[0]["workflow_id"] == "workflow-1"
    assert saved[0]["generation"] == 2
    assert saved[0]["agent_node_id"] == "agent-1"
    contents = [m["content"] for m in saved[0]["messages"]]
    assert contents == ["first", "one", "second", "two"]
    # The in-memory history advances so a second turn does not lose this one.
    assert [m["content"] for m in bridge.history] == contents


@pytest.mark.asyncio
async def test_record_turn_without_response_records_the_prompt_only(
    monkeypatch,
):
    saved: list[dict] = []

    async def fake_save(database: Any, **kwargs: Any) -> None:
        saved.append(kwargs)

    monkeypatch.setattr(agent_context, "save_conversation", fake_save)
    bridge = SpecializedAgentContextBridge(
        database=object(),
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="agent-1",
        provider="claude_code",
    )
    await bridge.record_turn("orphan prompt", None)
    assert [m["role"] for m in saved[0]["messages"]] == ["user"]
