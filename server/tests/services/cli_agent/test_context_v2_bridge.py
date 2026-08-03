"""Context V2 contracts for specialized CLI providers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models.agent_context import (
    AgentContextActiveState,
    AgentContextEvent,
    AgentContextRef,
)
from nodes.agent.claude_code_agent._pool import (
    ClaudeSessionPool,
    PooledClaudeSession,
)
from services.agent_context import reconstruct_message_wire_v2
from services.cli_agent.context_bridge import SpecializedAgentContextBridge
from services.cli_agent.protocol import CanonicalUsage
from services.cli_agent.service import AICliService
from services.cli_agent.types import ClaudeTaskSpec, CodexTaskSpec


def _bridge(*, fidelity: str, resumable: bool):
    ref = AgentContextRef(
        workflow_id="wf",
        context_node_id="wf:context:1",
        generation=2,
        thread_id="session:chat-1",
        epoch=3,
        revision=4,
    )
    writer = SimpleNamespace(
        ref=ref,
        append_transition=AsyncMock(),
    )
    store = SimpleNamespace()
    return SpecializedAgentContextBridge(
        store=store,
        writer=writer,
        provider="codex",
        operation_prefix="op",
        fidelity=fidelity,
        resumable=resumable,
    )


@pytest.mark.asyncio
async def test_specialized_bridge_rejects_generation_zero():
    with pytest.raises(ValueError, match="admitted generation"):
        await SpecializedAgentContextBridge.resolve(
            object(),
            {
                "kind": "context",
                "workflow_id": "wf",
                "context_node_id": "ctx",
                "generation": 0,
                "execution_id": "run-1",
            },
            provider="codex",
            fidelity="observable_only",
            resumable=False,
        )


@pytest.mark.asyncio
async def test_observable_only_event_is_explicitly_non_resumable_and_exact():
    bridge = _bridge(fidelity="observable_only", resumable=False)
    long_text = "x" * 10_000

    await bridge.capture_provider_event(
        {"type": "assistant", "content": long_text}
    )

    call = bridge.writer.append_transition.await_args.kwargs
    assert call["event_type"] == "provider.stream"
    assert call["payload"]["fidelity"] == "observable_only"
    assert call["payload"]["observable_only"] is True
    assert call["payload"]["non_resumable"] is True
    assert call["payload"]["event"]["content"] == long_text


@pytest.mark.asyncio
async def test_claude_pool_key_contains_context_thread_and_epoch():
    bridge = _bridge(fidelity="provider_bound", resumable=True)
    assert bridge.pool_key == (
        "wf:context:1",
        "session:chat-1",
        3,
    )


@pytest.mark.asyncio
async def test_provider_change_forks_epoch_with_portable_handoff(monkeypatch):
    from services.cli_agent import context_bridge as bridge_module

    old_ref = AgentContextRef(
        workflow_id="wf",
        context_node_id="ctx",
        generation=4,
        thread_id="execution:run-1",
        epoch=1,
        revision=2,
    )
    new_ref = old_ref.model_copy(update={"epoch": 2, "revision": 3})
    event = AgentContextEvent(
        sequence=1,
        event_type="provider.result",
        operation_id="old-result",
        provider="codex",
        previous_hash="0" * 64,
        payload_hash="1" * 64,
        payload_ref="sha256:old-result",
    )
    store = SimpleNamespace(
        resolve_thread=AsyncMock(return_value=old_ref),
        load_thread_summary=AsyncMock(
            return_value=SimpleNamespace(ref=old_ref, provider="codex")
        ),
        load_active=AsyncMock(
            return_value=AgentContextActiveState(
                ref=old_ref,
                tail=[event],
            )
        ),
        get_blob=AsyncMock(
            return_value={
                "fidelity": "observable_only",
                "event": {"response": "old provider answer"},
            }
        ),
        put_blob=AsyncMock(return_value="sha256:handoff"),
        fork_provider=AsyncMock(return_value=new_ref),
    )
    monkeypatch.setattr(
        bridge_module,
        "AgentContextStore",
        lambda database: store,
    )
    import_handoff = AsyncMock(return_value=old_ref)
    monkeypatch.setattr(
        bridge_module,
        "import_generation_zero_handoff",
        import_handoff,
    )

    bridge = await SpecializedAgentContextBridge.resolve(
        object(),
        {
            "kind": "context",
            "workflow_id": "wf",
            "context_node_id": "ctx",
            "generation": 4,
            "execution_id": "run-1",
        },
        provider="rlm",
        fidelity="observable_only",
        resumable=False,
    )

    assert bridge.ref == new_ref
    import_handoff.assert_awaited_once_with(store, old_ref)
    kwargs = store.fork_provider.await_args.kwargs
    assert kwargs["provider"] == "rlm"
    assert kwargs["portable_handoff_ref"] == "sha256:handoff"
    handoff = store.put_blob.await_args.args[0]
    assert handoff["from_provider"] == "codex"
    assert handoff["to_provider"] == "rlm"
    assert handoff["messages"]
    assert "old provider answer" in handoff["messages"][0]["content"]
    replayed = bridge.augment_prompt("new request")
    assert "old provider answer" in replayed
    assert replayed.endswith("new request")

    handoff_event = AgentContextEvent(
        sequence=2,
        event_type="provider_handoff",
        operation_id="fork:handoff",
        provider="rlm",
        previous_hash="1" * 64,
        payload_hash="2" * 64,
        payload_ref="sha256:handoff",
    )
    store.load_active.return_value = AgentContextActiveState(
        ref=new_ref,
        tail=[handoff_event],
    )
    store.get_blob.return_value = handoff
    _, reconstructed = await reconstruct_message_wire_v2(
        store,
        new_ref,
    )
    assert "old provider answer" in reconstructed[0]["content"]


@pytest.mark.asyncio
async def test_pending_provider_handoff_is_recovered_before_request(
    monkeypatch,
):
    from services.cli_agent import context_bridge as bridge_module

    ref = AgentContextRef(
        workflow_id="wf",
        context_node_id="ctx",
        generation=4,
        thread_id="execution:run-1",
        epoch=2,
        revision=3,
    )
    handoff = AgentContextEvent(
        sequence=4,
        event_type="provider_handoff",
        operation_id="fork:handoff",
        provider="rlm",
        previous_hash="0" * 64,
        payload_hash="1" * 64,
        payload_ref="sha256:handoff",
    )
    request = AgentContextEvent(
        sequence=5,
        event_type="provider.request",
        operation_id="retry:request",
        provider="rlm",
        previous_hash="1" * 64,
        payload_hash="2" * 64,
    )
    message = {
        "version": 2,
        "role": "assistant",
        "content": "committed prior answer",
        "blocks": [{"type": "text", "text": "committed prior answer"}],
        "tool_calls": [],
        "tool_call_id": None,
        "name": None,
        "provider_state": {},
    }
    store = SimpleNamespace(
        resolve_thread=AsyncMock(return_value=ref),
        load_thread_summary=AsyncMock(
            return_value=SimpleNamespace(ref=ref, provider="rlm")
        ),
        load_active=AsyncMock(
            return_value=AgentContextActiveState(
                ref=ref,
                tail=[handoff, request],
            )
        ),
        get_blob=AsyncMock(
            return_value={"messages": [message]}
        ),
    )
    monkeypatch.setattr(
        bridge_module,
        "AgentContextStore",
        lambda database: store,
    )
    monkeypatch.setattr(
        bridge_module,
        "import_generation_zero_handoff",
        AsyncMock(return_value=ref),
    )

    bridge = await SpecializedAgentContextBridge.resolve(
        object(),
        {
            "kind": "context",
            "workflow_id": "wf",
            "context_node_id": "ctx",
            "generation": 4,
            "execution_id": "run-1",
        },
        provider="rlm",
        fidelity="observable_only",
        resumable=False,
    )

    assert "committed prior answer" in bridge.augment_prompt("continue")


@pytest.mark.asyncio
async def test_cli_adapter_sends_portable_handoff_in_effective_prompt(
    monkeypatch,
):
    bridge = SimpleNamespace(
        augment_prompt=lambda prompt: f"prior portable answer\n{prompt}",
        append_observable=AsyncMock(),
    )
    monkeypatch.setattr(
        "services.cli_agent.service."
        "SpecializedAgentContextBridge.resolve",
        AsyncMock(return_value=bridge),
    )
    monkeypatch.setattr(
        "services.plugin.deps.get_database",
        lambda: object(),
    )
    monkeypatch.setattr(
        AICliService,
        "_resolve_repo_root",
        AsyncMock(return_value=None),
    )

    non_directory = Path(__file__).resolve()
    result = await AICliService().run_batch(
        "codex",
        tasks=[CodexTaskSpec(prompt="current request")],
        node_id="codex-1",
        workflow_id="wf",
        workspace_dir=non_directory,
        repo_root=non_directory,
        broadcaster=None,
        connected_context={
            "kind": "context",
            "workflow_id": "wf",
            "context_node_id": "ctx",
            "generation": 2,
            "execution_id": "run-1",
        },
        execution_id="run-1",
    )

    assert result.tasks[0].prompt == (
        "prior portable answer\ncurrent request"
    )
    request = bridge.append_observable.await_args_list[0].args[1]
    assert request["tasks"][0].prompt == (
        "prior portable answer\ncurrent request"
    )


@pytest.mark.asyncio
async def test_pool_terminates_previous_epoch_before_new_context_acquire(
    monkeypatch,
):
    old_key = ("wf:context:1", "session:chat-1", 1)
    new_key = ("wf:context:1", "session:chat-1", 2)
    old = PooledClaudeSession(
        memory_node_id=old_key,
        process=SimpleNamespace(returncode=None, pid=1),
        cwd=Path.cwd(),
    )
    new = PooledClaudeSession(
        memory_node_id=new_key,
        process=SimpleNamespace(returncode=None, pid=2),
        cwd=Path.cwd(),
    )
    pool = object.__new__(ClaudeSessionPool)
    pool._pool = {old_key: old}
    pool._pool_lock = asyncio.Lock()
    pool._max_size = 16
    terminated = []

    async def terminate(key, *, reason="explicit"):
        terminated.append((key, reason))
        pool._pool.pop(key, None)

    monkeypatch.setattr(pool, "_terminate_locked", terminate)
    monkeypatch.setattr(pool, "_spawn", AsyncMock(return_value=new))
    monkeypatch.setattr(pool, "_emit_event", AsyncMock())

    acquired = await pool.acquire(
        new_key,
        spec=ClaudeTaskSpec(prompt="next epoch"),
        cwd=Path.cwd(),
        env={},
        defaults={},
        mcp_endpoint_url=None,
        mcp_bearer_token=None,
    )
    await pool.release(acquired)

    assert terminated == [(old_key, "epoch_changed")]
    assert old_key not in pool._pool
    assert pool._pool[new_key] is new


class _FullResponseProvider:
    name = "claude"

    @staticmethod
    def is_final_event(event):
        return event.get("type") == "result"

    @staticmethod
    def event_to_session_result(events, stderr, exit_code):
        del stderr, exit_code
        return {
            "session_id": "uuid-1",
            "response": events[-1]["result"],
            "canonical_usage": CanonicalUsage(),
            "success": True,
        }

    @staticmethod
    def canonical_usage(events):
        del events
        return CanonicalUsage()


@pytest.mark.asyncio
async def test_pool_sends_full_raw_event_to_context_before_ui_truncation():
    raw = "z" * 10_000
    captured = []

    async def sink(event):
        captured.append(event)

    session = PooledClaudeSession(
        memory_node_id=("ctx", "execution:1", 1),
        process=SimpleNamespace(returncode=None, pid=1),
        cwd=Path.cwd(),
        context_event_sink=sink,
    )
    pool = object.__new__(ClaudeSessionPool)
    pool._provider = _FullResponseProvider()

    event = {"type": "result", "result": raw, "session_id": "uuid-1"}
    await pool._handle_stream_event(session, event)
    result = pool._build_result_from_events(
        session=session,
        events=[event],
        prompt="hello",
        success=True,
    )

    assert captured[0]["result"] == raw
    assert len(result.response) == 4_000


@pytest.mark.asyncio
async def test_pool_surfaces_context_persistence_failure():
    async def broken_sink(event):
        del event
        raise RuntimeError("database unavailable")

    session = PooledClaudeSession(
        memory_node_id=("ctx", "execution:1", 1),
        process=SimpleNamespace(returncode=None, pid=1),
        cwd=Path.cwd(),
        context_event_sink=broken_sink,
    )
    pool = object.__new__(ClaudeSessionPool)
    pool._provider = _FullResponseProvider()

    await pool._handle_stream_event(
        session,
        {"type": "assistant", "message": {"content": "secret"}},
    )

    assert session.result_event.is_set()
    assert "context_event_persistence_failed" in (
        session.context_capture_error or ""
    )
    assert session.events_this_turn == []
