from __future__ import annotations

from typing import Any

import pytest

from models.agent_context import AgentContextEvent
from services.agent_context.compaction import (
    AgentContextCompactionService,
    ContextCompactionCandidate,
    ContextCompactionPolicy,
    get_provider_context_adapter,
    provider_context_request_options,
    select_compaction_boundary,
)
from services.llm.protocol import Message, ToolCall, message_to_wire

from .test_store import (  # noqa: F401, F811
    _resolve,
    _wire,
    context_database,
)


def _event(
    sequence: int,
    event_type: str,
    wire: dict[str, Any] | None = None,
) -> AgentContextEvent:
    return AgentContextEvent(
        sequence=sequence,
        event_type=event_type,
        message_wire_v2=wire,
        operation_id=f"operation-{sequence}",
        provider="openai",
        previous_hash=f"{sequence - 1:064x}",
        payload_hash=f"{sequence:064x}",
    )


def test_boundary_requires_complete_tool_transaction_and_keeps_exact_tail():
    assistant = dict(
        message_to_wire(
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="a", name="lookup", args={}),
                    ToolCall(id="b", name="lookup", args={}),
                ],
            )
        )
    )
    first_tool = dict(
        message_to_wire(
            Message(role="tool", tool_call_id="a", name="lookup")
        )
    )
    second_tool = dict(
        message_to_wire(
            Message(role="tool", tool_call_id="b", name="lookup")
        )
    )
    events = [
        _event(1, "message.assistant", assistant),
        _event(2, "message.tool_result", first_tool),
        _event(3, "message.tool_result", second_tool),
        _event(4, "request.snapshot"),
        _event(5, "message.assistant", _wire("tail")),
    ]

    assert (
        select_compaction_boundary(
            events,
            exact_tail_retention_count=2,
        )
        == 3
    )
    assert (
        select_compaction_boundary(
            events[:2],
            exact_tail_retention_count=1,
        )
        is None
    )


def test_final_only_response_is_a_valid_compaction_boundary():
    events = [
        _event(1, "message.assistant", _wire("final answer")),
        _event(2, "response.final"),
        _event(3, "request.snapshot"),
    ]
    assert (
        select_compaction_boundary(
            events,
            exact_tail_retention_count=1,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_pressure_is_idempotent_and_portable_checkpoint_keeps_tail(
    context_database,  # noqa: F811
):
    from services.agent_context import AgentContextStore

    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    assistant_tool = _wire("tool request", with_tool=True)
    tool_result = dict(
        message_to_wire(
            Message(
                role="tool",
                content='{"ok":true}',
                tool_call_id="call-1",
                name="lookup",
            )
        )
    )
    transitions = [
        ("message.assistant", assistant_tool),
        ("message.tool_result", tool_result),
        ("message.assistant", _wire("final answer")),
        ("response.final", None),
        ("request.snapshot", None),
    ]
    for index, (event_type, wire) in enumerate(transitions, start=1):
        ref = (
            await store.append_transition(
                ref,
                event_type=event_type,
                operation_id=f"transition-{index}",
                message_wire_v2=wire,
                provider="gemini",
            )
        ).ref

    async def portable_compactor(_wires, _policy):
        return ContextCompactionCandidate(
            strategy="portable_structured",
            replay_payload={
                "messages": [
                    dict(
                        message_to_wire(
                            Message(
                                role="user",
                                content=(
                                    "Portable checkpoint: tool completed and "
                                    "the final answer was delivered."
                                ),
                            )
                        )
                    )
                ]
            },
            active_token_count=20,
            lifetime_usage={"input_tokens": 100, "output_tokens": 20},
        )

    result = await AgentContextCompactionService(
        store
    ).update_pressure_and_compact(
        ref,
        operation_id="execution-1:context",
        provider="gemini",
        model="gemini-2.5-pro",
        policy=ContextCompactionPolicy(
            mode="portable",
            trigger_ratio=0.8,
            context_window_override=1024,
            exact_tail_retention_count=1,
        ),
        active_input_tokens=0,
        output_headroom=200,
        rendered_request={"messages": ["x" * 3_000]},
        portable_compactor=portable_compactor,
    )

    assert result.compacted is True
    assert result.checkpoint is not None
    assert result.checkpoint.covers_through_sequence == 4
    assert result.pressure_tokens == 220
    state = await store.load_active(result.ref)
    assert [event.sequence for event in state.tail] == [5]
    assert state.active_token_count == 220

    same_ref = await store.record_active_pressure(
        result.ref,
        operation_id="same-pressure-operation",
        active_token_count=333,
    )
    duplicate = await store.record_active_pressure(
        same_ref,
        operation_id="same-pressure-operation",
        active_token_count=999,
    )
    assert duplicate == same_ref
    assert (await store.load_active(duplicate)).active_token_count == 333


@pytest.mark.asyncio
async def test_openai_native_candidate_preserves_compacted_output_unchanged():
    wire = _wire("continued answer")
    wire["provider_state"] = {
        "provider": "openai",
        "payload": {
            "api": "responses",
            "output": [
                {"type": "message", "role": "user", "content": []},
                {
                    "id": "cmp_1",
                    "type": "compaction",
                    "encrypted_content": "opaque",
                },
                {
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                },
            ],
        },
    }
    adapter = get_provider_context_adapter("openai")
    candidate = await adapter.compact(
        [_event(1, "message.assistant", wire)],
        ContextCompactionPolicy(mode="native"),
    )

    assert candidate is not None
    output = candidate.replay_payload["messages"][0]["provider_state"][
        "payload"
    ]["output"]
    assert output == wire["provider_state"]["payload"]["output"][1:]
    assert output[0]["encrypted_content"] == "opaque"


@pytest.mark.asyncio
async def test_native_checkpoint_stops_at_marker_and_keeps_later_events_exact(
    context_database,  # noqa: F811
):
    from services.agent_context import AgentContextStore

    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    marker_wire = _wire("call the tool", with_tool=True)
    marker_wire["provider_state"] = {
        "provider": "openai",
        "payload": {
            "api": "responses",
            "output": [
                {
                    "id": "cmp_1",
                    "type": "compaction",
                    "encrypted_content": "opaque",
                },
                {
                    "id": "call_1",
                    "type": "function_call",
                    "name": "lookup",
                    "arguments": '{"query":"call the tool"}',
                },
            ],
        },
    }
    tool_wire = dict(
        message_to_wire(
            Message(
                role="tool",
                content='{"ok":true}',
                tool_call_id="call-1",
                name="lookup",
            )
        )
    )
    transitions = [
        ("message.assistant", marker_wire),
        ("message.tool_result", tool_wire),
        ("message.assistant", _wire("final after tool")),
        ("response.final", None),
    ]
    for index, (event_type, wire) in enumerate(transitions, start=1):
        ref = (
            await store.append_transition(
                ref,
                event_type=event_type,
                operation_id=f"native-boundary-{index}",
                message_wire_v2=wire,
                provider="openai",
            )
        ).ref

    result = await AgentContextCompactionService(
        store
    ).update_pressure_and_compact(
        ref,
        operation_id="native-boundary-compaction",
        provider="openai",
        model="gpt-5.6",
        policy=ContextCompactionPolicy(
            mode="native",
            trigger_ratio=0.8,
            context_window_override=1024,
            exact_tail_retention_count=1,
        ),
        active_input_tokens=900,
        output_headroom=100,
    )

    assert result.compacted is True
    assert result.checkpoint is not None
    assert result.checkpoint.covers_through_sequence == 1
    state = await store.load_active(result.ref)
    assert state.ref == result.ref
    assert [event.sequence for event in state.tail] == [2, 3, 4]
    assert [event.message_wire_v2 for event in state.tail] == [
        tool_wire,
        _wire("final after tool"),
        None,
    ]


@pytest.mark.asyncio
async def test_compaction_returns_authoritative_ref_after_concurrent_append(
    context_database,  # noqa: F811
):
    from services.agent_context import AgentContextStore

    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    for index in range(1, 4):
        ref = (
            await store.append_transition(
                ref,
                event_type="message.assistant",
                operation_id=f"authoritative-ref-{index}",
                message_wire_v2=_wire(f"message-{index}"),
                provider="gemini",
            )
        ).ref

    async def compact_with_concurrent_append(_wires, _policy):
        await store.append_transition(
            ref,
            event_type="message.assistant",
            operation_id="authoritative-ref-concurrent",
            message_wire_v2=_wire("concurrent tail"),
            provider="gemini",
        )
        return ContextCompactionCandidate(
            strategy="portable_structured",
            replay_payload={"messages": [_wire("summary")]},
            active_token_count=10,
        )

    result = await AgentContextCompactionService(
        store
    ).update_pressure_and_compact(
        ref,
        operation_id="authoritative-ref-compaction",
        provider="gemini",
        model="gemini-2.5-pro",
        policy=ContextCompactionPolicy(
            mode="portable",
            trigger_ratio=0.8,
            context_window_override=1024,
            exact_tail_retention_count=1,
        ),
        active_input_tokens=900,
        output_headroom=100,
        portable_compactor=compact_with_concurrent_append,
    )

    state = await store.load_active(result.ref)
    assert result.compacted is True
    assert result.ref == state.ref
    assert [event.sequence for event in state.tail] == [3, 4]


def test_native_request_options_are_capability_driven():
    openai = provider_context_request_options(
        provider="openai",
        model="gpt-5.6",
        policy=ContextCompactionPolicy(
            mode="auto",
            trigger_ratio=0.8,
            context_window_override=100_000,
        ),
    )
    anthropic = provider_context_request_options(
        provider="anthropic",
        model="claude-sonnet-4-6",
        policy=ContextCompactionPolicy(
            mode="native",
            trigger_ratio=0.6,
            context_window_override=200_000,
        ),
    )
    compatible = provider_context_request_options(
        provider="openrouter",
        model="openai/gpt-5.6",
        policy=ContextCompactionPolicy(mode="auto"),
    )

    assert openai == {
        "type": "compaction",
        "compact_threshold": 80_000,
        "pause_after_compaction": False,
        "strategy": "openai_responses_compaction",
    }
    assert anthropic == {
        "type": "compaction",
        "compact_threshold": 120_000,
        "pause_after_compaction": True,
        "strategy": "anthropic_compact_20260112",
    }
    assert compatible is None


class _ContextUnifier:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        from services.llm.protocol import LLMResponse, Usage

        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return LLMResponse(
            content=content,
            usage=Usage(
                input_tokens=20,
                output_tokens=5,
                total_tokens=25,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["execute_agent", "execute_chat_agent"],
)
async def test_ai_service_wires_context_sink_for_both_native_paths(
    context_database,  # noqa: F811
    method_name,
):
    from services.agent_context import AgentContextStore
    from services.ai import AIService

    unifier = _ContextUnifier(["context-backed answer"])
    service = AIService(
        auth_service=object(),
        database=context_database,
        cache=None,
        settings=object(),
        chat_unifier=unifier,
    )
    descriptor = {
        "kind": "context",
        "workflow_id": f"workflow-{method_name}",
        "context_node_id": f"context-{method_name}",
        "generation": 1,
        "execution_id": "execution-1",
        "session_id": "chat-1",
        "delegated_task_id": None,
        "policy": {"compaction_mode": "disabled"},
    }
    result = await getattr(service, method_name)(
        node_id="agent-1",
        parameters={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "secret",
            "prompt": "exact user prompt",
            "system_message": "exact system prompt",
        },
        memory_data=descriptor,
        workflow_id=descriptor["workflow_id"],
        context={
            "execution_id": "execution-1",
            "workflow_id": descriptor["workflow_id"],
        },
        database=context_database,
    )

    assert result["success"] is True, result
    assert result["result"]["context"]["thread_id"] == "session:chat-1"
    assert "memory" not in result["result"]
    assert [message.role for message in unifier.calls[0]["messages"]] == [
        "system",
        "user",
    ]

    store = AgentContextStore(context_database)
    ref = await store.resolve_thread(
        workflow_id=descriptor["workflow_id"],
        context_node_id=descriptor["context_node_id"],
        generation=1,
        session_id="chat-1",
    )
    journal, _ = await store.load_journal_page(ref)
    assert [event.event_type for event in journal] == [
        "request.snapshot",
        "message.assistant",
        "response.final",
    ]
    assert all(
        "exact user prompt"
        not in event.model_dump_json()
        for event in journal
        if event.event_type != "message.assistant"
    )


@pytest.mark.asyncio
async def test_session_thread_reconstructs_exact_prior_turn_on_next_run(
    context_database,  # noqa: F811
):
    from services.ai import AIService

    unifier = _ContextUnifier(["first answer", "second answer"])
    service = AIService(
        auth_service=object(),
        database=context_database,
        cache=None,
        settings=object(),
        chat_unifier=unifier,
    )
    base_descriptor = {
        "kind": "context",
        "workflow_id": "workflow-continuity",
        "context_node_id": "context-continuity",
        "generation": 1,
        "session_id": "persistent-chat",
        "delegated_task_id": None,
        "policy": {"compaction_mode": "disabled"},
    }
    for execution_id, prompt in (
        ("execution-1", "first prompt"),
        ("execution-2", "second prompt"),
    ):
        descriptor = {
            **base_descriptor,
            "execution_id": execution_id,
        }
        result = await service.execute_chat_agent(
            node_id="agent-1",
            parameters={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "secret",
                "prompt": prompt,
                "system_message": "stable system",
            },
            memory_data=descriptor,
            workflow_id=descriptor["workflow_id"],
            context={"execution_id": execution_id},
            database=context_database,
        )
        assert result["success"] is True, result

    second_messages = unifier.calls[1]["messages"]
    assert [message.role for message in second_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert [message.content for message in second_messages] == [
        "stable system",
        "first prompt",
        "first answer",
        "second prompt",
    ]
