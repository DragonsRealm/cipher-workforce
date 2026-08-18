"""Wire contract for the Context conversation lifecycle event.

Two rules are locked here.

1. Context events are UI notifications, not workflow triggers. No node type
   registers a canary consumer for ``com.opencompany.context.*``, so routing
   them through ``services.events.dispatch.emit`` would run a Temporal
   Visibility query that is guaranteed to match nothing -- once per save.
   They broadcast directly instead.
2. The payload stays identity-only. The broadcast fans out to every connected
   socket, so a message body leaking in here would be a disclosure bug, not a
   display bug.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

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


def test_cloudevents_envelope_shape():
    event = context_events.context_updated(
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="1:aiAgent:1",
        message_count=6,
    )

    assert event.type == "com.opencompany.context.updated"
    assert event.source == "opencompany://nodes/context"
    assert event.subject == "1:aiAgent:1"
    assert event.data["agent_node_id"] == "1:aiAgent:1"


def test_updated_payload_carries_identity_only():
    event = context_events.context_updated(
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="1:aiAgent:1",
        message_count=6,
    )

    # Anything not in this set would be broadcast to every connected client.
    assert set(event.data) == {
        "workflow_id",
        "generation",
        "agent_node_id",
        "message_count",
    }


def test_negative_message_counts_are_clamped():
    event = context_events.context_updated(
        workflow_id="workflow-1",
        generation=1,
        agent_node_id="agent-1",
        message_count=-5,
    )

    assert event.data["message_count"] == 0


@pytest.mark.asyncio
async def test_save_listener_broadcasts_context_updated(monkeypatch):
    sent: list = []

    async def _capture(**metadata):
        sent.append(metadata)

    monkeypatch.setattr(
        context_events, "dispatch_context_updated", _capture
    )

    await context_events.on_conversation_saved(
        workflow_id="workflow-1",
        generation=2,
        agent_node_id="1:aiAgent:1",
        message_count=5,
    )

    assert sent == [
        {
            "workflow_id": "workflow-1",
            "generation": 2,
            "agent_node_id": "1:aiAgent:1",
            "message_count": 5,
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
    assert emitted == {"context.updated"}

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
    from services.agent_context import conversation, listeners

    for module in (conversation, listeners):
        source = inspect.getsource(module)
        assert "nodes." not in source and "from nodes" not in source, (
            "The store must stay free of plugin knowledge; the Context "
            "plugin registers its broadcaster via "
            "register_conversation_listener."
        )


def test_plugin_registers_its_save_listener():
    import nodes.context as context_plugin
    from services.agent_context import listeners

    source = inspect.getsource(context_plugin)
    assert "register_conversation_listener" in source
    assert context_events.on_conversation_saved in list(listeners._LISTENERS)
