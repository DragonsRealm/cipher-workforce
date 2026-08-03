"""Durability/security contracts for the explicit Simple Memory tool."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.memory.tool_store import (
    MemoryNotFoundError,
    MemoryScope,
    MemoryToolStore,
    MemoryVersionConflictError,
)


@pytest.fixture
async def memory_database():
    # Root conftest stubs core.database for fast unit tests. Load the real
    # implementation privately, matching test_runtime_mutations.py.
    module_name = f"tests._real_memory_tool_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[3] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".memory-tool-{uuid.uuid4().hex}.db"
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


async def test_namespace_isolation_and_durable_idempotency(memory_database):
    store = MemoryToolStore(memory_database)
    scope_a = MemoryScope("owner", "workflow", "memory-a")
    scope_b = MemoryScope("owner", "workflow", "memory-b")

    first = await store.remember(
        scope_a,
        content="Production database is in Mumbai",
        tags=["infra"],
        operation_id="tool-call-1",
    )
    replay = await store.remember(
        scope_a,
        content="This retry must not be inserted",
        operation_id="tool-call-1",
    )

    assert first["receipt"]["applied"] is True
    assert replay["receipt"]["applied"] is False
    assert replay["memory"]["id"] == first["memory"]["id"]
    assert (await store.list(scope_a))["count"] == 1
    assert (await store.list(scope_b))["count"] == 0


async def test_lexical_recall_survives_embedding_failure(memory_database):
    async def broken_embedder(_content: str):
        raise RuntimeError("embedding provider unavailable")

    store = MemoryToolStore(memory_database, embedder=broken_embedder)
    scope = MemoryScope("owner", "workflow", "memory")
    remembered = await store.remember(
        scope,
        content="Customer prefers quarterly PDF invoices",
        category="preference",
        tags=["billing"],
        operation_id="remember-invoice",
    )

    assert remembered["memory"]["indexing_state"] == "embedding_failed"
    recalled = await store.recall(
        scope,
        query="quarterly invoices",
        categories=["preference"],
        tags=["billing"],
    )
    assert recalled["count"] == 1
    assert recalled["items"][0]["id"] == remembered["memory"]["id"]
    assert recalled["retrieval"] in {"fts", "sql"}


async def test_optimistic_update_and_forget(memory_database):
    store = MemoryToolStore(memory_database)
    scope = MemoryScope("owner", "workflow", "memory")
    created = await store.remember(
        scope,
        content="Release is Tuesday",
        operation_id="remember-release",
    )
    memory_id = created["memory"]["id"]

    updated = await store.update(
        scope,
        memory_id=memory_id,
        expected_version=1,
        patch={"content": "Release is Wednesday", "tags": ["release"]},
        operation_id="update-release",
    )
    assert updated["memory"]["version"] == 2
    assert updated["memory"]["content"] == "Release is Wednesday"

    with pytest.raises(MemoryVersionConflictError):
        await store.update(
            scope,
            memory_id=memory_id,
            expected_version=1,
            patch={"content": "stale"},
            operation_id="stale-update",
        )

    await store.forget(
        scope,
        memory_id=memory_id,
        expected_version=2,
        operation_id="forget-release",
    )
    with pytest.raises(MemoryNotFoundError):
        await store.get(scope, memory_id)


async def test_expiry_pagination_and_reset_clear(memory_database):
    store = MemoryToolStore(memory_database)
    scope = MemoryScope("owner", "workflow", "memory")
    await store.remember(
        scope,
        content="already expired",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        operation_id="expired",
    )
    for index in range(3):
        await store.remember(
            scope,
            content=f"active-{index}",
            operation_id=f"active-{index}",
        )

    first_page = await store.list(scope, limit=2)
    second_page = await store.list(
        scope, limit=2, cursor=first_page["next_cursor"]
    )
    assert first_page["count"] == 2
    assert second_page["count"] == 1
    assert all(
        item["content"] != "already expired"
        for item in [*first_page["items"], *second_page["items"]]
    )

    cleared = await store.clear_namespace(
        scope, operation_id="workflow-reset"
    )
    assert cleared["cleared"] == 4
    assert (await store.list(scope))["count"] == 0
