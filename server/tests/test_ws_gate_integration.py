"""Integration tests: CSWSH gate is wired and enforced in the live endpoints.

Gap closed here
--------------
Argus's unit tests (test_ws_gate.py) prove the gate logic in isolation —
stub sockets, no server. These tests close the remaining gap: a real WebSocket
handshake against the actual FastAPI router confirms the gate is called at the
right point in the endpoint and that it refuses or admits as promised.

Boot strategy
-------------
Starlette TestClient (in-process ASGI transport, no subprocess). The DI
container is patched to a minimal stub; for refusal cases the broadcaster is
never reached so the stub is trivial. The success cases need accept() to
complete; the stub allows that by providing AsyncMock-backed connect/disconnect.

Scenarios
---------
1. Cross-origin browser connection to /ws/status  -> refused (code 4403)
2. /ws/internal + valid token + Origin header     -> refused (code 4403)
3. /ws/status + no token + no Origin             -> refused (code 4403)
4. /ws/internal + correct token + no Origin      -> admitted (no close)
"""

from __future__ import annotations

import threading
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.authz.ws_gate import (
    INTERNAL_TOKEN_HEADER,
    STATUS_TOKEN_HEADER,
    internal_ws_token,
)

PORT = 5678
HOST = f"127.0.0.1:{PORT}"
_POLICY_CLOSE = 4403  # ws_gate._WS_CLOSE_POLICY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings():
    """Minimal Settings stub: provides the port, auth disabled for clean gate-only path."""
    s = MagicMock()
    s.port = str(PORT)
    # auth_disabled branch: vite_auth_enabled = "false" skips the cookie check so
    # the test is only measuring the gate, not cookie/JWT logic.
    s.vite_auth_enabled = "false"
    return s


def _make_broadcaster():
    """Minimal broadcaster stub: connect must call websocket.accept() so the TestClient handshake
    completes; disconnect is a no-op AsyncMock; the rest are synchronous stubs."""
    b = MagicMock()

    async def _connect(websocket):
        # The real StatusBroadcaster.connect() calls websocket.accept() before adding the
        # connection to its set.  Without this the Starlette TestClient's __enter__ hangs
        # forever waiting for the ASGI "websocket.accept" message.
        await websocket.accept()

    b.connect = AsyncMock(side_effect=_connect)
    b.disconnect = AsyncMock()
    b.get_status = MagicMock(return_value={})
    b.connection_count = 0
    return b


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gate_client():
    """TestClient with the WebSocket router and a patched DI container."""
    from fastapi import FastAPI
    from routers.websocket import router as ws_router

    app = FastAPI()
    app.include_router(ws_router)

    settings = _make_settings()
    broadcaster = _make_broadcaster()

    mock_container = MagicMock()
    mock_container.settings.return_value = settings

    # Patch both the container reference and the broadcaster factory as they
    # are used in the endpoint function bodies (not at module-import time).
    with (
        patch("routers.websocket.container", mock_container),
        patch("routers.websocket.get_status_broadcaster", return_value=broadcaster),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


# ---------------------------------------------------------------------------
# Assertion helper
# ---------------------------------------------------------------------------


def _assert_refused(client: TestClient, path: str, headers: dict) -> None:
    """Assert the connection is closed with the CSWSH policy code (4403)."""
    try:
        with client.websocket_connect(path, headers=headers):
            pytest.fail(
                f"Expected refusal on {path} but the connection was accepted. "
                "Gate is not enforced."
            )
    except WebSocketDisconnect as exc:
        assert exc.code == _POLICY_CLOSE, (
            f"Gate refused (good) but with unexpected close code {exc.code!r}; "
            f"expected {_POLICY_CLOSE}. Reason may differ but code must match."
        )


# ---------------------------------------------------------------------------
# /ws/status
# ---------------------------------------------------------------------------


class TestStatusSocketLive:
    """Live handshake tests for the browser WebSocket (/ws/status)."""

    def test_cross_origin_browser_is_refused(self, gate_client):
        """Classic CSWSH shape: Origin from a foreign site must be refused.

        A page at http://evil.example opens ws://127.0.0.1:<port>/ws/status.
        Before the patch, auth_disabled skipped every check and this was admitted.
        """
        _assert_refused(
            gate_client,
            "/ws/status",
            {"host": HOST, "origin": "http://evil.example"},
        )

    def test_no_token_no_origin_is_refused(self, gate_client):
        """Bare WebSocket with no Origin and no token: must be refused.

        This is the CSWSH + no-cookie case that VITE_AUTH_ENABLED=false
        previously admitted unconditionally. Fail-closed means no proof = refusal.
        """
        _assert_refused(
            gate_client,
            "/ws/status",
            {"host": HOST},
        )

    def test_same_origin_browser_is_admitted(self, gate_client):
        """Correct same-origin proof (loopback Host + matching Origin) must be admitted.

        broadcaster.connect (AsyncMock with side_effect) calls websocket.accept(), so the
        TestClient's __enter__ succeeds and the with block is reached.  ws.close() puts a
        disconnect event into the ASGI receive queue; receive_loop catches WebSocketDisconnect,
        enqueues None, process_loop breaks, TaskGroup exits, endpoint returns cleanly.
        """
        with gate_client.websocket_connect(
            "/ws/status",
            headers={"host": HOST, "origin": f"http://{HOST}"},
        ) as ws:
            ws.close()


# ---------------------------------------------------------------------------
# /ws/internal
# ---------------------------------------------------------------------------


class TestInternalSocketLive:
    """Live handshake tests for the internal worker WebSocket (/ws/internal)."""

    def test_browser_with_valid_token_is_refused(self, gate_client):
        """Token-holding client that also sends Origin must be refused.

        A browser always sends Origin; the Temporal activity worker never does.
        Origin on /ws/internal is therefore a browser signal and must be rejected
        even when the token is valid.
        """
        token = internal_ws_token()
        assert token, "internal_ws_token() returned None — cannot prove the refusal"
        _assert_refused(
            gate_client,
            "/ws/internal",
            {
                "host": HOST,
                "origin": "http://evil.example",
                INTERNAL_TOKEN_HEADER: token,
            },
        )

    def test_no_token_is_refused(self, gate_client):
        """Internal socket with no token presented must be refused."""
        _assert_refused(
            gate_client,
            "/ws/internal",
            {"host": HOST},
        )

    def test_valid_worker_connection_is_admitted(self, gate_client):
        """Correct token, no Origin: activity worker shape must be admitted.

        This is the one case where the gate should pass end-to-end. If
        WebSocketDisconnect is raised here the gate is over-blocking.
        """
        token = internal_ws_token()
        assert token, "internal_ws_token() returned None — cannot prove admission"
        with gate_client.websocket_connect(
            "/ws/internal",
            headers={"host": HOST, INTERNAL_TOKEN_HEADER: token},
        ) as ws:
            # Connection open — gate passed. Close to unblock the server's receive loop.
            ws.close()
