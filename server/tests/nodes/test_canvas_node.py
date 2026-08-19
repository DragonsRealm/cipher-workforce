"""Canvas node — board store, display op, panel handlers, event contract.

Mirrors the named precedents:
- store fixture shape: tests/services/memory/test_tool_store.py (private real
  core.database load — the root conftest stubs it for unit speed)
- handler security: tests/services/test_tool_input_security.py (internal
  socket denial / owner mismatch / node-type check)
- event identity lock: tests/nodes/test_write_todos_handlers.py
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from nodes.tool.canvas import (
    CanvasNode,
    CanvasParams,
    _collect_connected_refs,
)
from nodes.tool.canvas._events import canvas_updated
from nodes.tool.canvas._handlers import (
    handle_canvas_clear,
    handle_canvas_list,
    handle_canvas_remove,
)
from nodes.tool.canvas._store import (
    CANVAS_NOTE_MAX_BYTES,
    CanvasScope,
    CanvasStore,
    CanvasStoreError,
    truncate_note,
)
from services.plugin import NodeContext, NodeUserError


# ---------------------------------------------------------------------------
# Real-database fixture (root conftest stubs core.database; load it privately)
# ---------------------------------------------------------------------------


@pytest.fixture
async def canvas_database():
    module_name = f"tests._real_canvas_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[2] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".canvas-{uuid.uuid4().hex}.db"
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


def _scope(node_id: str = "canvas-1", workflow_id: str = "wf-1") -> CanvasScope:
    return CanvasScope(
        owner_id="owner", workflow_id=workflow_id, node_id=node_id
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


async def test_store_scope_isolation_between_nodes(canvas_database):
    store = CanvasStore(canvas_database)
    a, b = _scope("canvas-a"), _scope("canvas-b")

    await store.append(a, [{"kind": "note", "content": "only on a"}])

    assert len((await store.list(a))["items"]) == 1
    assert (await store.list(b)) == {"items": [], "revision": 0}


async def test_store_append_replace_and_revision_monotonicity(canvas_database):
    store = CanvasStore(canvas_database)
    scope = _scope()

    _, r1, total1 = await store.append(
        scope, [{"kind": "note", "content": "one"}]
    )
    _, r2, total2 = await store.append(
        scope, [{"kind": "url", "url": "https://example.com"}]
    )
    assert (r1, total1) == (1, 1)
    assert (r2, total2) == (2, 2)

    added, r3, total3 = await store.append(
        scope, [{"kind": "note", "content": "fresh"}], mode="replace"
    )
    assert (r3, total3) == (3, 1)
    items = (await store.list(scope))["items"]
    assert [row["id"] for row in items] == [added[0]["id"]]

    removed = await store.remove(scope, added[0]["id"])
    assert removed == {"removed": True, "revision": 4}
    cleared = await store.clear(scope)
    assert cleared["revision"] == 5

    with pytest.raises(CanvasStoreError):
        await store.remove(scope, "no-such-item")


async def test_store_fifo_eviction_at_cap(canvas_database, monkeypatch):
    monkeypatch.setattr("nodes.tool.canvas._store.CANVAS_MAX_ITEMS", 3)
    store = CanvasStore(canvas_database)
    scope = _scope()

    for index in range(5):
        await store.append(
            scope, [{"kind": "note", "content": f"note-{index}"}]
        )

    listed = await store.list(scope)
    assert [row["content"] for row in listed["items"]] == [
        "note-2",
        "note-3",
        "note-4",
    ]
    assert listed["revision"] == 5


async def test_store_rejects_unknown_kind(canvas_database):
    store = CanvasStore(canvas_database)
    with pytest.raises(CanvasStoreError):
        await store.append(_scope(), [{"kind": "bytes", "content": "x"}])


async def test_item_wire_shape_is_stable(canvas_database):
    store = CanvasStore(canvas_database)
    added, _, _ = await store.append(
        _scope(), [{"kind": "note", "content": "shape", "title": "t"}]
    )
    assert set(added[0]) == {
        "id",
        "kind",
        "title",
        "ref",
        "url",
        "content",
        "language",
        "source",
        "created_at",
    }


def test_truncate_note_caps_with_visible_marker():
    text, truncated = truncate_note("x" * (CANVAS_NOTE_MAX_BYTES + 100))
    assert truncated is True
    assert text.endswith("[truncated]")
    assert len(text.encode("utf-8")) <= CANVAS_NOTE_MAX_BYTES

    short, untouched = truncate_note("short")
    assert (short, untouched) == ("short", False)


def test_board_id_is_versioned_and_scope_sensitive():
    base = _scope()
    assert base.board_id.startswith("canv_")
    assert base.board_id != _scope(node_id="other").board_id
    assert base.board_id != _scope(workflow_id="wf-2").board_id


# ---------------------------------------------------------------------------
# Display op (direct op call with a constructed NodeContext)
# ---------------------------------------------------------------------------


def _node() -> CanvasNode:
    return CanvasNode()


def _ctx(tmp_path, *, raw=None, workflow_id="wf-op") -> NodeContext:
    return NodeContext(
        node_id="canvas-node",
        node_type="canvas",
        workflow_id=workflow_id,
        workspace_dir=str(tmp_path),
        raw=raw or {},
    )


@pytest.fixture
def op_database(canvas_database, monkeypatch):
    monkeypatch.setattr(
        "services.plugin.deps.get_database", lambda: canvas_database
    )
    return canvas_database


@pytest.fixture
def captured_events(monkeypatch):
    events = []

    async def _capture(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(
        "nodes.tool.canvas._events.dispatch_canvas_updated", _capture
    )
    return events


async def test_display_paths_builds_contained_refs(
    tmp_path, op_database, captured_events
):
    media = tmp_path / "media"
    media.mkdir()
    (media / "chart.png").write_bytes(b"\x89PNG fake")

    result = await _node().display(
        _ctx(tmp_path),
        CanvasParams(paths=["media/chart.png"], title="Q3 chart"),
    )

    assert result["count"] == 1
    assert result["added"][0]["kind"] == "file"
    assert result["added"][0]["title"] == "Q3 chart"
    # Payload discipline: ids and titles only — no ref bodies in the output.
    assert set(result["added"][0]) == {"id", "kind", "title"}
    assert captured_events == [
        {"workflow_id": "wf-op", "node_id": "canvas-node", "revision": 1}
    ]

    store = CanvasStore(op_database)
    items = (
        await store.list(
            CanvasScope(
                owner_id="owner", workflow_id="wf-op", node_id="canvas-node"
            )
        )
    )["items"]
    ref = items[0]["ref"]
    assert ref["kind"] == "file"
    assert ref["path"] == "media/chart.png"
    assert ref["mime_type"] == "image/png"
    assert ref["workflow_id"] == "wf-op"
    assert ref["url"] == "/api/workspace/wf-op/files/media/chart.png"


async def test_display_rejects_traversal_paths(
    tmp_path, op_database, captured_events
):
    with pytest.raises(NodeUserError):
        await _node().display(
            _ctx(tmp_path), CanvasParams(paths=["../../credentials.db"])
        )
    assert captured_events == []


async def test_display_rejects_non_http_url(
    tmp_path, op_database, captured_events
):
    with pytest.raises(NodeUserError):
        await _node().display(
            _ctx(tmp_path), CanvasParams(url="javascript:alert(1)")
        )
    with pytest.raises(NodeUserError):
        await _node().display(
            _ctx(tmp_path), CanvasParams(url="file:///etc/passwd")
        )


async def test_display_note_truncates_and_reports(
    tmp_path, op_database, captured_events
):
    result = await _node().display(
        _ctx(tmp_path),
        CanvasParams(content="y" * (CANVAS_NOTE_MAX_BYTES + 10)),
    )
    assert "truncated" in result["message"]


async def test_display_empty_call_is_user_correctable(
    tmp_path, op_database, captured_events
):
    with pytest.raises(NodeUserError):
        await _node().display(_ctx(tmp_path), CanvasParams())


async def test_display_scans_connected_outputs_for_refs(
    tmp_path, op_database, captured_events
):
    connected = {
        "textToSpeech": {
            "audio": {
                "kind": "audio",
                "path": "audio/greeting.wav",
                "filename": "greeting.wav",
                "format": "wav",
            },
            # A gallery-style listing row is NOT a ref (extra keys fail
            # extra="forbid") — but the ref nested inside it is found.
            "row": {
                "name": "x.png",
                "is_dir": False,
                "ref": {
                    "kind": "file",
                    "path": "media/x.png",
                    "filename": "x.png",
                },
            },
        }
    }
    result = await _node().display(
        _ctx(tmp_path, raw={"connected_outputs": connected}), CanvasParams()
    )
    assert result["count"] == 2
    assert {entry["kind"] for entry in result["added"]} == {"file"}


async def test_display_labels_tool_calls_as_agent(
    tmp_path, op_database, captured_events
):
    await _node().display(
        _ctx(tmp_path, raw={"_tool_config": object()}),
        CanvasParams(content="from the model"),
    )
    store = CanvasStore(op_database)
    items = (
        await store.list(
            CanvasScope(
                owner_id="owner", workflow_id="wf-op", node_id="canvas-node"
            )
        )
    )["items"]
    assert items[0]["source"] == "agent"


async def test_display_replace_mode_clears_board(
    tmp_path, op_database, captured_events
):
    node, ctx = _node(), _ctx(tmp_path)
    await node.display(ctx, CanvasParams(content="first"))
    result = await node.display(
        ctx, CanvasParams(content="second", mode="replace")
    )
    assert result["count"] == 1
    assert "replaced" in result["message"]


def test_collect_connected_refs_dedupes_and_caps():
    ref = {"kind": "file", "path": "a/b.png", "filename": "b.png"}
    payload = {
        "one": ref,
        "two": {"nested": [dict(ref), {"kind": "file", "path": "c.md", "filename": "c.md"}]},
        "not_a_ref": {"kind": "file", "path": "x", "filename": "x", "extra_key": 1},
    }
    refs = _collect_connected_refs(payload, limit=10)
    assert [r["path"] for r in refs] == ["a/b.png", "c.md"]

    many = {str(i): {"kind": "file", "path": f"p{i}", "filename": f"p{i}"} for i in range(9)}
    assert len(_collect_connected_refs(many, limit=3)) == 3


# ---------------------------------------------------------------------------
# Params coercion
# ---------------------------------------------------------------------------


def test_params_coerce_paths_shapes():
    assert CanvasParams(paths='["a.png", "b.md"]').paths == ["a.png", "b.md"]
    assert CanvasParams(paths="single/path.png").paths == ["single/path.png"]
    assert CanvasParams(
        paths=[{"kind": "file", "path": "from/ref.wav", "filename": "ref.wav"}]
    ).paths == ["from/ref.wav"]
    assert CanvasParams(paths="").paths is None
    # Blank-string mode from a cleared panel field falls back to the default.
    assert CanvasParams(mode="").mode == "append"


# ---------------------------------------------------------------------------
# Panel WS handlers — security preamble + round trip
# ---------------------------------------------------------------------------


class _FakeSocket:
    def __init__(self, *, path="/ws/status", user_id="owner"):
        self.scope = {"path": path, "user_id": user_id}
        self.state = SimpleNamespace(user_id=user_id)


class _HandlerDatabase:
    """Real store engine + a canned workflow graph for scope resolution."""

    def __init__(self, database, graph):
        self._database = database
        self._graph = graph

    @property
    def engine(self):
        return self._database.engine

    def get_session(self):
        return self._database.get_session()

    async def get_workflow(self, workflow_id):
        if workflow_id != self._graph["id"]:
            return None
        return SimpleNamespace(data=self._graph)


def _graph(workflow_id="wf-h", node_id="canvas-h", node_type="canvas", owner="owner"):
    return {
        "id": workflow_id,
        "owner_id": owner,
        "nodes": [{"id": node_id, "type": node_type}],
        "edges": [],
    }


@pytest.fixture
def handler_env(canvas_database, monkeypatch):
    def bind(graph):
        wrapper = _HandlerDatabase(canvas_database, graph)
        monkeypatch.setattr(
            "nodes.tool.canvas._handlers.get_database", lambda: wrapper
        )
        return wrapper

    return bind


@pytest.fixture
def handler_events(monkeypatch):
    events = []

    async def _capture(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(
        "nodes.tool.canvas._handlers.dispatch_canvas_updated", _capture
    )
    return events


async def test_internal_socket_is_denied(handler_env, handler_events):
    handler_env(_graph())
    response = await handle_canvas_list(
        {"workflow_id": "wf-h", "node_id": "canvas-h"},
        _FakeSocket(path="/ws/internal"),
    )
    assert response["success"] is False
    assert "authenticated" in response["error"]


async def test_owner_mismatch_is_denied(handler_env, handler_events):
    handler_env(_graph(owner="someone-else"))
    response = await handle_canvas_list(
        {"workflow_id": "wf-h", "node_id": "canvas-h"}, _FakeSocket()
    )
    assert response["success"] is False
    assert "denied" in response["error"].lower()


async def test_wrong_node_type_is_denied(handler_env, handler_events):
    handler_env(_graph(node_type="simpleMemory"))
    response = await handle_canvas_list(
        {"workflow_id": "wf-h", "node_id": "canvas-h"}, _FakeSocket()
    )
    assert response["success"] is False
    assert "does not belong" in response["error"]


async def test_handler_round_trip_list_remove_clear(
    canvas_database, handler_env, handler_events
):
    handler_env(_graph())
    store = CanvasStore(canvas_database)
    scope = CanvasScope(owner_id="owner", workflow_id="wf-h", node_id="canvas-h")
    added, _, _ = await store.append(
        scope,
        [
            {"kind": "note", "content": "keep"},
            {"kind": "note", "content": "drop"},
        ],
    )

    listed = await handle_canvas_list(
        {"workflow_id": "wf-h", "node_id": "canvas-h"}, _FakeSocket()
    )
    assert listed["success"] is True
    assert [row["content"] for row in listed["items"]] == ["keep", "drop"]

    removed = await handle_canvas_remove(
        {
            "workflow_id": "wf-h",
            "node_id": "canvas-h",
            "item_id": added[1]["id"],
        },
        _FakeSocket(),
    )
    assert removed["success"] is True

    cleared = await handle_canvas_clear(
        {"workflow_id": "wf-h", "node_id": "canvas-h"}, _FakeSocket()
    )
    assert cleared["success"] is True

    # Both mutations broadcast identity + revision.
    assert [event["node_id"] for event in handler_events] == [
        "canvas-h",
        "canvas-h",
    ]

    final = await handle_canvas_list(
        {"workflow_id": "wf-h", "node_id": "canvas-h"}, _FakeSocket()
    )
    assert final["items"] == []


# ---------------------------------------------------------------------------
# Event + tool-schema contract locks
# ---------------------------------------------------------------------------


def test_canvas_updated_event_is_identity_only():
    event = canvas_updated(workflow_id="wf-e", node_id="canvas-e", revision=7)
    assert event.source == "opencompany://nodes/canvas"
    assert event.type == "com.opencompany.canvas.updated"
    assert event.subject == "canvas-e"
    assert event.data == {
        "workflow_id": "wf-e",
        "node_id": "canvas-e",
        "revision": 7,
    }


def test_tool_schema_is_locked_flat_and_named_canvas():
    schema = CanvasNode.as_tool_schema()
    assert schema["name"] == "canvas"
    assert CanvasNode.tool_schema_locked is True

    def assert_no_ref_keys(value):
        if isinstance(value, dict):
            assert "$defs" not in value and "$ref" not in value, value.keys()
            for child in value.values():
                assert_no_ref_keys(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_ref_keys(child)

    assert_no_ref_keys(schema)

    assert CanvasNode.ui_hints["isCanvasPanel"] is True
    assert CanvasNode.ui_hints["isConfigNode"] is False
    assert CanvasNode.annotations == {
        "destructive": False,
        "readonly": False,
        "open_world": False,
    }
