"""Live cross-origin WebSocket integration tests for ws_gate.

These tests connect to the **already-running** server at 127.0.0.1:8473 over
real TCP using the ``websockets`` library.  They are marked ``live`` so the
default pytest run (which excludes live tests) never hits the server
accidentally.

Run with::

    pytest -m live server/tests/test_ws_gate_live.py -v

Or as a standalone script::

    python server/tests/test_ws_gate_live.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import websockets
import websockets.exceptions

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER = "ws://127.0.0.1:8473"
HOST = "127.0.0.1:8473"
_WS_CLOSE_POLICY = 4403

_TOKEN_PATH = Path.home() / ".cipheros" / "workforce" / "internal_ws_token"


def _read_internal_token() -> str:
    if not _TOKEN_PATH.exists():
        raise FileNotFoundError(f"Internal WS token not found at {_TOKEN_PATH}")
    return _TOKEN_PATH.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _try_connect(url: str, extra_headers: dict[str, str]) -> tuple[bool, int | None]:
    """Attempt a WS connection.

    Returns ``(success, close_code_or_None)``.  ``success`` is True when the
    handshake completes (HTTP 101); False when the server refuses it.  On
    refusal the close code is extracted from the exception when available.
    """
    try:
        async with websockets.connect(url, additional_headers=extra_headers):
            return True, None
    except websockets.exceptions.InvalidStatus as exc:
        # Server rejected the upgrade (non-101 response) — treat as refusal.
        status = getattr(exc.response, "status_code", None)
        return False, status
    except websockets.exceptions.ConnectionClosedError as exc:
        code = exc.rcvd.code if exc.rcvd else None
        return False, code
    except websockets.exceptions.ConnectionClosed as exc:
        code = exc.rcvd.code if exc.rcvd else None
        return False, code


# ---------------------------------------------------------------------------
# Scenario implementations
# ---------------------------------------------------------------------------


async def scenario_a_cross_origin_refused() -> tuple[bool, str]:
    """Cross-origin /ws/status must be refused."""
    success, code = await _try_connect(
        f"{SERVER}/ws/status",
        {"Host": HOST, "Origin": "http://evil.example"},
    )
    if success:
        return False, f"FAIL — server accepted a cross-origin connection (should have refused)"
    if code == _WS_CLOSE_POLICY or code in (403, 1008):
        return True, f"PASS — refused with code {code}"
    return True, f"PASS — refused (code={code}, non-101 or close)"


async def scenario_b_no_origin_no_token_refused() -> tuple[bool, str]:
    """No Origin, no token → /ws/status must be refused."""
    success, code = await _try_connect(
        f"{SERVER}/ws/status",
        {"Host": HOST},
    )
    if success:
        return False, f"FAIL — server accepted a connection with no Origin and no token"
    return True, f"PASS — refused with code {code}"


async def scenario_c_same_origin_accepted() -> tuple[bool, str]:
    """Same-origin /ws/status must be accepted."""
    success, code = await _try_connect(
        f"{SERVER}/ws/status",
        {"Host": HOST, "Origin": f"http://{HOST}"},
    )
    if success:
        return True, "PASS — same-origin handshake accepted"
    return False, f"FAIL — same-origin connection was refused with code {code}"


async def scenario_d_internal_token_accepted() -> tuple[bool, str]:
    """Valid internal token → /ws/internal must be accepted (no Origin)."""
    try:
        token = _read_internal_token()
    except FileNotFoundError as exc:
        return False, f"FAIL — {exc}"

    success, code = await _try_connect(
        f"{SERVER}/ws/internal",
        {"Host": HOST, "x-workforce-internal-token": token},
    )
    if success:
        return True, "PASS — internal token handshake accepted"
    return False, f"FAIL — internal token connection was refused with code {code}"


# ---------------------------------------------------------------------------
# pytest integration
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
async def test_cross_origin_refused() -> None:
    passed, msg = await scenario_a_cross_origin_refused()
    print(f"\n[A] Cross-origin /ws/status → {msg}")
    assert passed, msg


@pytest.mark.live
@pytest.mark.asyncio
async def test_no_origin_no_token_refused() -> None:
    passed, msg = await scenario_b_no_origin_no_token_refused()
    print(f"\n[B] No-origin no-token /ws/status → {msg}")
    assert passed, msg


@pytest.mark.live
@pytest.mark.asyncio
async def test_same_origin_accepted() -> None:
    passed, msg = await scenario_c_same_origin_accepted()
    print(f"\n[C] Same-origin /ws/status → {msg}")
    assert passed, msg


@pytest.mark.live
@pytest.mark.asyncio
async def test_internal_token_accepted() -> None:
    passed, msg = await scenario_d_internal_token_accepted()
    print(f"\n[D] Internal token /ws/internal → {msg}")
    assert passed, msg


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


async def _main() -> None:
    scenarios = [
        ("A", "Cross-origin /ws/status → refused", scenario_a_cross_origin_refused),
        ("B", "No Origin + no token /ws/status → refused", scenario_b_no_origin_no_token_refused),
        ("C", "Same-origin /ws/status → accepted", scenario_c_same_origin_accepted),
        ("D", "Valid token /ws/internal (no Origin) → accepted", scenario_d_internal_token_accepted),
    ]

    results: list[tuple[str, bool, str]] = []
    for label, description, fn in scenarios:
        print(f"[{label}] {description} ...", end=" ", flush=True)
        passed, msg = await fn()
        status = "PASS" if passed else "FAIL"
        print(status)
        if not passed:
            print(f"     Detail: {msg}")
        results.append((label, passed, msg))

    print()
    all_pass = all(r[1] for r in results)
    print("=" * 50)
    print(f"Result: {'ALL PASS' if all_pass else 'FAILURES DETECTED'}")
    for label, passed, msg in results:
        mark = "+" if passed else "x"
        print(f"  [{mark}] {label}: {msg}")
    print("=" * 50)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(_main())
