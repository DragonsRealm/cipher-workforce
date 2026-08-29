"""WS-layer end-to-end manifest gate tests — Argus D2 requirement.

Scenarios 1, 3, 4: call the real handle_execute_node with a mocked WebSocket
carrying state.dispatch_soul_id / state.is_soul_plane, wired to a real
NodeExecutor so node_executor.py's gate logic is exercised, not bypassed.

Scenario 2: TestClient verifies the connection-level 4403 refusal for a
garbage dispatch token (this test was already passing; its assertion is
unchanged and remains the only real detection-capable test from the original
file per Argus's second-round ruling).

Acceptance bar (Argus): change
    if _is_soul_plane or _dispatch_soul_id:
to
    if False:
in services/node_executor.py; at least one test in this suite must FAIL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.authz.ws_gate import INTERNAL_TOKEN_HEADER, internal_ws_token
from services.authz.dispatch_token import issue_dispatch_token, DISPATCH_TOKEN_HEADER
from services.node_executor import NodeExecutor

PORT = 5678
HOST = f"127.0.0.1:{PORT}"
_POLICY_CLOSE = 4403

# Node type used in "no token" and "unlisted node" tests.  This type must NOT
# appear in any soul manifest (including _UNKNOWN_MANIFEST) so the gate always
# refuses it.
_UNLISTED_NODE_TYPE = "superSecretPrivilegedNodeType_NEVER_IN_MANIFEST"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_executor() -> NodeExecutor:
    """Real NodeExecutor with an empty handler registry.

    _build_handler_registry is patched to {} to avoid importing all plugins.
    This is safe: the manifest gate (node_executor.py lines 158-183) fires
    BEFORE any handler lookup; refused executions return from the gate without
    touching _handlers.
    """
    with patch.object(NodeExecutor, "_build_handler_registry", return_value={}):
        return NodeExecutor(
            database=MagicMock(),
            ai_service=MagicMock(),
            maps_service=MagicMock(),
            text_service=MagicMock(),
            android_service=MagicMock(),
            settings=MagicMock(),
        )


def _make_broadcaster():
    b = MagicMock()
    b.update_node_status = AsyncMock()
    b.workflow_run_started = AsyncMock()
    b.workflow_run_ended = AsyncMock()
    return b


def _make_mock_websocket(*, is_soul_plane: bool, dispatch_soul_id: str | None):
    """Mocked WebSocket carrying gate-relevant state attributes."""
    ws = MagicMock()
    ws.state = MagicMock()
    ws.state.is_soul_plane = is_soul_plane
    ws.state.dispatch_soul_id = dispatch_soul_id
    return ws


def _build_execute_node_data(
    node_type: str,
    node_id: str = "test-node-1",
    workflow_id: str = "wf-test",
    execution_id: str = "exec-test",
) -> dict:
    return {
        "type": "execute_node",
        "node_id": node_id,
        "node_type": node_type,
        "workflow_id": workflow_id,
        "execution_id": execution_id,
        "parameters": {},
        "nodes": [],
        "edges": [],
    }


async def _run_handle_execute_node(
    data: dict,
    is_soul_plane: bool,
    dispatch_soul_id: str | None,
    broadcaster,
    real_executor: NodeExecutor,
) -> dict:
    """Drive handle_execute_node with a mocked WebSocket and real NodeExecutor.

    Patches container and get_status_broadcaster so the handler uses the
    provided broadcaster and calls through to real_executor.execute().
    """
    from routers.websocket import handle_execute_node

    mock_container = MagicMock()
    settings = MagicMock()
    settings.dlq_enabled = False
    mock_container.settings.return_value = settings

    async def _execute_node_via_real_gate(
        node_id, node_type, parameters=None, nodes=None, edges=None,
        session_id="default", execution_id=None, workflow_id=None,
        outputs=None, extras=None, user_id=None,
    ):
        context: dict = {
            "session_id": session_id,
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "nodes": nodes or [],
            "edges": edges or [],
            "outputs": outputs or {},
        }
        if extras:
            context.update(extras)
        return await real_executor.execute(
            node_id=node_id,
            node_type=node_type,
            parameters=parameters or {},
            context=context,
        )

    mock_container.workflow_service.return_value.execute_node = AsyncMock(
        side_effect=_execute_node_via_real_gate
    )

    ws = _make_mock_websocket(
        is_soul_plane=is_soul_plane,
        dispatch_soul_id=dispatch_soul_id,
    )

    with (
        patch("routers.websocket.container", mock_container),
        patch("routers.websocket.get_status_broadcaster", return_value=broadcaster),
        patch("routers.websocket.execution_principal", return_value="owner"),
    ):
        return await handle_execute_node(data, ws)


def _error_calls_with_refusal_string(broadcaster) -> list:
    """Return update_node_status calls carrying status='error' + 'fail-closed'."""
    result = []
    for c in broadcaster.update_node_status.call_args_list:
        if len(c.args) < 2:
            continue
        if c.args[1] != "error":
            continue
        payload = c.args[2] if len(c.args) >= 3 else {}
        if isinstance(payload, dict) and "fail-closed" in payload.get("error", ""):
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Scenario 1: No dispatch token -> unlisted node refused (fail-closed)
# ---------------------------------------------------------------------------


class TestNoTokenRefused:
    """Soul-plane connection with NO dispatch token must refuse every node type.

    handle_execute_node propagates _is_soul_plane=True, _dispatch_soul_id=None.
    NodeExecutor resolves soul_id="" -> _UNKNOWN_MANIFEST (zero capabilities).
    Every node_type is unlisted -> refused (fail-closed).

    MUTATION SENSITIVITY: disabling the gate in node_executor.py (if False:)
    means the executor skips the manifest check and no "fail-closed" string
    appears in any update_node_status call.  The assertion below goes RED.
    """

    async def test_no_dispatch_token_unlisted_node_refused(self):
        broadcaster = _make_broadcaster()
        real_executor = _make_node_executor()

        await _run_handle_execute_node(
            data=_build_execute_node_data("pythonExecutor"),
            is_soul_plane=True,
            dispatch_soul_id=None,
            broadcaster=broadcaster,
            real_executor=real_executor,
        )

        refusal_calls = _error_calls_with_refusal_string(broadcaster)
        assert refusal_calls, (
            "Expected broadcaster.update_node_status called with status='error' "
            "and 'fail-closed' in the error string. "
            "No dispatch token on a soul-plane connection must resolve to "
            "_UNKNOWN_MANIFEST and refuse every node_type (fail-closed). "
            "If this fails with 'if False:' mutation, the gate is correctly wired."
        )


# ---------------------------------------------------------------------------
# Scenario 2: Wrong / unresolvable token -> connection refused at 4403
# ---------------------------------------------------------------------------


class TestWrongTokenRefused:
    """A garbage dispatch token must cause WS connection refusal (4403)."""

    def test_wrong_dispatch_token_refused_at_handshake(self):
        from routers.websocket import router as ws_router

        app = FastAPI()
        app.include_router(ws_router)

        mock_container = MagicMock()
        settings = MagicMock()
        settings.port = str(PORT)
        settings.vite_auth_enabled = "false"
        mock_container.settings.return_value = settings

        broadcaster = _make_broadcaster()

        with (
            patch("routers.websocket.container", mock_container),
            patch("routers.websocket.get_status_broadcaster", return_value=broadcaster),
            patch("routers.websocket.execution_principal", return_value="owner"),
        ):
            with TestClient(app, raise_server_exceptions=False) as client:
                headers = {
                    "host": HOST,
                    INTERNAL_TOKEN_HEADER: internal_ws_token(),
                    DISPATCH_TOKEN_HEADER: "definitely-not-a-real-token",
                }
                try:
                    with client.websocket_connect("/ws/internal", headers=headers):
                        pytest.fail(
                            "Expected refusal for garbage dispatch token but connection was accepted."
                        )
                except WebSocketDisconnect as exc:
                    assert exc.code == _POLICY_CLOSE, (
                        f"Expected close code {_POLICY_CLOSE}, got {exc.code!r}"
                    )


# ---------------------------------------------------------------------------
# Scenario 3: Valid token + soul -> gate permits allowed node (no fail-closed)
# ---------------------------------------------------------------------------


class TestValidTokenPermitted:
    """Valid dispatch token for a known soul; an allowed node must not be refused."""

    async def test_valid_token_allowed_node_passes_gate(self):
        from services.soul_manifest import get_manifest

        soul_id = "zane"
        manifest = get_manifest(soul_id)
        enabled = manifest.enabled_node_types()
        if not enabled:
            pytest.skip(f"Soul {soul_id!r} has no enabled node_types; cannot test permitted path")

        node_type = next(iter(enabled))
        broadcaster = _make_broadcaster()
        real_executor = _make_node_executor()

        await _run_handle_execute_node(
            data=_build_execute_node_data(node_type),
            is_soul_plane=True,
            dispatch_soul_id=soul_id,
            broadcaster=broadcaster,
            real_executor=real_executor,
        )

        # The gate must NOT produce a fail-closed refusal for an allowed node.
        refusal_calls = _error_calls_with_refusal_string(broadcaster)
        assert not refusal_calls, (
            f"Manifest gate produced a fail-closed refusal for soul {soul_id!r} "
            f"attempting an allowed node_type '{node_type}'. "
            f"Gate or soul_id binding is broken. Calls: {refusal_calls!r}"
        )

    async def test_valid_token_unlisted_node_refused_via_manifest(self):
        """Valid soul token, but node_type NOT in manifest -> gate refuses.

        MUTATION SENSITIVITY: disabling the gate (if False:) means no
        "fail-closed" string appears, and the assertion below goes RED.
        """
        from services.soul_manifest import get_manifest

        soul_id = "zane"
        manifest = get_manifest(soul_id)
        assert _UNLISTED_NODE_TYPE not in manifest.enabled_node_types(), (
            f"Test invariant violated: {_UNLISTED_NODE_TYPE!r} must not appear "
            f"in soul {soul_id!r}'s manifest."
        )

        broadcaster = _make_broadcaster()
        real_executor = _make_node_executor()

        await _run_handle_execute_node(
            data=_build_execute_node_data(_UNLISTED_NODE_TYPE),
            is_soul_plane=True,
            dispatch_soul_id=soul_id,
            broadcaster=broadcaster,
            real_executor=real_executor,
        )

        refusal_calls = _error_calls_with_refusal_string(broadcaster)
        assert refusal_calls, (
            "Expected broadcaster.update_node_status called with status='error' "
            f"and 'fail-closed' in the error string for soul {soul_id!r} "
            f"attempting unlisted node_type '{_UNLISTED_NODE_TYPE}'. "
            "Manifest gate is not wired."
        )
