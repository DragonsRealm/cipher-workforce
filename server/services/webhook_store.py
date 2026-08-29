"""Webhook event persistence and HMAC verification for DCS capability plane.

Augments the existing ``server/routers/webhook.py`` with:
1. HMAC-SHA256 signature verification for inbound webhook requests.
2. Persistent event storage in ``~/.cipheros/workforce/webhooks.db``.
3. Named handler registration for DCS soul workflows.

Argus-flagged: HMAC secret is loaded from the isolated workforce env
(``WORKFORCE_WEBHOOK_HMAC_SECRET``). Never from ``os.environ``.

Design:
- Events are append-only (never mutated after insert).
- HMAC is optional per path — paths without a registered secret accept
  unauthenticated events (same behaviour as upstream webhook router).
- Handler registration is in-process only (not persisted); handlers must
  re-register on server restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

_DB_DIR = Path.home() / ".cipheros" / "workforce"
_DB_PATH = _DB_DIR / "webhooks.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL,
    method      TEXT    NOT NULL,
    headers     TEXT    NOT NULL,   -- JSON
    body        TEXT    NOT NULL,
    received_at REAL    NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0  -- 1 = HMAC verified, 0 = unverified/no secret
);
CREATE INDEX IF NOT EXISTS idx_events_path ON webhook_events(path);
CREATE INDEX IF NOT EXISTS idx_events_received ON webhook_events(received_at);
"""

# In-process handler registry: path -> async callable
_HANDLERS: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}


def _get_conn() -> sqlite3.Connection:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _load_hmac_secret() -> Optional[bytes]:
    """Load the HMAC secret from isolated env. Returns None if not configured.

    Argus-flagged: sole credential-read site for webhook HMAC.
    """
    from services.workforce_env import get_workforce_credential

    secret = get_workforce_credential("WORKFORCE_WEBHOOK_HMAC_SECRET")
    return secret.encode("utf-8") if secret else None


def verify_hmac(payload: bytes, signature_header: Optional[str]) -> bool:
    """Verify an inbound webhook HMAC-SHA256 signature.

    Signature header format: ``sha256=<hex_digest>`` (GitHub/Stripe style).

    Returns:
        ``True`` if verified or if no HMAC secret is configured (open).
        ``False`` if a secret is configured and the signature is absent or wrong.
    """
    secret = _load_hmac_secret()
    if secret is None:
        # No secret configured — accept unauthenticated (consistent with
        # upstream behaviour; callers may enforce via path-level registration)
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    # constant-time comparison
    return hmac.compare_digest(expected.encode("utf-8"), signature_header.encode("utf-8"))


def store_event(
    path: str,
    method: str,
    headers: dict,
    body: str,
    verified: bool = False,
) -> int:
    """Persist a webhook event. Returns the new row id."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO webhook_events (path, method, headers, body, received_at, verified) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, method, json.dumps(headers), body, time.time(), int(verified)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_events(path: str, limit: int = 50) -> list[dict]:
    """Return the most recent ``limit`` events for ``path``."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, path, method, headers, body, received_at, verified "
            "FROM webhook_events WHERE path = ? ORDER BY received_at DESC LIMIT ?",
            (path, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "id": r[0],
            "path": r[1],
            "method": r[2],
            "headers": json.loads(r[3]),
            "body": r[4],
            "received_at": r[5],
            "verified": bool(r[6]),
        }
        for r in rows
    ]


def register_webhook_handler(path: str, callback: Callable[[dict], Awaitable[None]]) -> None:
    """Register an async handler for inbound events at ``path``.

    Multiple handlers may be registered for the same path — all are called.
    Re-registration on restart is the caller's responsibility.
    """
    _HANDLERS.setdefault(path, []).append(callback)


async def dispatch_to_handlers(path: str, event: dict) -> None:
    """Call all registered handlers for ``path`` with the event dict."""
    for handler in _HANDLERS.get(path, []):
        await handler(event)
