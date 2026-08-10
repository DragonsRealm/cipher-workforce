"""Wire contract for Context lifecycle events.

Two rules are locked here.

1. Context events are UI notifications, not workflow triggers. No node type
   registers a canary consumer for ``com.opencompany.context.*``, so routing
   them through ``services.events.dispatch.emit`` would run a Temporal
   Visibility query that is guaranteed to match nothing -- once per journal
   append. They broadcast directly instead.
2. The payload stays identity-only. The broadcast fans out to every connected
   socket, so a message body or provider state leaking in here would be a
   disclosure bug, not a display bug.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from models.agent_context import AgentContextRef
from nodes.context import _events as context_events


def _imported_modules(module) -> set[str]:
    """Modules actually imported by ``module``, ignoring prose.

    Parsed rather than grepped so the module docstring can explain *why*
    ``dispatch.emit`` is avoided without tripping the assertion.
    """

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_dispatchers_do_not_run_a_visibility_query():
    imported = _imported_modules(context_events)
    assert "services.events.dispatch" not in imported, (
        "Context events must not route through dispatch.emit -- it runs a "
        "Temporal Visibility query per call and no canary consumer exists "
        "for com.opencompany.context.*. Broadcast directly instead."
    )
    assert "services.status_broadcaster" in imported


@pytest.mark.parametrize(
    "factory, expected_type",
    [
        ("context_updated", "com.opencompany.context.updated"),
        ("context_compacted", "com.opencompany.context.compacted"),
        ("context_epoch_started", "com.opencompany.context.epoch.started"),
    ],
)
def test_cloudevents_envelope_shape(factory, expected_type):
    kwargs = {
        "workflow_id": "workflow-1",
        "context_node_id": "workflow-1:context:1",
        "thread_id": "session:abc",
        "epoch": 3,
        "revision": 12,
        "provider": "anthropic",
    }
    if factory == "context_updated":
        kwargs["active_token_count"] = 42
    elif factory == "context_compacted":
        kwargs.update(
            active_token_count=42,
            strategy="portable",
            covers_through_sequence=7,
        )
    else:
        kwargs["reason"] = "clear"

    event = getattr(context_events, factory)(**kwargs)

    assert event.type == expected_type
    assert event.source == "opencompany://nodes/context"
    assert event.subject == "workflow-1:context:1"
    assert event.data["context_node_id"] == "workflow-1:context:1"


def test_updated_payload_carries_identity_only():
    event = context_events.context_updated(
        workflow_id="workflow-1",
        context_node_id="workflow-1:context:1",
        thread_id="session:abc",
        epoch=1,
        revision=9,
        provider="anthropic",
        active_token_count=100,
        sequence=17,
    )

    assert event.data["sequence"] == 17
    # Anything not in this set would be broadcast to every connected client.
    assert set(event.data) == {
        "workflow_id",
        "context_node_id",
        "thread_id",
        "epoch",
        "revision",
        "provider",
        "active_token_count",
        "sequence",
    }


def test_sequence_is_omitted_when_absent():
    event = context_events.context_updated(
        workflow_id="workflow-1",
        context_node_id="workflow-1:context:1",
        thread_id="session:abc",
        epoch=1,
        revision=9,
        provider=None,
        active_token_count=0,
    )

    assert "sequence" not in event.data


def test_negative_token_counts_are_clamped():
    event = context_events.context_updated(
        workflow_id="workflow-1",
        context_node_id="workflow-1:context:1",
        thread_id="session:abc",
        epoch=1,
        revision=1,
        provider=None,
        active_token_count=-5,
    )

    assert event.data["active_token_count"] == 0


@pytest.mark.asyncio
async def test_commit_listener_broadcasts_context_updated(monkeypatch):
    sent: list = []

    async def _capture(**metadata):
        sent.append(metadata)

    monkeypatch.setattr(
        context_events, "dispatch_context_updated", _capture
    )

    ref = AgentContextRef(
        workflow_id="workflow-1",
        context_node_id="workflow-1:context:1",
        generation=2,
        thread_id="session:abc",
        epoch=3,
        revision=11,
    )
    await context_events.on_context_commit(
        ref=ref,
        provider="anthropic",
        active_token_count=64,
        sequence=5,
    )

    assert sent == [
        {
            "workflow_id": "workflow-1",
            "context_node_id": "workflow-1:context:1",
            "thread_id": "session:abc",
            "epoch": 3,
            "revision": 11,
            "provider": "anthropic",
            "active_token_count": 64,
            "sequence": 5,
        }
    ]


def test_frontend_handles_every_emitted_wire_key():
    """The panel goes live only if the frontend switches on these keys.

    Same direction as tests/test_frontend_no_node_type_copies.py: the backend
    owns the wire key, and renaming one here would otherwise silently stop the
    Context panel refreshing with no test failing anywhere.
    """

    source = inspect.getsource(context_events)
    emitted = set(re.findall(r'wire_routing_key="([^"]+)"', source))
    assert emitted == {
        "context.updated",
        "context.compacted",
        "context.epoch.started",
    }

    ws_context = (
        Path(__file__).resolve().parents[3]
        / "client"
        / "src"
        / "contexts"
        / "WebSocketContext.tsx"
    )
    if not ws_context.exists():  # server-only checkouts
        pytest.skip("client sources not present")
    consumed = ws_context.read_text(encoding="utf-8")
    for key in sorted(emitted):
        assert f"case '{key}'" in consumed, (
            f"{key} is broadcast by nodes/context/_events.py but no case "
            f"handles it in WebSocketContext.tsx -- the Context panel would "
            f"silently stop updating."
        )


def test_store_never_imports_the_plugin():
    from services.agent_context import store

    source = inspect.getsource(store)
    assert "nodes." not in source and "from nodes" not in source, (
        "The store must stay free of plugin knowledge; the Context plugin "
        "registers its broadcaster via register_context_commit_listener."
    )


def test_plugin_registers_its_commit_listener():
    import nodes.context as context_plugin
    from services.agent_context import listeners

    source = inspect.getsource(context_plugin)
    assert "register_context_commit_listener" in source
    assert context_events.on_context_commit in list(listeners._LISTENERS)
