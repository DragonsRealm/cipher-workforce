"""Approval gate stub — Gate 3 authority now lives in cipherd.

Orion ruling 2026-08-28: the canonical ApprovalGovernor / HumanApprovalQueue
implementation moves to cipherd.  This module provides:

- Pure constants and helpers that are safe to keep client-side (SOUL_ALLOWLIST,
  STATE_*, _contains_path_escape).
- Stub classes / functions for everything that requires the approval DB or
  the dispatch lock.  All stubs raise NotImplementedError with a message that
  names the Phase 2 work item: wire a cipherd HTTP client here.

Phase 2 work item (cipher-workforce):
  Replace the NotImplementedError stubs below with a thin async HTTP client
  that calls cipherd's approval REST endpoints (to be provisioned).
  Endpoint contract TBD by Orion/Maren.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

# ---------------------------------------------------------------------------
# Static constants — safe to keep client-side
# ---------------------------------------------------------------------------

#: Authorized DCS soul profile-ids.  ADVISORY PRE-VALIDATION ONLY.
#:
#: This is a client-side cache that must match cipherd's authoritative
#: allowlist.  It gates dispatch early (before a network call) but is NOT
#: the enforcement boundary — cipherd enforces at the authoritative gate.
#:
#: Keeping it in sync:
#:   - Adding or removing a soul here requires a matching change in cipherd.
#:   - Argus sign-off required for any change to this set.
#:   - The Phase 2 cipherd HTTP client must verify against cipherd's live
#:     allowlist, not solely against this local copy.
SOUL_ALLOWLIST: frozenset = frozenset({
    "orion",
    "maren",
    "cael",
    "argus",
    "vera",
    "reeve",
})

#: Maximum delegation depth at which a soul dispatch is permitted.
MAX_DISPATCH_DEPTH: int = 1

#: Approval row state constants (mirror cipherd wire values).
STATE_PENDING = "PENDING"
STATE_APPROVED = "APPROVED"
STATE_DENIED = "DENIED"
STATE_CONSUMED = "CONSUMED"
STATE_EXPIRED = "EXPIRED"

_NOT_WIRED = (
    "approval gate lives in cipherd — wire the client in Phase 2 "
    "(cipher-workforce: replace cipherd_approval stubs with HTTP client)"
)


# ---------------------------------------------------------------------------
# Pure helper — kept client-side (no IO, no state)
# ---------------------------------------------------------------------------

def _contains_path_escape(value: str) -> bool:
    r"""Return True if value contains path traversal or absolute-path indicators.

    Pure function — no DB or FS access.  Kept client-side so dcs_soul/__init__.py
    can validate inputs before calling the remote gate.

    Checks performed (case-insensitive on traversal tokens):
      - Unix traversal:  "../"
      - Windows traversal: "..\\" (os.sep) and the literal "..\" sequence
      - Percent-encoded variants: %2e (dot), %2f (slash), %5c (backslash)
      - Null byte: "\x00"
      - Absolute paths: leading "/" or "\"
      - Leading-whitespace absolute paths: value.strip() starting with "/" or "\"
    """
    if not isinstance(value, str):
        return True
    traversal_checks = (
        "../",
        ".." + os.sep,
        "..\\" ,           # Windows literal traversal (Argus F2 M1)
        "\x00",
        "%2e",
        "%2f",
        "%5c",
    )
    lower = value.lower()
    if any(c in lower for c in traversal_checks):
        return True
    # Check both raw and stripped value so " /etc/passwd" (leading space) is caught
    # (Argus F2 M2)
    stripped = value.strip()
    if value.startswith("/") or value.startswith("\\"):
        return True
    if stripped.startswith("/") or stripped.startswith("\\"):
        return True
    return False


# ---------------------------------------------------------------------------
# Stubs — raise NotImplementedError until the cipherd client is wired
# ---------------------------------------------------------------------------

@contextmanager
def _soul_lock(blocking: bool = True) -> Iterator[bool]:
    """Stub: cross-process dispatch lock — lives in cipherd in Phase 2."""
    raise NotImplementedError(_NOT_WIRED)
    yield  # unreachable; satisfies the type system


def _audit(record: Dict[str, Any]) -> None:
    """Stub: fsync'd audit log — lives in cipherd in Phase 2."""
    raise NotImplementedError(_NOT_WIRED)


class HumanApprovalQueue:
    """Stub: durable approval queue — lives in cipherd in Phase 2."""

    def enqueue(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError(_NOT_WIRED)

    def poll(self, approval_id: str) -> str:
        raise NotImplementedError(_NOT_WIRED)

    def approve(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError(_NOT_WIRED)

    def deny(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError(_NOT_WIRED)

    def atomic_consume(self, approval_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        raise NotImplementedError(_NOT_WIRED)


class ApprovalGovernor:
    """Stub: approval governor — Gate 3 authority moved to cipherd (Phase 2).

    Phase 2: replace evaluate() with a call to the cipherd approval REST
    endpoint.  The return shape must remain identical so dcs_soul/__init__.py
    needs no further changes.
    """

    def __init__(self) -> None:
        self._queue = HumanApprovalQueue()

    def evaluate(self, **kwargs: Any) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_WIRED)


_governor: Optional[ApprovalGovernor] = None


def get_approval_governor() -> ApprovalGovernor:
    """Return the module-level ApprovalGovernor stub singleton.

    Phase 2: replace this with a cipherd client factory.
    """
    raise NotImplementedError(_NOT_WIRED)
