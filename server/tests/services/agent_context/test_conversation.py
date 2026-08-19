"""Contract for the plain conversation store.

One ``agent_conversations`` row per ``(workflow_id, generation, agent_node_id)``.
These tests lock the properties every writer relies on:

* load returns exactly what save committed, per key, isolated across keys
* save is an upsert — the whole message list replaces the stored one
* a save notifies listeners only after the durable commit
* a failing listener can never fail the save
* clear narrows by generation/agent and reports the deleted row count
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.agent_context import (
    clear_conversation,
    list_conversations,
    load_conversation,
    save_conversation,
)
from services.agent_context import listeners as listener_module
from services.llm.protocol import Message, message_to_wire


@pytest.fixture
async def conversation_database():
    # The root conftest stubs core.database for fast plugin tests, so load
    # the real module privately for transaction coverage.
    module_name = f"tests._conversation_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[3] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".conversation-{uuid.uuid4().hex}.db"
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


def _wire(role: str, content: str) -> dict:
    return dict(message_to_wire(Message(role=role, content=content)))


_KEY = {
    "workflow_id": "workflow-1",
    "generation": 2,
    "agent_node_id": "1:aiAgent:1",
}


def _sans_ts(messages: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if k != "ts"} for m in messages]


@pytest.mark.asyncio
async def test_load_returns_exactly_what_save_committed(
    conversation_database, isolated_listeners
):
    messages = [_wire("user", "hello"), _wire("assistant", "hi")]
    await save_conversation(
        conversation_database, **_KEY, messages=messages
    )
    stored = await load_conversation(conversation_database, **_KEY)
    # Content round-trips exactly; the store adds a view-only `ts` stamp.
    assert _sans_ts(stored) == messages
    assert all(m.get("ts") for m in stored)


@pytest.mark.asyncio
async def test_missing_key_loads_empty_not_error(conversation_database):
    assert await load_conversation(conversation_database, **_KEY) == []


@pytest.mark.asyncio
async def test_save_is_an_upsert_replacing_the_whole_list(
    conversation_database, isolated_listeners
):
    await save_conversation(
        conversation_database, **_KEY, messages=[_wire("user", "one")]
    )
    replacement = [
        _wire("user", "one"),
        _wire("assistant", "two"),
        _wire("user", "three"),
    ]
    await save_conversation(
        conversation_database, **_KEY, messages=replacement
    )
    assert (
        _sans_ts(await load_conversation(conversation_database, **_KEY))
        == replacement
    )
    rows = await list_conversations(
        conversation_database, workflow_id=_KEY["workflow_id"]
    )
    assert len(rows) == 1
    assert rows[0]["message_count"] == 3


@pytest.mark.asyncio
async def test_timestamps_stamp_new_messages_and_preserve_old_ones(
    conversation_database, isolated_listeners
):
    """Callers regenerate wires each turn (no stamps of their own), so the
    store keeps the original ``ts`` for the unchanged prefix and stamps
    only the appended turn — a message's time is when it first persisted,
    not when the conversation last saved."""

    first_turn = [_wire("user", "one"), _wire("assistant", "two")]
    await save_conversation(
        conversation_database, **_KEY, messages=first_turn
    )
    first_stored = await load_conversation(conversation_database, **_KEY)
    original_stamps = [m["ts"] for m in first_stored]

    # Next turn re-sends the SAME prefix (fresh wires, no ts) + new tail.
    second_turn = [*first_turn, _wire("user", "three")]
    await save_conversation(
        conversation_database, **_KEY, messages=second_turn
    )
    second_stored = await load_conversation(conversation_database, **_KEY)
    assert [m["ts"] for m in second_stored[:2]] == original_stamps
    assert second_stored[2]["ts"]
    assert _sans_ts(second_stored) == second_turn


@pytest.mark.asyncio
async def test_keys_are_isolated_across_generation_and_agent(
    conversation_database, isolated_listeners
):
    await save_conversation(
        conversation_database, **_KEY, messages=[_wire("user", "gen2")]
    )
    await save_conversation(
        conversation_database,
        workflow_id="workflow-1",
        generation=3,
        agent_node_id=_KEY["agent_node_id"],
        messages=[_wire("user", "gen3")],
    )
    await save_conversation(
        conversation_database,
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="1:aiAgent:2",
        messages=[_wire("user", "other agent")],
    )
    assert (await load_conversation(conversation_database, **_KEY))[0][
        "content"
    ] == "gen2"
    rows = await list_conversations(
        conversation_database, workflow_id="workflow-1"
    )
    assert len(rows) == 3
    # Newest generation first.
    assert rows[0]["generation"] == 3


@pytest.mark.asyncio
async def test_non_dict_messages_are_dropped_not_saved(
    conversation_database, isolated_listeners
):
    await save_conversation(
        conversation_database,
        **_KEY,
        messages=[_wire("user", "kept"), "not a message", None],
    )
    stored = await load_conversation(conversation_database, **_KEY)
    assert [m["content"] for m in stored] == ["kept"]


@pytest.mark.asyncio
async def test_save_notifies_after_commit_with_identity_and_count(
    conversation_database, isolated_listeners
):
    observed: list[dict] = []

    async def listener(**kwargs):
        # The row must already be durable when the listener fires.
        stored = await load_conversation(conversation_database, **_KEY)
        kwargs["stored_count"] = len(stored)
        observed.append(kwargs)

    isolated_listeners.register_conversation_listener(listener)
    await save_conversation(
        conversation_database,
        **_KEY,
        messages=[_wire("user", "a"), _wire("assistant", "b")],
    )
    assert observed == [
        {
            "workflow_id": "workflow-1",
            "generation": 2,
            "agent_node_id": "1:aiAgent:1",
            "message_count": 2,
            "stored_count": 2,
        }
    ]


@pytest.mark.asyncio
async def test_listener_failure_cannot_fail_the_save(
    conversation_database, isolated_listeners
):
    async def broken(**kwargs):
        raise RuntimeError("listener exploded")

    isolated_listeners.register_conversation_listener(broken)
    await save_conversation(
        conversation_database, **_KEY, messages=[_wire("user", "still saved")]
    )
    assert len(await load_conversation(conversation_database, **_KEY)) == 1


@pytest.mark.asyncio
async def test_no_listeners_registered_is_a_no_op(
    conversation_database, isolated_listeners
):
    await save_conversation(
        conversation_database, **_KEY, messages=[_wire("user", "quiet")]
    )
    assert len(await load_conversation(conversation_database, **_KEY)) == 1


@pytest.mark.asyncio
async def test_concurrent_saves_to_one_key_serialize(
    conversation_database, isolated_listeners
):
    async def writer(n: int):
        await save_conversation(
            conversation_database,
            **_KEY,
            messages=[_wire("user", f"turn-{n}")],
        )

    await asyncio.gather(*(writer(n) for n in range(8)))
    stored = await load_conversation(conversation_database, **_KEY)
    # Last-writer-wins per whole-list upsert; the row is intact and single.
    assert len(stored) == 1
    rows = await list_conversations(
        conversation_database, workflow_id=_KEY["workflow_id"]
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_clear_narrows_by_generation_and_agent(
    conversation_database, isolated_listeners
):
    for generation in (1, 2):
        for agent in ("agent-a", "agent-b"):
            await save_conversation(
                conversation_database,
                workflow_id="workflow-1",
                generation=generation,
                agent_node_id=agent,
                messages=[_wire("user", "x")],
            )
    cleared = await clear_conversation(
        conversation_database,
        workflow_id="workflow-1",
        generation=1,
        agent_node_id="agent-a",
    )
    assert cleared == 1
    cleared = await clear_conversation(
        conversation_database, workflow_id="workflow-1", generation=2
    )
    assert cleared == 2
    cleared = await clear_conversation(
        conversation_database, workflow_id="workflow-1"
    )
    assert cleared == 1
    assert (
        await list_conversations(
            conversation_database, workflow_id="workflow-1"
        )
        == []
    )


def test_store_never_imports_the_plugin():
    """The store must stay importable without ``nodes/``; the plugin
    registers its broadcaster through the listener registry instead."""

    import inspect

    from services.agent_context import conversation, listeners

    for module in (conversation, listeners):
        source = inspect.getsource(module)
        assert "from nodes" not in source
        assert "import nodes" not in source
