"""Loopback + Origin + token gate for the two WebSocket handshakes.

This is the cipher-workforce port of CIPHER OS's ``native/app/terminal_mount.py``
``authorize_operator_request`` pattern, applied to the two sockets that dispatch
``execute_node`` with a caller-supplied ``node_type``:

* ``/ws/status``   — the browser socket. Previously it skipped every check when
  ``VITE_AUTH_ENABLED=false`` and assigned the owner principal unconditionally,
  with no Origin check at all. Any web page the operator visited could open
  ``ws://127.0.0.1:<port>/ws/status`` (classic CSWSH — the browser attaches no
  cookie requirement to a WebSocket and same-origin policy does not apply to the
  handshake) and drive node execution as the owner.
* ``/ws/internal`` — the Temporal activity-worker callback. Previously it called
  ``websocket.accept()`` unconditionally. Its handler allowlist
  (``ws_surface.INTERNAL_SOCKET_HANDLERS``) limits the blast radius but still
  admits ``execute_node`` / ``execute_ai_node``, which is the RCE surface.

Design, mirroring ``terminal_mount``:

* **Host allowlist first** — refuses a DNS-rebinding Host that resolves to
  127.0.0.1. Same loopback identities (incl. the bracketed IPv6 literal) the
  reference implementation trusts.
* **Origin is mandatory on a browser handshake.** ``terminal_mount`` documents
  the rule verbatim: "unlike ordinary token-authenticated HTTP clients, a
  browser WebSocket handshake must carry an explicit trusted loopback Origin. A
  cookie is accepted only with that same-origin proof." A missing Origin is a
  refusal, not a pass — that is what makes this fail-closed rather than
  degraded-open.
* **The internal socket is credentialed, not Origin-gated.** A service client
  sends no Origin, so the browser rule cannot apply; instead it must present a
  per-host shared token AND must NOT present an Origin (a browser always sends
  one, so an Origin on ``/ws/internal`` is by construction not the activity
  worker).

Node execution itself is deliberately untouched: the handshake is the fix.

The token is provisioned the way cipherd provisions its operator token — a
random value in a ``0600`` file under a ``0700`` directory, created on first
use, read by both the backend and the activity worker (same host, same uid).
That keeps the gate fail-closed without a manual bootstrap step and without a
secret in the repo. ``WORKFORCE_INTERNAL_WS_TOKEN`` in the isolated workforce
env file overrides it; nothing else is consulted, so no ``CIPHERD_*`` value can
reach this plane.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Header the activity worker presents on ``/ws/internal``.
INTERNAL_TOKEN_HEADER = "x-workforce-internal-token"

#: Optional header a non-browser client may present on ``/ws/status`` in place
#: of the same-origin proof (the token branch of the reference implementation).
STATUS_TOKEN_HEADER = "x-workforce-status-token"

_TOKEN_DIR = Path.home() / ".cipheros" / "workforce"
_TOKEN_PATH = _TOKEN_DIR / "internal_ws_token"

_WS_CLOSE_POLICY = 4403


def loopback_hosts(port: int) -> frozenset[str]:
    """Loopback Host header values trusted for this port.

    Only the three loopback identities, each pinned to the serving port — a
    Host naming any other name (a rebound attacker domain, a LAN address) is
    refused even though it resolved here.
    """
    return frozenset(
        {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        }
    )


def allowed_origins(port: int) -> frozenset[str]:
    """Origins that count as same-origin with this loopback server."""
    hosts = loopback_hosts(port)
    return frozenset({f"http://{host}" for host in hosts} | {f"https://{host}" for host in hosts})


def _header(websocket: Any, name: str) -> str:
    """Case-insensitive header read that tolerates a stub socket in tests."""
    headers = getattr(websocket, "headers", None)
    if headers is None:
        return ""
    try:
        return headers.get(name) or headers.get(name.title()) or ""
    except Exception:  # pragma: no cover - defensive against odd mappings
        return ""


def internal_ws_token() -> Optional[str]:
    """Resolve (or provision) the shared token for ``/ws/internal``.

    Order: the isolated workforce env file's ``WORKFORCE_INTERNAL_WS_TOKEN``,
    then a per-host file created on first use. Returns ``None`` only when the
    token can neither be read nor created — callers then refuse the connection,
    because a gate that cannot resolve its credential must fail closed.
    """
    try:
        from services.workforce_env import get_workforce_credential

        configured = get_workforce_credential("WORKFORCE_INTERNAL_WS_TOKEN")
        if configured:
            return configured
    except Exception:  # pragma: no cover - env loader is optional at import time
        pass

    try:
        if _TOKEN_PATH.exists():
            value = _TOKEN_PATH.read_text(encoding="utf-8").strip()
            if value:
                return value
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(_TOKEN_DIR, 0o700)
        value = secrets.token_urlsafe(32)
        fd = os.open(str(_TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode("utf-8"))
        finally:
            os.close(fd)
        return value
    except OSError as exc:
        logger.error("ws_gate: cannot resolve internal token: %s", exc)
        return None


def _host_ok(websocket: Any, port: int) -> bool:
    presented = _header(websocket, "host")
    if presented in loopback_hosts(port):
        return True
    logger.warning("ws_gate reject reason=untrusted_host host=%r", presented)
    return False


def authorize_status_ws(websocket: Any, port: int) -> bool:
    """Gate the browser socket: loopback Host + trusted Origin, or a token.

    Fail-closed by construction — every branch that is not an explicit match
    returns ``False``. A handshake with no Origin and no token is refused, which
    is precisely the CSWSH case ``VITE_AUTH_ENABLED=false`` used to admit.
    """
    if not _host_ok(websocket, port):
        return False

    origin = _header(websocket, "origin")
    if origin and origin in allowed_origins(port):
        return True
    if origin:
        logger.warning("ws_gate reject reason=cross_origin origin=%r", origin)
        return False

    # No Origin: not a browser handshake. Accept only a valid shared token.
    presented = _header(websocket, STATUS_TOKEN_HEADER)
    expected = internal_ws_token()
    if expected and presented and hmac.compare_digest(presented, expected):
        return True
    logger.warning("ws_gate reject reason=missing_origin_and_token")
    return False


def authorize_internal_ws(websocket: Any, port: int) -> bool:
    """Gate the service socket: loopback Host + shared token + no Origin."""
    if not _host_ok(websocket, port):
        return False

    origin = _header(websocket, "origin")
    if origin:
        # A browser always sends Origin; the activity worker never does.
        logger.warning("ws_gate reject reason=origin_on_internal origin=%r", origin)
        return False

    expected = internal_ws_token()
    if not expected:
        logger.error("ws_gate reject reason=no_internal_token_configured")
        return False
    presented = _header(websocket, INTERNAL_TOKEN_HEADER)
    if presented and hmac.compare_digest(presented, expected):
        return True
    logger.warning("ws_gate reject reason=bad_internal_token")
    return False


async def refuse(websocket: Any, reason: str) -> None:
    """Close an unauthorized handshake without accepting it."""
    try:
        await websocket.close(code=_WS_CLOSE_POLICY, reason=reason)
    except Exception:  # pragma: no cover - peer may already be gone
        pass


__all__ = [
    "INTERNAL_TOKEN_HEADER",
    "STATUS_TOKEN_HEADER",
    "allowed_origins",
    "authorize_internal_ws",
    "authorize_status_ws",
    "internal_ws_token",
    "loopback_hosts",
    "refuse",
]
