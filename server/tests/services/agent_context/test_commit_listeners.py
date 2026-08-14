"""Contract for the Context commit-notification fanout.

The store is the single place every Context writer passes through, so it is
where the "thread advanced" notification is emitted. These tests lock the
three properties that make that safe:

* a notification is emitted only after durable commit, carrying post-commit state
* an idempotent replay emits nothing (a replay is not a state change)
* a failing listener can never fail, slow, or roll back the commit
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from models.agent_context import AgentContextRef
from services.agent_context import AgentContextStore
from services.agent_context import listeners as listener_module
from services.llm.protocol import Message, message_to_wire


@pytest.fixture
async def context_database():
    # Mirrors tests/services/agent_context/test_store.py: the root conftest
    # stubs core.database for fast plugin tests, so load the real module
    # privately for transaction coverage.
    module_name = f"tests._commit_listener_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[3] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".commit-listener-{uuid.uuid4().hex}.db"
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


@pytest.fixture
def isolated_listeners():
    """Swap the process-wide fanout for an empty one, then restore it.

    The Context plugin registers its broadcaster at import time, so without
    this every test in this file would also hit a real WebSocket broadcast.
    """

    original = list(listener_module._LISTENERS)
    listener_module._LISTENERS.clear()
    try:
        yield listener_module
    finally:
        listener_module._LISTENERS.clear()
        listener_module._LISTENERS.extend(original)


def _wire(content: str) -> dict:
    return dict(message_to_wire(Message(role="assistant", content=content)))


async def _resolve(store: AgentContextStore, **overrides) -> AgentContextRef:
    values = {
        "workflow_id": "workflow-1",
        "context_node_id": "workflow-1:context:1",
        "generation": 2,
        "execution_id": "execution-1",
    }
    values.update(overrides)
    return await store.resolve_thread(**values)


def _recorder(sink: list) -> callable:
    async def _listener(**kwargs):
        sink.append(kwargs)

    return _listener


@pytest.mark.asyncio
async def test_append_notifies_with_post_commit_state(
    context_database, isolated_listeners
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    seen: list = []
    isolated_listeners.register_context_commit_listener(_recorder(seen))

    result = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="op-1",
        message_wire=_wire("hello"),
        provider="anthropic",
    )

    assert result.applied is True
    assert len(seen) == 1
    notified = seen[0]
    # The notification must describe committed state, never a projection of
    # it: same revision the caller got back, and the sequence just written.
    assert notified["ref"] == result.ref
    assert notified["ref"].revision == result.ref.revision
    assert notified["provider"] == "anthropic"
    assert notified["sequence"] == result.event.sequence


@pytest.mark.asyncio
async def test_idempotent_replay_emits_nothing(
    context_database, isolated_listeners
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    seen: list = []
    isolated_listeners.register_context_commit_listener(_recorder(seen))

    wire = _wire("hello")
    first = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="op-replay",
        message_wire=wire,
    )
    replay = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="op-replay",
        message_wire=wire,
    )

    assert first.applied is True
    assert replay.applied is False
    # A retried activity is not a state change. Broadcasting here would wake
    # every open panel for something that did not happen.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_listener_failure_cannot_fail_the_commit(
    context_database, isolated_listeners
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)

    async def _explode(**_kwargs):
        raise RuntimeError("listener is broken")

    survived: list = []
    isolated_listeners.register_context_commit_listener(_explode)
    isolated_listeners.register_context_commit_listener(_recorder(survived))

    result = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="op-boom",
        message_wire=_wire("hello"),
    )

    # The commit stands, and one broken listener does not starve the others.
    assert result.applied is True
    assert len(survived) == 1
    active = await store.load_active(result.ref)
    assert len(active.tail) == 1


@pytest.mark.asyncio
async def test_pressure_update_notifies_once_per_commit(
    context_database, isolated_listeners
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)
    seen: list = []
    isolated_listeners.register_context_commit_listener(_recorder(seen))

    updated = await store.record_active_pressure(
        ref,
        operation_id="pressure-1",
        active_token_count=1234,
    )
    await store.record_active_pressure(
        ref,
        operation_id="pressure-1",
        active_token_count=1234,
    )

    assert len(seen) == 1
    assert seen[0]["active_token_count"] == 1234
    assert seen[0]["ref"].revision == updated.revision
    # Pressure is not a journal append, so there is no sequence to report.
    assert seen[0]["sequence"] is None


@pytest.mark.asyncio
async def test_no_listeners_registered_is_a_no_op(
    context_database, isolated_listeners
):
    store = AgentContextStore(context_database)
    ref = await _resolve(store)

    result = await store.append_transition(
        ref,
        event_type="message.assistant",
        operation_id="op-quiet",
        message_wire=_wire("hello"),
    )

    assert result.applied is True
