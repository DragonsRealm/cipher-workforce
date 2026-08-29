"""Server-side one-use dispatch-token store.

Dispatch tokens are minted at Gate-3 soul-spawn approval time, presented by
the soul process on its FIRST connection to /ws/internal (or on a REST call to
/workflow/node/execute), and consumed immediately on resolution — they cannot
be replayed.

The binding mechanism:

    1. ``issue_dispatch_token(soul_id)`` → returns a 256-bit hex token
       (stored in _token_store: token → soul_id).
    2. Soul process presents token as ``X-Workforce-Dispatch-Token`` header.
    3. ``resolve_dispatch_token(token)`` pops and returns the bound soul_id,
       or None if the token is unknown or already consumed.
    4. The caller (websocket endpoint or REST handler) writes the resolved
       soul_id to ``websocket.state.dispatch_soul_id`` — from that point on
       the identity is server-bound and unreachable from caller-supplied data.

This design matches the spec in the dispatch_token.py docstring Argus cited:
"per-dispatch one-use 256-bit token … not from anything the caller can inject
into the WS message payload."
"""

from __future__ import annotations

import secrets
from typing import Optional

# HTTP / WS header name the soul process presents its token under.
DISPATCH_TOKEN_HEADER = "x-workforce-dispatch-token"

# In-memory store: hex-token → soul_id.
# Tokens are 256-bit (32 bytes, 64 hex chars).  They live only until consumed.
_token_store: dict[str, str] = {}


def issue_dispatch_token(soul_id: str) -> str:
    """Mint a one-use 256-bit token bound to *soul_id*.

    Thread-safety: CPython dict assignments are GIL-protected; asyncio
    runs on a single thread by default, so no lock is needed here.
    """
    token = secrets.token_hex(32)
    _token_store[token] = soul_id
    return token


def resolve_dispatch_token(token: str) -> Optional[str]:
    """Resolve and consume *token*, returning the bound soul_id.

    Returns None if the token is unknown, already consumed, or malformed.
    The pop is atomic under CPython's GIL.
    """
    if not token or not isinstance(token, str):
        return None
    return _token_store.pop(token, None)
