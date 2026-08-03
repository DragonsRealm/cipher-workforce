from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from models.agent_context import (
    AgentContextBlobRecord,
    AgentContextCheckpointRecord,
    AgentContextEventRecord,
    AgentContextRef,
    AgentContextThreadRecord,
)
from nodes.context import AgentContextNode
from nodes.context import _handlers as context_handlers
from services.agent_context import (
    AgentContextError,
    AgentContextStore,
    AgentContextTransitionWriter,
    ContextArchivedError,
    StaleEpochError,
    import_generation_zero_handoff,
    reconstruct_message_wire_v2,
)
from services.llm.protocol import (
    ContentBlock,
    Message,
    ToolCall,
    message_to_wire,
)


@pytest.fixture
async def context_database():
    # Root conftest stubs core.database for fast plugin tests. Load the real
    # module privately for SQLite transaction/restart coverage.
    module_name = f"tests._real_context_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[3] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".agent-context-{uuid.uuid4().hex}.db"
    settings = SimpleNamespace(
        database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
        database_echo=False,
        database_pool_size=5,
        database_max_overflow=5,
    )
    database = module.Database(settings)
    await database.startup()
    try:
        yield database
    finally:
        await database.shutdown()
        sys.modules.pop(module_name, None)
        for candidate in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ):
            candidate.unlink(missing_ok=True)


def _wire(content: str, *, with_tool: bool = False) -> dict:
    calls = (
        [
            ToolCall(
                id="call-1",
                name="lookup",
                args={"query": content},
                raw_arguments=f'{{"query":"{content}"}}',
            )
        ]
        if with_tool
        else []
    )
    blocks = [ContentBlock(type="text", text=content)]
    if calls:
        blocks.append(ContentBlock(type="tool_call", tool_call=calls[0]))
    return dict(
        message_to_wire(
            Message(
                role="assistant",
                content=content,
                tool_calls=calls,
                blocks=blocks,
                provider_state={
                    "openai": {
                        "output": [{"type": "reasoning", "id": "rs_1"}]
                    }
                },
            )
        )
    )


async def _resolve(
    store: AgentContextStore,
    **overrides,
) -> AgentContextRef:
    values = {
        "workflow_id": "workflow-1",
        "context_node_id": "workflow-1:context:1",
        "generation": 2,
        "execution_id": "execution-1",
    }
    values.update(overrides)
    return await store.resolve_thread(**values)


@pytest.mark.asyncio
async def test_thread_resolution_priority_and_isolation(context_database):
    store = AgentContextStore(context_database)
    execution = await _resolve(store)
    task = await _resolve(
        store,
        delegated_task_id="task-1",
        execution_id="execution-2",
    )
    session = await _resolve(
        store,
        session_id="chat-1",
        delegated_task_id="task-2",
        execution_id="execution-3",
    )
    same_session = await _resolve(
        store,
        session_id="chat-1",
        execution_id="execution-99",
    )

    assert execution.thread_id == "execution:execution-1"
    assert task.thread_id == "task:task-1"
    assert session.thread_id == "session:chat-1"
    assert same_session == session
    assert len({execution.thread_id, task.thread_id, session.thread_id}) == 3
    summary = await store.load_thread_summary(task)
    assert summary.ref == task
    assert summary.thread_kind == "task"


@pytest.mark.asyncio
async def test_generation_zero_legacy_artifact_handoff_is_idempotent(
    context_database,
):
    store = AgentContextStore(context_database)
    source = await _resolve(
        store,
        generation=0,
        session_id="legacy-session",
        execution_id=None,
    )
    artifact = {
        "format": "legacy_markdown",
        "fidelity": "legacy_partial",
        "content": "# Conversation History\n\nUser: keep this fact",
    }
    artifact_ref = await store.put_blob(artifact)
    source = (
        await store.append_transition(
            source,
            event_type="legacy_partial",
            operation_id="legacy-import-1",
            payload_ref=artifact_ref,
            provider="legacy",
        )
    ).ref
    await store.bind_provider(
        source,
        provider="claude_code",
        binding_type="session_uuid",
        binding={"session_uuid": "session-uuid-1"},
        operation_id="legacy-binding-1",
    )
    target = await _resolve(
        store,
        generation=2,
        session_id="legacy-session",
        execution_id=None,
    )

    first = await import_generation_zero_handoff(store, target)
    second = await import_generation_zero_handoff(store, first)
    _, replay = await reconstruct_message_wire_v2(store, second)
    bindings = await store.load_provider_bindings(
        second,
        provider="claude_code",
    )

    assert len(replay) == 1
    assert "keep this fact" in replay[0]["content"]
    assert (await store.load_active(second)).tail[0].payload_ref == artifact_ref
    assert await store.get_blob(artifact_ref) == artifact
    assert len(bindings) == 1
    assert await store.get_blob(bindings[0].binding_ref) == {
        "session_uuid": "session-uuid-1"
    }

    cleared = await store.start_epoch(
        second,
        operation_id="clear-after-legacy-handoff",
        provider="openai",
    )
    cleared = await import_generation_zero_handoff(store, cleared)
    assert (await store.load_active(cleared)).tail == []
    assert await store.load_provider_bindings(
        cleared,
        provider="claude_code",
    ) == []

    later_target = await _resolve(
        store,
        generation=3,
        session_id="legacy-session",
        execution_id=None,
    )
    later = await import_generation_zero_handoff(store, later_target)
    _, later_replay = await reconstruct_message_wire_v2(store, later)
    assert len(later_replay) == 1


@pytest.mark.asyncio
async def test_workflow_reset_rotates_only_the_active_generation(
    context_database,
):
    store = AgentContextStore(context_database)
    migration = await _resolve(store, generation=0)
    current = await _resolve(store, generation=2)
    other = await _resolve(store, generation=3)

    result = await AgentContextNode.reset_execution_state(
        node_id=current.context_node_id,
        workflow_id=current.workflow_id,
        execution_id="execution-reset",
        generation=2,
        graph={},
        database=context_database,
    )
    migration_after = (
        await store.list_threads(
            workflow_id=migration.workflow_id,
            context_node_id=migration.context_node_id,
            generation=0,
        )
    )[0].ref
    current_after = (
        await store.list_threads(
            workflow_id=current.workflow_id,
            context_node_id=current.context_node_id,
            generation=2,
        )
    )[0].ref
    other_after = (
        await store.list_threads(
            workflow_id=other.workflow_id,
            context_node_id=other.context_node_id,
            generation=3,
        )
    )[0].ref

    assert result["rotated_threads"] == 1
    assert migration_after.epoch == migration.epoch
    assert current_after.epoch == current.epoch + 1
    assert other_after.epoch == other.epoch


@pytest.mark.asyncio
async def test_workflow_reset_scans_beyond_one_thousand_threads(
    context_database,
    monkeypatch,
):
    thread_count = 1001
    async with context_database.get_session() as session:
        session.add_all(
            [
                AgentContextThreadRecord(
                    workflow_id="workflow-paged-reset",
                    context_node_id="context-paged-reset",
                    generation=7,
                    thread_id=f"session:thread-{index:04d}",
                    thread_kind="session",
                )
                for index in range(thread_count)
            ]
        )
        await session.commit()

    rotated: list[str] = []

    async def rotate(
        self,
        ref,
        *,
        operation_id,
        provider=None,
        handoff_payload_ref=None,
    ):
        del self, operation_id, provider, handoff_payload_ref
        rotated.append(ref.thread_id)
        return ref.model_copy(
            update={
                "epoch": ref.epoch + 1,
                "revision": ref.revision + 1,
            }
        )

    monkeypatch.setattr(AgentContextStore, "start_epoch", rotate)
    fence = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "services.agent_context.lifecycle."
        "fence_context_provider_resources",
        fence,
    )
    monkeypatch.setattr(
        "nodes.context._events.dispatch_context_epoch_started",
        dispatch,
    )

    result = await AgentContextNode.reset_execution_state(
        node_id="context-paged-reset",
        workflow_id="workflow-paged-reset",
        execution_id="execution-paged-reset",
        generation=7,
        graph={},
        database=context_database,
    )

    assert result == {
        "reset": True,
        "rotated_threads": thread_count,
    }
    assert len(rotated) == thread_count
    assert len(set(rotated)) == thread_count
    assert fence.await_count == thread_count
    assert dispatch.await_count == thread_count


@pytest.mark.asyncio
async def test_append_is_exact_ordered_hash_chained_and_idempotent(
    context_database,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    first = await store.append_transition(
        ref,
        event_type="assistant",
        operation_id="provider-response-1",
        message_wire_v2=_wire("first", with_tool=True),
        provider="openai",
    )
    duplicate = await store.append_transition(
        ref,
        event_type="assistant",
        operation_id="provider-response-1",
        message_wire_v2=_wire("first", with_tool=True),
        provider="openai",
    )
    with pytest.raises(
        AgentContextError,
        match="context_operation_id_reuse_mismatch",
    ):
        await store.append_transition(
            ref,
            event_type="assistant",
            operation_id="provider-response-1",
            message_wire_v2=_wire(
                "this must not replace the committed event"
            ),
            provider="openai",
        )
    second = await store.append_transition(
        first.ref,
        event_type="tool_result",
        operation_id="tool-result-1",
        message_wire_v2=dict(
            message_to_wire(
                Message(
                    role="tool",
                    content='{"ok":true}',
                    tool_call_id="call-1",
                    name="lookup",
                )
            )
        ),
        provider="openai",
    )

    state = await store.load_active(second.ref)
    assert [event.sequence for event in state.tail] == [1, 2]
    assert duplicate.applied is False
    assert duplicate.event.payload_hash == first.event.payload_hash
    assert state.tail[0].message_wire_v2 == _wire("first", with_tool=True)
    assert state.tail[1].previous_hash == state.tail[0].payload_hash
    assert state.ref.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        (
            "sequence",
            3,
            "context_event_sequence_integrity_broken",
        ),
        (
            "previous_hash",
            "f" * 64,
            "context_event_chain_integrity_broken",
        ),
        (
            "payload_hash",
            "e" * 64,
            "context_event_hash_integrity_broken",
        ),
    ],
)
async def test_load_active_rejects_corrupt_event_chain(
    context_database,
    field,
    value,
    error_code,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    first = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="integrity-event-1",
        message_wire_v2=_wire("first"),
        provider="openai",
    )
    second = await store.append_transition(
        first.ref,
        event_type="message.assistant",
        operation_id="integrity-event-2",
        message_wire_v2=_wire("second"),
        provider="openai",
    )

    async with context_database.get_session() as session:
        result = await session.execute(
            select(AgentContextEventRecord).where(
                AgentContextEventRecord.operation_id
                == "integrity-event-2"
            )
        )
        event = result.scalar_one()
        setattr(event, field, value)
        await session.commit()

    with pytest.raises(AgentContextError, match=error_code):
        await store.load_active(second.ref)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        (
            "covers_through_sequence",
            2,
            "active_checkpoint_invariant_broken",
        ),
        (
            "source_hash",
            "f" * 64,
            "checkpoint_source_integrity_broken",
        ),
    ],
)
async def test_load_active_rejects_corrupt_checkpoint_source(
    context_database,
    field,
    value,
    error_code,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    for index in range(1, 3):
        ref = (
            await store.append_transition(
                ref,
                event_type="message.assistant",
                operation_id=f"checkpoint-integrity-{index}",
                message_wire_v2=_wire(f"message-{index}"),
                provider="openai",
            )
        ).ref
    plan = await store.prepare_compaction(
        ref,
        operation_id="checkpoint-integrity-prepare",
        provider="openai",
        strategy="portable",
        covers_through_sequence=1,
    )
    replay_ref = await store.put_blob({"messages": [_wire("summary")]})
    await store.commit_checkpoint(
        ref,
        attempt_id=plan.attempt_id,
        operation_id="checkpoint-integrity-commit",
        replay_payload_ref=replay_ref,
        active_token_count=10,
    )

    async with context_database.get_session() as session:
        result = await session.execute(
            select(AgentContextCheckpointRecord).where(
                AgentContextCheckpointRecord.operation_id
                == "checkpoint-integrity-commit"
            )
        )
        checkpoint = result.scalar_one()
        setattr(checkpoint, field, value)
        await session.commit()

    with pytest.raises(AgentContextError, match=error_code):
        await store.load_active(ref)


@pytest.mark.asyncio
async def test_blob_digest_is_verified_for_direct_and_checkpoint_replay(
    context_database,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    first = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="blob-integrity-event-1",
        message_wire_v2=_wire("first"),
        provider="openai",
    )
    second = await store.append_transition(
        first.ref,
        event_type="message.assistant",
        operation_id="blob-integrity-event-2",
        message_wire_v2=_wire("second"),
        provider="openai",
    )
    plan = await store.prepare_compaction(
        second.ref,
        operation_id="blob-integrity-prepare",
        provider="openai",
        strategy="portable",
        covers_through_sequence=1,
    )
    replay_ref = await store.put_blob({"messages": [_wire("summary")]})
    await store.commit_checkpoint(
        second.ref,
        attempt_id=plan.attempt_id,
        operation_id="blob-integrity-commit",
        replay_payload_ref=replay_ref,
        active_token_count=10,
    )

    digest = replay_ref.removeprefix("sha256:")
    async with context_database.get_session() as session:
        blob = await session.get(AgentContextBlobRecord, digest)
        assert blob is not None
        blob.json_payload = {"messages": [_wire("tampered")]}
        await session.commit()

    with pytest.raises(
        AgentContextError,
        match="context_blob_integrity_broken",
    ):
        await store.get_blob(replay_ref)
    with pytest.raises(
        AgentContextError,
        match="context_blob_integrity_broken",
    ):
        await store.load_active(second.ref)


@pytest.mark.asyncio
async def test_parallel_appends_serialize_without_losing_events(
    context_database,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)

    results = await asyncio.gather(
        *(
            store.append_transition(
                ref,
                event_type="message.assistant",
                operation_id=f"parallel-{index}",
                message_wire_v2=_wire(f"parallel-message-{index}"),
            )
            for index in range(8)
        )
    )
    current = max(results, key=lambda result: result.ref.revision).ref
    state = await store.load_active(current)
    assert [event.sequence for event in state.tail] == list(range(1, 9))
    assert {event.operation_id for event in state.tail} == {
        f"parallel-{index}" for index in range(8)
    }
    for previous, current_event in zip(state.tail, state.tail[1:]):
        assert current_event.previous_hash == previous.payload_hash


@pytest.mark.asyncio
async def test_epoch_rotation_fences_late_writes_and_keeps_handoff(
    context_database,
):
    store = AgentContextStore(context_database)
    old_ref = await _resolve(store)
    old_ref = (
        await store.append_transition(
            old_ref,
            event_type="message.assistant",
            operation_id="before-provider-fork",
            message_wire_v2=_wire("old epoch exact message"),
            provider="openai",
        )
    ).ref
    handoff_ref = await store.put_blob(
        {"messages": [_wire("portable exact tail")]}
    )
    new_ref = await store.fork_provider(
        old_ref,
        provider="anthropic",
        operation_id="provider-fork-1",
        portable_handoff_ref=handoff_ref,
    )

    assert new_ref.epoch == old_ref.epoch + 1
    current = await store.load_active(new_ref)
    assert [event.event_type for event in current.tail] == [
        "provider_handoff"
    ]
    assert current.tail[0].payload_ref == handoff_ref
    journal, next_cursor = await store.load_journal_page(new_ref)
    assert [event.event_type for event in journal] == [
        "message.assistant",
        "provider_handoff",
    ]
    assert next_cursor is None
    with pytest.raises(StaleEpochError):
        await store.append_transition(
            old_ref,
            event_type="assistant",
            operation_id="late-provider-response",
            message_wire_v2=_wire("late"),
        )
    # Retrying the already-committed rotation is idempotent even with the old
    # reference that it deliberately fenced.
    assert (
        await store.fork_provider(
            old_ref,
            provider="anthropic",
            operation_id="provider-fork-1",
            portable_handoff_ref=handoff_ref,
        )
    ) == new_ref


@pytest.mark.asyncio
async def test_epoch_rotation_rejects_conflicting_operation_id_reuse(
    context_database,
):
    store = AgentContextStore(context_database)
    old_ref = await _resolve(store)
    first_handoff = await store.put_blob(
        {"messages": [_wire("first portable handoff")]}
    )
    second_handoff = await store.put_blob(
        {"messages": [_wire("different portable handoff")]}
    )
    await store.fork_provider(
        old_ref,
        provider="anthropic",
        operation_id="provider-fork-conflict",
        portable_handoff_ref=first_handoff,
    )

    with pytest.raises(
        AgentContextError,
        match="context_operation_id_reuse_mismatch",
    ):
        await store.fork_provider(
            old_ref,
            provider="openai",
            operation_id="provider-fork-conflict",
            portable_handoff_ref=first_handoff,
        )
    with pytest.raises(
        AgentContextError,
        match="context_operation_id_reuse_mismatch",
    ):
        await store.fork_provider(
            old_ref,
            provider="anthropic",
            operation_id="provider-fork-conflict",
            portable_handoff_ref=second_handoff,
        )


@pytest.mark.asyncio
async def test_compaction_cas_keeps_concurrent_tail(context_database):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    for index in range(1, 4):
        result = await store.append_transition(
            ref,
            event_type="assistant",
            operation_id=f"response-{index}",
            message_wire_v2=_wire(f"message-{index}"),
            provider="openai",
        )
        ref = result.ref

    plan = await store.prepare_compaction(
        ref,
        operation_id="compact-prepare-1",
        provider="openai",
        strategy="native",
        covers_through_sequence=2,
    )
    concurrent = await store.append_transition(
        ref,
        event_type="assistant",
        operation_id="response-4",
        message_wire_v2=_wire("message-4"),
        provider="openai",
    )
    replay_ref = await store.put_blob(
        {"provider_output": ["opaque", "native", "items"]}
    )
    checkpoint = await store.commit_checkpoint(
        concurrent.ref,
        attempt_id=plan.attempt_id,
        operation_id="compact-commit-1",
        replay_payload_ref=replay_ref,
        active_token_count=120,
    )
    state = await store.load_active(concurrent.ref.model_copy(
        update={"revision": concurrent.ref.revision + 1}
    ))

    assert checkpoint.covers_through_sequence == 2
    assert checkpoint.source_hash == plan.source_hash
    assert [event.sequence for event in state.tail] == [3, 4]
    assert state.checkpoint == checkpoint
    assert state.active_token_count == 120


@pytest.mark.asyncio
async def test_failed_compaction_preserves_prior_active_state(
    context_database,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    for index in range(1, 3):
        ref = (
            await store.append_transition(
                ref,
                event_type="assistant",
                operation_id=f"failure-response-{index}",
                message_wire_v2=_wire(f"failure-{index}"),
            )
        ).ref
    before = await store.load_active(ref)
    plan = await store.prepare_compaction(
        ref,
        operation_id="failed-prepare",
        provider="openai",
        strategy="portable",
        covers_through_sequence=1,
    )
    await store.fail_compaction(
        attempt_id=plan.attempt_id,
        error_code="candidate_did_not_reduce_tokens",
    )
    after = await store.load_active(ref)
    assert after == before


@pytest.mark.asyncio
async def test_runtime_writer_blobs_payloads_and_reconstructs_portable_replay(
    context_database,
):
    store = AgentContextStore(context_database)
    writer = AgentContextTransitionWriter(store, await _resolve(store))
    first_wire = _wire("checkpoint-message-1")
    second_wire = _wire("checkpoint-message-2")
    first = await writer.append_transition(
        event_type="message.assistant",
        operation_id="writer-1",
        provider="openai",
        message_wire_v2=first_wire,
        payload={"usage": {"input_tokens": 10}},
    )
    second = await writer.append_transition(
        event_type="message.assistant",
        operation_id="writer-2",
        provider="openai",
        message_wire_v2=second_wire,
    )
    assert writer.ref == second.ref
    assert first.event.payload_ref is not None
    assert await store.get_blob(first.event.payload_ref) == {
        "usage": {"input_tokens": 10}
    }

    plan = await store.prepare_compaction(
        writer.ref,
        operation_id="writer-prepare",
        provider="openai",
        strategy="portable",
        covers_through_sequence=1,
    )
    replay_ref = await store.put_blob({"messages": [first_wire]})
    await store.commit_checkpoint(
        writer.ref,
        attempt_id=plan.attempt_id,
        operation_id="writer-commit",
        replay_payload_ref=replay_ref,
        active_token_count=20,
    )
    latest = writer.ref.model_copy(update={"revision": writer.ref.revision + 1})
    reconstructed_ref, wires = await reconstruct_message_wire_v2(
        store,
        latest,
    )
    assert reconstructed_ref.revision == latest.revision
    assert wires == [first_wire, second_wire]


@pytest.mark.asyncio
async def test_reconstruction_uses_latest_exact_request_snapshot(
    context_database,
):
    store = AgentContextStore(context_database)
    writer = AgentContextTransitionWriter(store, await _resolve(store))
    system = dict(
        message_to_wire(Message(role="system", content="exact system"))
    )
    user = dict(
        message_to_wire(Message(role="user", content="exact user"))
    )
    assistant = _wire("exact assistant")
    await writer.append_transition(
        event_type="request.snapshot",
        operation_id="snapshot-1",
        provider="openai",
        payload={"messages": [system, user], "tools": []},
    )
    await writer.append_transition(
        event_type="message.assistant",
        operation_id="assistant-after-snapshot",
        provider="openai",
        message_wire_v2=assistant,
    )
    # Metadata-only final events must not appear in provider replay.
    await writer.append_transition(
        event_type="response.final",
        operation_id="final-after-snapshot",
        provider="openai",
        payload={"finish_reason": "stop"},
    )

    _, wires = await reconstruct_message_wire_v2(store, writer.ref)
    assert wires == [system, user, assistant]


@pytest.mark.asyncio
async def test_provider_binding_is_opaque_and_archived_on_epoch_change(
    context_database,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    binding = await store.bind_provider(
        ref,
        provider="claude_code",
        binding_type="session_uuid",
        binding={"uuid": "secret-provider-session"},
        operation_id="bind-1",
    )

    assert "secret-provider-session" not in binding.model_dump_json()
    assert await store.get_blob(binding.binding_ref) == {
        "uuid": "secret-provider-session"
    }
    assert await store.load_provider_bindings(ref) == [binding]
    new_ref = await store.start_epoch(
        ref,
        provider="openai",
        operation_id="epoch-after-binding",
    )
    assert await store.load_provider_bindings(new_ref) == []


@pytest.mark.asyncio
async def test_provider_binding_rejects_conflicting_operation_id_reuse(
    context_database,
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    binding = await store.bind_provider(
        ref,
        provider="vertex",
        binding_type="interaction_id",
        binding={"interaction_id": "interaction-1"},
        operation_id="binding-conflict",
    )
    duplicate = await store.bind_provider(
        ref,
        provider="vertex",
        binding_type="interaction_id",
        binding={"interaction_id": "interaction-1"},
        operation_id="binding-conflict",
    )
    assert duplicate == binding

    conflicting_calls = (
        {
            "provider": "vertex",
            "binding_type": "interaction_id",
            "binding": {"interaction_id": "interaction-2"},
        },
        {
            "provider": "claude_code",
            "binding_type": "session_uuid",
            "binding": {"interaction_id": "interaction-1"},
        },
        {
            "provider": "vertex",
            "binding_type": "interaction_id",
            "binding": {"interaction_id": "interaction-1"},
            "fidelity": "observable_only",
        },
    )
    for call in conflicting_calls:
        with pytest.raises(
            AgentContextError,
            match="context_operation_id_reuse_mismatch",
        ):
            await store.bind_provider(
                ref,
                operation_id="binding-conflict",
                **call,
            )


@pytest.mark.asyncio
async def test_archive_fences_writes_and_purge_removes_state(context_database):
    store = AgentContextStore(context_database)
    retained_blob_ref = await store.put_blob({"pending_reference": True})
    ref = await _resolve(store)
    ref = (
        await store.append_transition(
            ref,
            event_type="assistant",
            operation_id="before-archive",
            message_wire_v2=_wire("preserved until purge"),
        )
    ).ref
    archived = await store.archive(ref, operation_id="archive-1")
    with pytest.raises((StaleEpochError, ContextArchivedError)):
        await store.append_transition(
            ref,
            event_type="assistant",
            operation_id="after-archive",
            message_wire_v2=_wire("must not commit"),
        )

    # Archive is recoverable and idempotent; purge is the separate,
    # explicitly destructive operation.
    assert (
        await store.archive(ref, operation_id="archive-1")
    ) == archived
    assert await store.purge(
        workflow_id=ref.workflow_id,
        context_node_id=ref.context_node_id,
        generation=ref.generation,
    ) == 1
    assert await store.get_blob(retained_blob_ref) == {
        "pending_reference": True
    }
    fresh = await _resolve(store)
    assert fresh.revision == 0
    assert (await store.load_active(fresh)).tail == []


@pytest.mark.asyncio
async def test_archive_context_defaults_to_every_generation(context_database):
    store = AgentContextStore(context_database)
    generation_two = await _resolve(store, execution_id="execution-g2")
    generation_three = await _resolve(
        store,
        generation=3,
        execution_id="execution-g3",
    )

    archived = await store.archive_context(
        workflow_id="workflow-1",
        context_node_id="workflow-1:context:1",
        operation_id="archive-all-generations",
    )
    assert {
        (ref.generation, ref.thread_id) for ref in archived
    } == {
        (2, generation_two.thread_id),
        (3, generation_three.thread_id),
    }
    for ref in archived:
        with pytest.raises(ContextArchivedError):
            await store.load_active(ref)


@pytest.mark.asyncio
async def test_authorized_handlers_paginate_raw_data_but_redact_exports(
    context_database,
    monkeypatch,
):
    await context_database.save_workflow(
        "workflow-1",
        "Context test",
        "Context_test",
        {
            "owner_id": "authenticated-user",
            "nodes": [
                {
                    "id": "workflow-1:context:1",
                    "type": "context",
                    "data": {"label": "Context"},
                }
            ],
            "edges": [],
        },
    )
    monkeypatch.setattr(
        context_handlers,
        "_database",
        lambda: context_database,
    )
    socket = SimpleNamespace(
        state=SimpleNamespace(user_id="authenticated-user"),
        scope={"path": "/ws/status"},
    )
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    secret_payload = {"resolved_system_instruction": "classified prompt"}
    first = await AgentContextTransitionWriter(
        store,
        ref,
    ).append_transition(
        event_type="request.snapshot",
        operation_id="handler-event-1",
        provider="openai",
        payload=secret_payload,
    )
    await store.append_transition(
        first.ref,
        event_type="message.assistant",
        operation_id="handler-event-2",
        provider="openai",
        message_wire_v2=_wire("classified response"),
    )

    page = await context_handlers.handle_get_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
            "view": "journal",
            "limit": 1,
        },
        socket,
    )
    assert page["success"] is True
    assert page["context"]["events"][0]["payload"] == secret_payload
    assert page["context"]["next_cursor"]
    second_page = await context_handlers.handle_get_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
            "view": "journal",
            "limit": 1,
            "cursor": page["context"]["next_cursor"],
        },
        socket,
    )
    assert [
        event["sequence"] for event in second_page["context"]["events"]
    ] == [2]

    exported = await context_handlers.handle_export_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
        },
        socket,
    )
    assert exported["success"] is True
    assert '"redacted": true' in exported["content"]
    assert "classified prompt" not in exported["content"]
    assert "classified response" not in exported["content"]
    assert "message_wire_v2" not in exported["content"]
    assert "payload_ref" not in exported["content"]

    unauthorized = await context_handlers.handle_get_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "another-context",
        },
        socket,
    )
    assert unauthorized["success"] is False
    wrong_owner = await context_handlers.handle_get_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
        },
        SimpleNamespace(
            state=SimpleNamespace(user_id="another-user"),
            scope={"path": "/ws/status"},
        ),
    )
    assert wrong_owner["success"] is False
    internal = await context_handlers.handle_get_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
        },
        SimpleNamespace(scope={"path": "/ws/internal"}),
    )
    assert internal["success"] is False


@pytest.mark.asyncio
async def test_clear_and_fork_handlers_rotate_epoch_with_metadata_events(
    context_database,
    monkeypatch,
):
    await context_database.save_workflow(
        "workflow-1",
        "Context lifecycle",
        "Context_lifecycle",
        {
            "nodes": [
                {
                    "id": "workflow-1:context:1",
                    "type": "context",
                    "data": {},
                }
            ],
            "edges": [],
        },
    )
    monkeypatch.setattr(
        context_handlers,
        "_database",
        lambda: context_database,
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(
        context_handlers,
        "dispatch_context_epoch_started",
        dispatch,
    )
    socket = SimpleNamespace(scope={"path": "/ws/status"})
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    ref = (
        await store.append_transition(
            ref,
            event_type="message.assistant",
            operation_id="lifecycle-event",
            provider="openai",
            message_wire_v2=_wire("before clear"),
        )
    ).ref

    cleared = await context_handlers.handle_clear_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
            "request_id": "clear-request-1",
        },
        socket,
    )
    assert cleared["success"] is True
    assert cleared["context"]["epoch"] == ref.epoch + 1
    current_ref = ref.model_copy(
        update={
            "epoch": cleared["context"]["epoch"],
            "revision": cleared["context"]["revision"],
        }
    )
    assert (await store.load_active(current_ref)).tail == []

    forked = await context_handlers.handle_fork_agent_context(
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
            "provider": "anthropic",
            "request_id": "fork-request-1",
        },
        socket,
    )
    assert forked["success"] is True
    assert forked["context"]["epoch"] == current_ref.epoch + 1
    latest = current_ref.model_copy(
        update={
            "epoch": forked["context"]["epoch"],
            "revision": forked["context"]["revision"],
        }
    )
    state = await store.load_active(latest)
    assert [event.event_type for event in state.tail] == [
        "provider_handoff"
    ]
    assert dispatch.await_count == 2
    assert dispatch.await_args_list[0].kwargs["reason"] == "clear"
    assert dispatch.await_args_list[1].kwargs["reason"] == "fork"
