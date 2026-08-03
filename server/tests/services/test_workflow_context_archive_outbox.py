from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.agent_context import AgentContextStore


@pytest.fixture
async def outbox_database():
    # The root test configuration replaces core.database for fast contract
    # tests. Load the real implementation privately for transaction coverage.
    module_name = f"tests._real_workflow_outbox_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[2] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".workflow-outbox-{uuid.uuid4().hex}.db"
    database = module.Database(
        SimpleNamespace(
            database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
            database_echo=False,
            database_pool_size=5,
            database_max_overflow=5,
        )
    )
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


def _graph(*, with_context: bool) -> dict:
    return {
        "graphVersion": 2,
        "owner_id": "owner",
        "nodes": (
            [
                {
                    "id": "ctx",
                    "type": "context",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "label": "Context",
                        "systemManaged": True,
                        "agentNodeId": "agent",
                    },
                }
            ]
            if with_context
            else []
        ),
        "edges": [],
    }


@pytest.mark.asyncio
async def test_graph_save_and_archive_intent_commit_atomically(
    outbox_database,
):
    database = outbox_database
    assert await database.save_workflow(
        "1",
        "First",
        "first",
        _graph(with_context=True),
    )
    assert await database.save_workflow(
        "2",
        "Second",
        "second",
        _graph(with_context=False),
    )

    # The conflicting slug makes the workflow UPDATE fail. Its archive intent
    # must roll back in the same transaction, leaving Context still referenced.
    assert not await database.save_workflow(
        "1",
        "First",
        "second",
        _graph(with_context=False),
    )
    persisted = await database.get_workflow("1")
    assert persisted is not None
    assert [node["id"] for node in persisted.data["nodes"]] == ["ctx"]
    assert await database.list_workflow_context_archive_outbox("1") == []

    assert await database.save_workflow(
        "1",
        "First",
        "first",
        _graph(with_context=False),
    )
    pending = await database.list_workflow_context_archive_outbox("1")
    assert len(pending) == 1
    assert pending[0]["context_node_id"] == "ctx"


@pytest.mark.asyncio
async def test_context_id_canonicalization_is_not_a_deletion(
    outbox_database,
):
    database = outbox_database
    assert await database.save_workflow(
        "1",
        "Workflow",
        "workflow",
        _graph(with_context=True),
    )
    canonical = _graph(with_context=True)
    canonical["nodes"][0]["id"] = "1:context:1"

    assert await database.save_workflow(
        "1",
        "Workflow",
        "workflow",
        canonical,
        context_id_aliases={"ctx": "1:context:1"},
    )

    assert (
        await database.list_workflow_context_archive_outbox("1")
        == []
    )


@pytest.mark.asyncio
async def test_atomic_canvas_mutation_enqueues_context_removal(
    outbox_database,
):
    database = outbox_database
    assert await database.save_workflow(
        "1",
        "Workflow",
        "workflow",
        _graph(with_context=True),
    )

    _, metadata, applied = await database.mutate_workflow_data_atomic(
        "1",
        lambda _: (
            _graph(with_context=False),
            {"removed": "ctx"},
        ),
        mutation_id="remove-context",
        operation="test_remove_context",
    )

    assert applied is True
    assert metadata == {"found": True, "removed": "ctx"}
    pending = await database.list_workflow_context_archive_outbox("1")
    assert len(pending) == 1
    assert pending[0]["context_node_id"] == "ctx"


@pytest.mark.asyncio
async def test_pending_archive_recovers_on_workflow_read(
    outbox_database,
    monkeypatch,
):
    from services.workflow_storage import handlers

    database = outbox_database
    assert await database.save_workflow(
        "1",
        "Workflow",
        "workflow",
        _graph(with_context=True),
    )
    store = AgentContextStore(database)
    await store.resolve_thread(
        workflow_id="1",
        context_node_id="ctx",
        generation=1,
        session_id="session",
    )

    # Simulate a process crash after graph commit and before the outbox drain.
    assert await database.save_workflow(
        "1",
        "Workflow",
        "workflow",
        _graph(with_context=False),
    )
    assert len(await database.list_workflow_context_archive_outbox("1")) == 1

    real_store = AgentContextStore

    class _FailingStore:
        def __init__(self, database):
            self.database = database

        async def archive_context(self, **kwargs):
            raise RuntimeError("simulated_archive_crash")

    monkeypatch.setattr(
        "services.agent_context.AgentContextStore",
        _FailingStore,
    )
    monkeypatch.setattr(handlers.container, "database", lambda: database)
    first = await handlers.handle_get_workflow(
        {"workflow_id": "1"},
        websocket=None,
    )
    assert first["context_archives_pending"] == 1
    assert (
        await store.list_threads(
            workflow_id="1",
            context_node_id="ctx",
            include_archived=True,
        )
    )[0].status == "active"

    monkeypatch.setattr(
        "services.agent_context.AgentContextStore",
        real_store,
    )
    recovered = await handlers.handle_get_workflow(
        {"workflow_id": "1"},
        websocket=None,
    )
    assert recovered["context_archives_completed"] == 1
    assert recovered["context_archives_pending"] == 0
    assert await database.list_workflow_context_archive_outbox("1") == []
    assert (
        await store.list_threads(
            workflow_id="1",
            context_node_id="ctx",
            include_archived=True,
        )
    )[0].status == "archived"


@pytest.mark.asyncio
async def test_workflow_delete_commits_tombstone_then_archives(
    outbox_database,
):
    from services.workflow_storage.handlers import (
        delete_workflow_with_context_archival,
    )

    database = outbox_database
    assert await database.save_workflow(
        "1",
        "Workflow",
        "workflow",
        _graph(with_context=True),
    )
    store = AgentContextStore(database)
    await store.resolve_thread(
        workflow_id="1",
        context_node_id="ctx",
        generation=1,
        session_id="session",
    )

    result = await delete_workflow_with_context_archival(
        database,
        "1",
    )

    assert result == {
        "success": True,
        "workflow_id": "1",
        "contexts_archived": 1,
        "context_archives_pending": 0,
    }
    assert await database.get_workflow("1") is None
    assert (
        await database.list_workflow_context_archive_outbox("1")
        == []
    )
    assert (
        await store.list_threads(
            workflow_id="1",
            context_node_id="ctx",
            include_archived=True,
        )
    )[0].status == "archived"


@pytest.mark.asyncio
async def test_failed_workflow_delete_rolls_back_archive_tombstone(
    outbox_database,
    monkeypatch,
):
    database = outbox_database
    database_module = sys.modules[database.__class__.__module__]
    monkeypatch.setattr(
        database_module.secrets,
        "token_hex",
        lambda _: "fixed",
    )

    # Reserve the deterministic outbox primary key.
    assert await database.save_workflow(
        "1",
        "First",
        "first",
        _graph(with_context=True),
    )
    assert await database.save_workflow(
        "1",
        "First",
        "first",
        _graph(with_context=False),
    )

    assert await database.save_workflow(
        "2",
        "Second",
        "second",
        _graph(with_context=True),
    )
    store = AgentContextStore(database)
    await store.resolve_thread(
        workflow_id="2",
        context_node_id="ctx",
        generation=1,
        session_id="session",
    )

    # Enqueuing workflow 2's archive collides with the existing outbox ID.
    # The workflow DELETE and tombstone INSERT must both roll back.
    assert not await database.delete_workflow("2")
    persisted = await database.get_workflow("2")
    assert persisted is not None
    assert [node["id"] for node in persisted.data["nodes"]] == ["ctx"]
    assert (
        await database.list_workflow_context_archive_outbox("2")
        == []
    )
    assert (
        await store.list_threads(
            workflow_id="2",
            context_node_id="ctx",
            include_archived=True,
        )
    )[0].status == "active"
