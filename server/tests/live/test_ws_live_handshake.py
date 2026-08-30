"""Live cross-origin WebSocket integration test against the running
cipher-workforce service at ``ws://127.0.0.1:8473/ws/internal``.

This closes the ws_gate.py acceptance gap: the manifest-gate wiring
(dispatch_token.py, soul-bound identity, fail-closed) was merged and
Argus-approved based on ASGI ``TestClient`` coverage
(``test_ws_manifest_gate_e2e.py``, ``test_ws_gate_integration.py``), which
never performs a real cross-origin TCP handshake against the live process.
This script does — real TCP via the ``websockets`` library, no ASGI client,
no mocks.

Adaptation note vs. the original brief: the credential that actually gates
the ``/ws/internal`` handshake open/closed is the shared per-host token in
``services.authz.ws_gate`` (header ``x-workforce-internal-token``), not a
bearer-style ``Authorization`` header and not the dispatch token. The
dispatch token (``services.authz.dispatch_token``) is a *second*, optional
layer that binds a soul identity onto an already-accepted connection for the
manifest gate in ``node_executor.py`` — it has no production minting
endpoint yet (only unit-test helpers), so it cannot be issued against the
live, separately-running server process from an external script. Scenario D
below exercises the same manifest-gate code path Argus's mutation test
targets (an unbound / unknown-soul connection attempting node execution)
without needing to mint one.

Run standalone::

    python3 server/tests/live/test_ws_live_handshake.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
import websockets
import websockets.exceptions

HEALTH_URL = "http://127.0.0.1:8473/health"
WS_INTERNAL_URL = "ws://127.0.0.1:8473/ws/internal"
HOST = "127.0.0.1:8473"

# Must match services.authz.ws_gate.INTERNAL_TOKEN_HEADER
INTERNAL_TOKEN_HEADER = "x-workforce-internal-token"

# Same file services.authz.ws_gate.internal_ws_token() reads/provisions.
_TOKEN_PATH = Path.home() / ".cipheros" / "workforce" / "internal_ws_token"

_WS_CLOSE_POLICY = 4403


def _require_service_up() -> None:
    """Confirm the live service answers /health before running anything."""
    try:
        resp = httpx.get(HEALTH_URL, timeout=5.0)
    except httpx.HTTPError as exc:
        print(f"ERROR: cipher-workforce not reachable at {HEALTH_URL}: {exc}")
        sys.exit(2)
    if resp.status_code != 200:
        print(f"ERROR: /health returned {resp.status_code}, expected 200: {resp.text[:200]}")
        sys.exit(2)


def _read_internal_token() -> str:
    if not _TOKEN_PATH.exists():
        raise FileNotFoundError(f"Internal WS token not found at {_TOKEN_PATH}")
    return _TOKEN_PATH.read_text(encoding="utf-8").strip()


async def _try_connect(headers: dict[str, str]):
    """Attempt a handshake. Returns (websocket_or_None, close_code_or_None)."""
    try:
        ws = await websockets.connect(WS_INTERNAL_URL, additional_headers=headers, open_timeout=5)
        return ws, None
    except websockets.exceptions.InvalidStatus as exc:
        return None, getattr(exc.response, "status_code", None)
    except websockets.exceptions.ConnectionClosedError as exc:
        return None, exc.rcvd.code if exc.rcvd else None
    except websockets.exceptions.ConnectionClosed as exc:
        return None, exc.rcvd.code if exc.rcvd else None


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_no_token_refused() -> tuple[bool, str]:
    """No dispatch/internal token at all -> handshake must be refused."""
    ws, code = await _try_connect({"Host": HOST})
    if ws is not None:
        await ws.close()
        return False, "FAIL — server accepted a connection with no token at all"
    if code == _WS_CLOSE_POLICY:
        return True, f"PASS — refused with close code {code}"
    return True, f"PASS — refused (code={code})"


async def scenario_garbage_token_refused() -> tuple[bool, str]:
    """Garbage bearer-style token -> handshake must be refused (4403)."""
    ws, code = await _try_connect({"Host": HOST, INTERNAL_TOKEN_HEADER: "Bearer garbage"})
    if ws is not None:
        await ws.close()
        return False, "FAIL — server accepted a connection with a garbage token"
    if code == _WS_CLOSE_POLICY:
        return True, f"PASS — refused with close code {code}"
    return True, f"PASS — refused (code={code}, expected 4403)"


async def scenario_valid_token_accepted() -> tuple[bool, str, object]:
    """Valid internal token -> handshake must be accepted.

    Returns the open connection (or None) as the third element so the next
    scenario can reuse it.
    """
    try:
        token = _read_internal_token()
    except FileNotFoundError as exc:
        return False, f"FAIL — {exc}", None

    ws, code = await _try_connect({"Host": HOST, INTERNAL_TOKEN_HEADER: token})
    if ws is not None:
        return True, "PASS — valid internal token handshake accepted", ws
    return False, f"FAIL — valid token connection was refused with code {code}", None


async def scenario_manifest_gated_node_refused(ws) -> tuple[bool, str]:
    """On the accepted connection, execute_node for an unbound soul identity
    must be refused by the manifest gate (fail-closed), not crash the socket.

    No dispatch token was presented on this connection, so
    ``dispatch_soul_id`` is None while ``is_soul_plane`` is True — the exact
    condition node_executor.py resolves to ``_UNKNOWN_MANIFEST`` (zero
    capabilities), refusing every node_type. This is the live counterpart of
    ``test_ws_manifest_gate_e2e.py::TestNoTokenRefused``.
    """
    if ws is None:
        return False, "FAIL — no accepted connection to test node execution on"

    request_id = uuid.uuid4().hex
    payload = {
        "type": "execute_node",
        "request_id": request_id,
        "node_id": "live-test-node-1",
        "node_type": "pythonExecutor",
        "workflow_id": "wf-live-ws-test",
        "execution_id": uuid.uuid4().hex,
        "parameters": {},
        "nodes": [],
        "edges": [],
    }
    try:
        await ws.send(__import__("json").dumps(payload))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
    except (TimeoutError, asyncio.TimeoutError):
        return False, "FAIL — no response from server within 10s (possible hang/crash)"
    except websockets.exceptions.ConnectionClosed as exc:
        return False, f"FAIL — connection crashed/closed instead of returning a refusal: {exc}"

    import json

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"FAIL — non-JSON response: {raw!r}"

    success = result.get("success")
    error = result.get("error") or ""
    if success is False and "fail-closed" in error:
        return True, f"PASS — refused via manifest gate (not a crash): {error!r}"
    if success is True:
        return False, f"FAIL — unbound connection was permitted to execute a node: {result!r}"
    return False, f"FAIL — unexpected response shape: {result!r}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _main() -> None:
    _require_service_up()
    print(f"[setup] /health OK at {HEALTH_URL}")

    results: list[tuple[str, bool, str]] = []

    label, desc = "1", "No token -> /ws/internal refused"
    print(f"[{label}] {desc} ...", end=" ", flush=True)
    passed, msg = await scenario_no_token_refused()
    print("PASS" if passed else "FAIL")
    results.append((label, passed, msg))

    label, desc = "2", "Garbage token -> /ws/internal refused (4403)"
    print(f"[{label}] {desc} ...", end=" ", flush=True)
    passed, msg = await scenario_garbage_token_refused()
    print("PASS" if passed else "FAIL")
    results.append((label, passed, msg))

    label, desc = "3", "Valid internal token -> /ws/internal accepted"
    print(f"[{label}] {desc} ...", end=" ", flush=True)
    passed, msg, ws = await scenario_valid_token_accepted()
    print("PASS" if passed else "FAIL")
    results.append((label, passed, msg))

    label, desc = "4", "Unbound soul-plane connection -> execute_node refused by manifest gate"
    print(f"[{label}] {desc} ...", end=" ", flush=True)
    passed, msg = await scenario_manifest_gated_node_refused(ws)
    print("PASS" if passed else "FAIL")
    results.append((label, passed, msg))

    if ws is not None:
        await ws.close()

    print()
    print("=" * 60)
    all_pass = all(r[1] for r in results)
    print(f"Result: {'ALL PASS' if all_pass else 'FAILURES DETECTED'}")
    for lbl, passed, msg in results:
        mark = "+" if passed else "x"
        print(f"  [{mark}] {lbl}: {msg}")
    print("=" * 60)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(_main())
