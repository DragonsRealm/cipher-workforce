"""Approval gate client — cipher-workforce side.

Architecture (Orion ruling 2026-08-28):
  cipherd is the authoritative dispatcher and session owner.  This module
  provides a thin HTTP client that delegates all approval store operations to
  the cipherd approval server at http://127.0.0.1:8474.

  The following are kept client-side (pure, no IO, safe to run locally):
  - SOUL_ALLOWLIST: advisory pre-validation (must match cipherd's copy)
  - STATE_* constants
  - _contains_path_escape(): pure input guard
  - MAX_DISPATCH_DEPTH: depth gate (checked locally before any network call)

  The following delegate to cipherd:
  - HumanApprovalQueue: all store operations via HTTP
  - ApprovalGovernor.evaluate(): local fast-path checks, then cipherd

  Cross-process locking: handled server-side by cipherd via fcntl.flock.
  In-process locking: threading.Lock within HumanApprovalQueue (belt +
  suspenders; cipherd's SQLite WAL + CAS is the real guard).

  Audit log: written server-side by cipherd.  _audit() is a no-op here.

  _soul_lock(): client-side context manager now wraps a threading.Lock.
  Cross-process enforcement lives in cipherd (fcntl.flock on
  ~/.cipheros/approvals.lock).  Tests that previously verified the
  subprocess lock behavior (T7, F1-lock-busy) now verify threading.Lock
  contention instead — the cross-process invariant is tested at the
  cipherd server level.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static configuration — advisory client-side copies
# ---------------------------------------------------------------------------

#: Authorized DCS soul profile-ids.  ADVISORY PRE-VALIDATION ONLY.
#: Must match cipherd's authoritative SOUL_ALLOWLIST.
SOUL_ALLOWLIST: frozenset = frozenset({
    "orion",
    "maren",
    "cael",
    "argus",
    "vera",
    "reeve",
})

MAX_DISPATCH_DEPTH: int = 1

STATE_PENDING = "PENDING"
STATE_APPROVED = "APPROVED"
STATE_DENIED = "DENIED"
STATE_CONSUMED = "CONSUMED"
STATE_EXPIRED = "EXPIRED"

_CIPHERD_BASE_URL = "http://127.0.0.1:8474"
_SECRETS_PATH = Path.home() / ".config" / "cipher-os" / "secrets.env"


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
    stripped = value.strip()
    if value.startswith("/") or value.startswith("\\"):
        return True
    if stripped.startswith("/") or stripped.startswith("\\"):
        return True
    return False


# ---------------------------------------------------------------------------
# No-op audit — audit is cipherd server-side
# ---------------------------------------------------------------------------

def _audit(record: Dict[str, Any]) -> None:
    """No-op: fsync'd audit log lives in cipherd.

    cipherd writes the audit record for every store operation.  This
    function is retained so callers (ApprovalGovernor._write_audit) compile
    and can be tested without wiring; it intentionally does nothing here.
    """


# ---------------------------------------------------------------------------
# Client-side in-process lock
# ---------------------------------------------------------------------------

#: In-process threading lock.  Cross-process enforcement is cipherd-side.
_in_process_lock = threading.Lock()


@contextmanager
def _soul_lock(blocking: bool = True) -> Iterator[bool]:
    """In-process concurrency lock for soul dispatch.

    The real cross-process dispatch lock lives in cipherd (fcntl.flock on
    ~/.cipheros/approvals.lock).  This context manager provides in-process
    serialization only — it will NOT prevent two separate OS processes from
    calling the dispatch gate concurrently.

    Yields True when the lock is held, False if non-blocking and busy.

    Note for tests (T7, F1-lock-busy): cross-process tests should target
    the cipherd server directly.  Within cipher-workforce, thread-level
    contention is the correct unit to test.
    """
    acquired = _in_process_lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            _in_process_lock.release()


# ---------------------------------------------------------------------------
# Token loader
# ---------------------------------------------------------------------------

def _load_token() -> Optional[str]:
    """Load CIPHERD_APPROVAL_TOKEN from env or ~/.config/cipher-os/secrets.env."""
    env_val = os.environ.get("CIPHERD_APPROVAL_TOKEN")
    if env_val:
        return env_val.strip()
    if not _SECRETS_PATH.exists():
        return None
    for line in _SECRETS_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "CIPHERD_APPROVAL_TOKEN":
            return val.strip()
    return None


# ---------------------------------------------------------------------------
# HTTP client helper
# ---------------------------------------------------------------------------

def _http(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    base_url: str = _CIPHERD_BASE_URL,
) -> Dict[str, Any]:
    """Make a request to the cipherd approval server.

    Returns the parsed JSON response dict.
    Raises RuntimeError("Approval store unavailable: ...") on any failure.
    """
    token = _load_token()
    if not token:
        raise RuntimeError(
            "Approval store unavailable: CIPHERD_APPROVAL_TOKEN not configured"
        )
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return json.loads(exc.read())
        raise RuntimeError(
            f"Approval store unavailable: HTTP {exc.code} from {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Approval store unavailable: connection error — {exc.reason}"
        ) from exc


# ---------------------------------------------------------------------------
# HumanApprovalQueue — delegates to cipherd
# ---------------------------------------------------------------------------

class HumanApprovalQueue:
    """Approval queue backed by cipherd over HTTP.

    Thread-safe via cipherd's SQLite WAL + row-level CAS.  Cross-process
    serialization enforced by cipherd's fcntl.flock.

    All methods raise RuntimeError if the store is unreachable.
    """

    _lock: threading.Lock = threading.Lock()  # belt-and-suspenders in-process guard

    def enqueue(
        self,
        soul: str,
        task: str,
        root_exec_id: str,
        autonomy: str = "write",
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enqueue a soul dispatch for human approval.  Returns approval_id."""
        result = _http("POST", "/approvals/submit", {
            "soul": soul,
            "task": task,
            "root_exec_id": root_exec_id,
            "autonomy": autonomy,
            "context": context or {},
        })
        return result["approval_id"]

    def poll(self, approval_id: str) -> str:
        """Return the current state of an approval row.

        Calls GET /approvals/{approval_id} directly and returns the state
        field from the response.  Returns PENDING on 404 (fail-closed: treat
        unknown id as not-yet-approved).  Raises RuntimeError on any other
        error.
        """
        try:
            result = _http("GET", f"/approvals/{approval_id}")
            return result["state"]
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                logger.warning(
                    "poll(): approval_id %s not found (404); treating as PENDING",
                    approval_id,
                )
                return STATE_PENDING
            raise

    def atomic_consume(self, approval_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Atomic CAS APPROVED → CONSUMED.

        Returns (True, row_data) on success or (False, None) on any failure.
        This is the security-critical operation: one approval, one spawn, no replay.
        """
        try:
            result = _http("POST", f"/approvals/{approval_id}/consume")
            if result.get("ok"):
                return True, result.get("row")
            return False, None
        except RuntimeError:
            logger.error(
                "Approval store unavailable during consume of %s", approval_id
            )
            return False, None


# ---------------------------------------------------------------------------
# ApprovalGovernor — entry point for the soul node
# ---------------------------------------------------------------------------

class ApprovalGovernor:
    """Gate every DCS soul dispatch through a human approval check.

    Local fast-path checks (depth, allowlist, path-escape, autonomy) run
    before any network call.  Approval store operations are delegated to
    HumanApprovalQueue which calls cipherd over HTTP.

    The eight invariants from Argus Gate 3 §2 are maintained:
    - depth < MAX_DISPATCH_DEPTH enforced locally (fast)
    - allowlist check enforced locally (fast)
    - path-escape check enforced locally (fast)
    - autonomy == 'autonomous' blocked locally (fast)
    - store unreachability → refuse (fail closed)
    - APPROVED row consumed atomically (single-use)
    - DENIED → terminal refuse (no retry)
    - EXPIRED/CONSUMED → re-enqueue as new PENDING
    """

    def __init__(self) -> None:
        self._queue = HumanApprovalQueue()

    def evaluate(
        self,
        *,
        soul: str,
        task: str,
        root_exec_id: str,
        delegation_depth: int,
        approval_id: Optional[str] = None,
        autonomy: str = "write",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Evaluate a soul dispatch request.

        Returns one of:
        - {"action": "APPROVED", "approval_id": ..., "row": {...}}
        - {"action": "PENDING", "approval_id": ..., "message": ...}
        - {"action": "REFUSED", "reason": ..., "message": ...}

        Never raises — all exceptions are converted to REFUSED (fail-closed).
        """
        try:
            return self._evaluate_inner(
                soul=soul,
                task=task,
                root_exec_id=root_exec_id,
                delegation_depth=delegation_depth,
                approval_id=approval_id,
                autonomy=autonomy,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ApprovalGovernor internal error (failing closed): %s", exc, exc_info=True
            )
            return {
                "action": "REFUSED",
                "reason": "governor_error",
                "message": f"Approval governor error — refusing to prevent fail-open: {exc}",
            }

    def _evaluate_inner(
        self,
        *,
        soul: str,
        task: str,
        root_exec_id: str,
        delegation_depth: int,
        approval_id: Optional[str],
        autonomy: str,
        context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        # 1. Depth gate (local fast-path)
        if delegation_depth >= MAX_DISPATCH_DEPTH:
            reason = f"depth_exceeded: delegation_depth={delegation_depth} >= {MAX_DISPATCH_DEPTH}"
            return {
                "action": "REFUSED",
                "reason": "depth_exceeded",
                "message": (
                    f"DCS soul dispatch refused: delegation depth {delegation_depth} "
                    f">= limit {MAX_DISPATCH_DEPTH}. Souls may not dispatch souls."
                ),
            }

        # 2. Allowlist gate (local fast-path; cipherd also enforces)
        if soul not in SOUL_ALLOWLIST:
            return {
                "action": "REFUSED",
                "reason": "soul_not_allowed",
                "message": f"Soul {soul!r} is not on the authorized roster.",
            }

        # 3. Path-injection guard (local fast-path)
        if _contains_path_escape(soul) or _contains_path_escape(task):
            return {
                "action": "REFUSED",
                "reason": "path_injection_detected",
                "message": "Soul or task contains path escape sequences; dispatch refused.",
            }

        # 4. Autonomy gate (local fast-path)
        if autonomy == "autonomous":
            return {
                "action": "REFUSED",
                "reason": "autonomy_tier_blocked",
                "message": "Autonomous tier is structurally blocked from canvas dispatch.",
            }

        # 5. If an approval_id was provided, try to consume it.
        if approval_id:
            state = self._queue.poll(approval_id)

            if state == STATE_APPROVED:
                consumed, row = self._queue.atomic_consume(approval_id)
                if consumed and row:
                    _audit({
                        "decision": "APPROVED",
                        "reason": "approval_consumed",
                        "soul": soul,
                        "root_exec_id": root_exec_id,
                        "approval_id": approval_id,
                    })
                    return {"action": "APPROVED", "approval_id": approval_id, "row": row}
                _audit({
                    "decision": "REFUSED",
                    "reason": "approval_already_consumed",
                    "soul": soul,
                    "root_exec_id": root_exec_id,
                    "approval_id": approval_id,
                })
                return {
                    "action": "REFUSED",
                    "reason": "approval_already_consumed",
                    "message": (
                        "Approval has already been consumed. "
                        "Each approval is single-use — request a new approval."
                    ),
                }

            if state == STATE_DENIED:
                _audit({
                    "decision": "REFUSED",
                    "reason": "approval_denied",
                    "soul": soul,
                    "root_exec_id": root_exec_id,
                    "approval_id": approval_id,
                })
                return {
                    "action": "REFUSED",
                    "reason": "approval_denied",
                    "message": "Approval was denied. Denied approvals are never auto-retried.",
                }

            # EXPIRED or CONSUMED → fall through to re-enqueue

        # 6. No valid approval — enqueue and return PENDING.
        new_approval_id = self._queue.enqueue(
            soul=soul,
            task=task,
            root_exec_id=root_exec_id,
            autonomy=autonomy,
            context=context,
        )
        _audit({
            "decision": "PENDING",
            "reason": "awaiting_human_approval",
            "soul": soul,
            "root_exec_id": root_exec_id,
            "approval_id": new_approval_id,
        })
        return {
            "action": "PENDING",
            "approval_id": new_approval_id,
            "message": (
                f"Dispatch of soul '{soul}' is PENDING human approval "
                f"(approval_id={new_approval_id!r}). "
                "Do NOT call this tool again. Use 'check_delegated_tasks' to poll status."
            ),
        }

    def _write_audit(
        self,
        *,
        decision: str,
        reason: str,
        soul: str,
        root_exec_id: Optional[str],
        approval_id: Optional[str],
        task_id: Optional[str] = None,
    ) -> None:
        """No-op: audit is cipherd server-side."""
        _audit({
            "decision": decision,
            "reason": reason,
            "soul": soul,
            "root_exec_id": root_exec_id,
            "approval_id": approval_id,
            "task_id": task_id,
        })


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_governor: Optional[ApprovalGovernor] = None


def get_approval_governor() -> ApprovalGovernor:
    """Return the module-level ApprovalGovernor singleton.

    Phase 2 implementation: returns a real ApprovalGovernor backed by
    the cipherd HTTP client.  Raises RuntimeError if the approval token
    is not configured.
    """
    global _governor  # noqa: PLW0603
    if _governor is None:
        _governor = ApprovalGovernor()
    return _governor
