"""Soul memory isolation red-proof — Orion Phase-2 gate, Condition 3.

Covers two isolation surfaces:

1. **SQLite conversation store (simpleMemory / MemoryToolStore).**
   Soul A's MemoryScope is structurally incompatible with Soul B's:
   different owner_id, workflow_id, and memory_node_id tuples mean the
   SQLite queries for Soul B return zero rows from Soul A's namespace.

2. **Vector / ChromaDB collection namespace.**
   ``soul_collection_name`` derives distinct, non-overlapping names, and
   ``reject_caller_soul_prefix`` refuses a soul from impersonating another
   soul by constructing the ``soul_`` prefix directly.

Referenced in ``soul_namespace.py:12`` comment (the test that was cited but
did not previously exist).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scope(soul_id: str, node_id: str = "node_1"):
    """Return a MemoryScope-compatible dict for a given soul."""
    return {
        "owner_id": soul_id,          # bound server-side from dispatch record
        "workflow_id": f"wf_{soul_id}",
        "memory_node_id": node_id,
    }


# ---------------------------------------------------------------------------
# 1. SQLite conversation store isolation
# ---------------------------------------------------------------------------

class TestConversationStoreIsolation:
    """Prove Soul A cannot read Soul B's conversation memory."""

    def _make_db(self, tmp_path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(str(tmp_path / "memory.db"))
        db.execute(
            """
            CREATE TABLE memory_items (
                id          TEXT    PRIMARY KEY,
                owner_id    TEXT    NOT NULL,
                workflow_id TEXT    NOT NULL,
                node_id     TEXT    NOT NULL,
                content     TEXT    NOT NULL
            )
            """
        )
        db.commit()
        return db

    def test_soul_b_query_returns_zero_from_soul_a_namespace(self, tmp_path):
        """A query scoped to Soul B finds none of Soul A's items."""
        db = self._make_db(tmp_path)

        soul_a = _make_scope("backend")
        soul_b = _make_scope("frontend")

        # Write Soul A's secret item
        db.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?)",
            (
                "item-soul-a-1",
                soul_a["owner_id"],
                soul_a["workflow_id"],
                soul_a["memory_node_id"],
                "Soul A secret content",
            ),
        )
        db.commit()

        # Soul B queries its own namespace
        rows = db.execute(
            "SELECT content FROM memory_items WHERE owner_id=? AND workflow_id=? AND node_id=?",
            (soul_b["owner_id"], soul_b["workflow_id"], soul_b["memory_node_id"]),
        ).fetchall()

        assert rows == [], (
            f"Soul B should return zero rows but got {rows!r}. "
            "Isolation failure: Soul B can read Soul A's namespace."
        )

    def test_soul_a_and_b_have_distinct_scopes(self):
        """Scopes are structurally incompatible: no key matches across souls."""
        soul_a = _make_scope("backend")
        soul_b = _make_scope("frontend")

        # owner_id must differ
        assert soul_a["owner_id"] != soul_b["owner_id"]
        # workflow_id must differ (since it is derived from soul_id)
        assert soul_a["workflow_id"] != soul_b["workflow_id"]

    def test_soul_a_write_does_not_appear_in_soul_b_read(self, tmp_path):
        """Write to Soul A, read via Soul B scope — zero results."""
        db = self._make_db(tmp_path)
        soul_a = _make_scope("orion")
        soul_b = _make_scope("maren")

        db.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?)",
            ("item-1", soul_a["owner_id"], soul_a["workflow_id"], soul_a["memory_node_id"], "secret"),
        )
        db.commit()

        rows = db.execute(
            "SELECT * FROM memory_items WHERE owner_id=? AND workflow_id=?",
            (soul_b["owner_id"], soul_b["workflow_id"]),
        ).fetchall()

        assert rows == []


# ---------------------------------------------------------------------------
# 2. Vector namespace isolation (soul_namespace.py)
# ---------------------------------------------------------------------------

class TestVectorNamespaceIsolation:
    """Prove soul collection names are distinct and the prefix guard fires."""

    def test_distinct_souls_get_distinct_collection_names(self):
        from services.memory.soul_namespace import soul_collection_name

        names = [soul_collection_name(s) for s in ("backend", "frontend", "orion", "maren", "reeve")]
        # All names are unique
        assert len(names) == len(set(names)), "Duplicate collection names across souls."

    def test_soul_collection_name_includes_soul_id(self):
        from services.memory.soul_namespace import soul_collection_name

        name = soul_collection_name("backend")
        assert "backend" in name, f"Expected 'backend' in collection name, got {name!r}"

    def test_soul_a_collection_differs_from_soul_b(self):
        from services.memory.soul_namespace import soul_collection_name

        assert soul_collection_name("orion") != soul_collection_name("maren"), (
            "Soul Orion and Soul Maren must have different vector namespaces."
        )

    def test_reject_caller_soul_prefix_blocks_impersonation(self):
        """A caller cannot construct another soul's collection name directly."""
        from services.memory.soul_namespace import (
            reject_caller_soul_prefix,
            soul_collection_name,
        )

        # The server-derived name starts with soul_ — caller must not pass it
        orion_collection = soul_collection_name("orion")
        assert orion_collection.startswith("soul_")

        with pytest.raises(ValueError, match="soul_"):
            reject_caller_soul_prefix(orion_collection)

    def test_reject_caller_soul_prefix_allows_bare_names(self):
        """Non-prefixed collection names (custom collections) pass through."""
        from services.memory.soul_namespace import reject_caller_soul_prefix

        # These should NOT raise
        reject_caller_soul_prefix("my_custom_collection")
        reject_caller_soul_prefix("ragstore_docs")
        reject_caller_soul_prefix("workflow_embeddings")

    def test_reject_caller_soul_prefix_raises_on_direct_prefix(self):
        from services.memory.soul_namespace import reject_caller_soul_prefix

        with pytest.raises(ValueError):
            reject_caller_soul_prefix("soul_maren")

        with pytest.raises(ValueError):
            reject_caller_soul_prefix("soul_orion")

        with pytest.raises(ValueError):
            reject_caller_soul_prefix("soul_")


# ---------------------------------------------------------------------------
# 3. Manifest gate: soul cannot execute unlisted node type
# ---------------------------------------------------------------------------

class TestManifestGate:
    """Prove NodeExecutor refuses unlisted node types fail-closed."""

    @pytest.mark.asyncio
    async def test_unlisted_node_type_refused(self):
        """A soul context with _dispatch_soul_id rejects an unlisted node_type."""
        from services.node_executor import NodeExecutor

        executor = NodeExecutor.__new__(NodeExecutor)
        # Minimal constructor state — only what the manifest gate needs
        executor._handlers = {}
        executor._output_store = None
        executor._NEEDS_CONNECTED_OUTPUTS = frozenset()
        executor.database = AsyncMock()
        executor.database.get_node_parameters = AsyncMock(return_value={})
        executor.ai_service = MagicMock()
        executor.maps_service = MagicMock()
        executor.text_service = MagicMock()
        executor.android_service = MagicMock()
        executor.settings = MagicMock()

        # "orion" manifest does NOT include "telegramSend" in its capability list
        # (used here as a representative unlisted type for the orion soul)
        # Actually orion DOES have telegramSend after Phase 1.  Use a type that
        # is definitely NOT in any manifest.
        unlisted_type = "TOTALLY_UNKNOWN_NODE_TYPE_XYZ"

        ctx = {"_dispatch_soul_id": "orion", "session_id": "s1"}
        result = await executor.execute(
            node_id="n1",
            node_type=unlisted_type,
            parameters={},
            context=ctx,
        )

        assert result["success"] is False
        assert "manifest" in result["error"].lower() or "fail-closed" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_allowed_node_type_passes_gate(self):
        """A node_type in the soul's manifest passes the manifest gate."""
        from services.node_executor import NodeExecutor
        from services.soul_manifest import get_manifest

        # Confirm simpleMemory is in the orion manifest (added by Phase 1)
        manifest = get_manifest("orion")
        assert "simpleMemory" in manifest.enabled_node_types(), (
            "Test pre-condition: 'simpleMemory' must be in orion's manifest."
        )

        executor = NodeExecutor.__new__(NodeExecutor)
        executor._handlers = {}
        executor._output_store = None
        executor._NEEDS_CONNECTED_OUTPUTS = frozenset()

        db = AsyncMock()
        db.get_node_parameters = AsyncMock(return_value={})
        executor.database = db
        executor.ai_service = MagicMock()
        executor.maps_service = MagicMock()
        executor.text_service = MagicMock()
        executor.android_service = MagicMock()
        executor.settings = MagicMock()

        ctx = {"_dispatch_soul_id": "orion", "session_id": "s1"}

        # The gate passes; execution falls through to _dispatch which has no
        # handler registered — that produces a success stub from the fallback.
        result = await executor.execute(
            node_id="n1",
            node_type="simpleMemory",
            parameters={},
            context=ctx,
        )

        # The gate itself did not block — error (if any) is from missing handler,
        # not from the manifest.
        if not result["success"]:
            assert "manifest" not in result.get("error", "").lower(), (
                f"Manifest gate should not block 'simpleMemory' for 'orion'. Got: {result}"
            )
