"""ApprovalGovernor — human-in-loop gate for DCS soul dispatch.

Architecture (Argus Gate 3 C1–C5):
- HumanApprovalQueue: durable SQLite store under ~/.cipheros/soul_approvals.db
- Cross-process concurrency lock: fcntl.flock on ~/.cipheros/soul_dispatch.lock
- Fsync'd audit log: append-only JSONL at ~/.cipheros/audit/soul_dispatch.jsonl
- Atomic consume: single CAS UPDATE WHERE state='APPROVED' → 'CONSUMED'

Security invariants (non-negotiable):
- No spawn before consume — write-then-read-back; an unread write is not a check
- Missing store ≠ no approval required — unreachable store → refuse
- Never fail open — any exception → refuse
- Write failure in audit → refuse (C4)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static configuration (non-negotiable; no env-var override by design)
# ---------------------------------------------------------------------------

#: Authorized DCS soul profile-ids. Adding a soul requires a code change +
#: Argus sign-off, per V2 plan §4.
SOUL_ALLOWLIST: frozenset[str] = frozenset({
    "orion",
    "maren",
    "cael",
    "argus",
    "vera",
    "reeve",
})

#: Approval row TTL in seconds (30 min). An expired APPROVED row is treated
#: as absent — the approver must re-approve after expiry.
APPROVAL_TTL_SECONDS: int = 1800

#: Maximum depth at which a soul dispatch is permitted. A soul node at
#: delegation_depth >= MAX_DISPATCH_DEPTH is refused — souls cannot
#: recursively dispatch souls.
MAX_DISPATCH_DEPTH: int = 1

# ---------------------------------------------------------------------------
# On-disk paths
# ---------------------------------------------------------------------------

_STATE_DIR: Path = Path.home() / ".cipheros"
_DB_PATH: Path = _STATE_DIR / "soul_approvals.db"
_LOCK_PATH: Path = _STATE_DIR / "soul_dispatch.lock"
_AUDIT_DIR: Path = _STATE_DIR / "audit"
_AUDIT_PATH: Path = _AUDIT_DIR / "soul_dispatch.jsonl"

# ---------------------------------------------------------------------------
# Approval row states
# ---------------------------------------------------------------------------

STATE_PENDING = "PENDING"
STATE_APPROVED = "APPROVED"
STATE_DENIED = "DENIED"
STATE_CONSUMED = "CONSUMED"
STATE_EXPIRED = "EXPIRED"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    """Create state directories if absent. Fail loud if impossible."""
    _STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _AUDIT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _open_db() -> sqlite3.Connection:
    """Open the approvals DB (WAL mode, check_same_thread=False)."""
    _ensure_dirs()
    conn = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS soul_approvals (
            approval_id   TEXT PRIMARY KEY,
            root_exec_id  TEXT NOT NULL,
            soul          TEXT NOT NULL,
            task_fp       TEXT NOT NULL,
            state         TEXT NOT NULL DEFAULT 'PENDING',
            created_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            approver      TEXT,
            task_id       TEXT,
            notes         TEXT
        )
    """)
    conn.commit()
    return conn


def _task_fingerprint(soul: str, task: str, root_exec_id: str) -> str:
    """Stable fingerprint for (root_exec_id, soul, task) — not a security control."""
    raw = f"{root_exec_id}:{soul}:{task}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit(record: Dict[str, Any]) -> None:
    """Append one record to the fsync'd audit log.

    MUST succeed — caller refuses on IOError (C4).
    """
    _ensure_dirs()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# Cross-process concurrency lock
# ---------------------------------------------------------------------------

@contextmanager
def _soul_lock(blocking: bool = True) -> Iterator[bool]:
    """Acquire the durable cross-process concurrency lock.

    Uses fcntl.LOCK_EX on a lockfile under ~/.cipheros/.  A process holds at
    most one live soul dispatch at a time (concurrency limit = 1 per Argus
    ruling §4).

    Yields True when the lock is held, False if non-blocking and busy.
    """
    _ensure_dirs()
    flag = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fh = open(_LOCK_PATH, "w")  # noqa: WPS515
    except OSError as exc:
        raise RuntimeError(f"Cannot open soul dispatch lock file: {exc}") from exc
    try:
        try:
            fcntl.flock(fh.fileno(), flag)
        except BlockingIOError:
            fh.close()
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# HumanApprovalQueue
# ---------------------------------------------------------------------------

class HumanApprovalQueue:
    """Durable approval queue backed by SQLite.

    Thread-safe and cross-process via WAL mode + row-level CAS.
    """

    def enqueue(
        self,
        soul: str,
        task: str,
        root_exec_id: str,
        autonomy: str = "write",
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enqueue a soul dispatch for human approval.

        Returns the approval_id to poll.  Never raises on duplicate — returns
        the existing PENDING row's approval_id instead.
        """
        fp = _task_fingerprint(soul, task, root_exec_id)
        now = time.time()
        expires = now + APPROVAL_TTL_SECONDS

        try:
            conn = _open_db()
            with conn:
                # Idempotent: return existing PENDING row for same fingerprint
                row = conn.execute(
                    "SELECT approval_id FROM soul_approvals "
                    "WHERE root_exec_id=? AND soul=? AND task_fp=? AND state=?",
                    (root_exec_id, soul, fp, STATE_PENDING),
                ).fetchone()
                if row:
                    return row[0]

                approval_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO soul_approvals
                        (approval_id, root_exec_id, soul, task_fp, state,
                         created_at, expires_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        root_exec_id,
                        soul,
                        fp,
                        STATE_PENDING,
                        now,
                        expires,
                        json.dumps({"autonomy": autonomy, "context": context or {}}),
                    ),
                )
            return approval_id
        except sqlite3.Error as exc:
            raise RuntimeError(f"Approval store unavailable: {exc}") from exc
        finally:
            conn.close()

    def poll(self, approval_id: str) -> str:
        """Return the current state of an approval row.

        Returns one of: PENDING, APPROVED, DENIED, CONSUMED, EXPIRED.
        Raises RuntimeError if the store is unreachable.
        """
        try:
            conn = _open_db()
            try:
                row = conn.execute(
                    "SELECT state, expires_at FROM soul_approvals WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    return STATE_PENDING  # unknown id treated as pending
                state, expires_at = row
                if state == STATE_APPROVED and time.time() > expires_at:
                    return STATE_EXPIRED
                return state
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Approval store unavailable: {exc}") from exc

    def approve(
        self,
        approval_id: str,
        approver: str = "dragon",
        task_id: Optional[str] = None,
    ) -> bool:
        """Mark an approval row APPROVED.  Returns True on success."""
        try:
            conn = _open_db()
            with conn:
                cur = conn.execute(
                    "UPDATE soul_approvals SET state=?, approver=?, task_id=? "
                    "WHERE approval_id=? AND state=?",
                    (STATE_APPROVED, approver, task_id, approval_id, STATE_PENDING),
                )
                return cur.rowcount == 1
        except sqlite3.Error as exc:
            raise RuntimeError(f"Approval store unavailable: {exc}") from exc
        finally:
            conn.close()

    def deny(self, approval_id: str, approver: str = "dragon") -> bool:
        """Mark an approval row DENIED.  Returns True on success."""
        try:
            conn = _open_db()
            with conn:
                cur = conn.execute(
                    "UPDATE soul_approvals SET state=?, approver=? "
                    "WHERE approval_id=? AND state=?",
                    (STATE_DENIED, approver, approval_id, STATE_PENDING),
                )
                return cur.rowcount == 1
        except sqlite3.Error as exc:
            raise RuntimeError(f"Approval store unavailable: {exc}") from exc
        finally:
            conn.close()

    def atomic_consume(self, approval_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Atomically consume an APPROVED row (CAS APPROVED → CONSUMED).

        Returns (True, row_data) on success or (False, None) if the row is
        absent, expired, denied, already consumed, or the store is
        unreachable.

        This is the security-critical operation: one approval, one spawn, no
        replay.
        """
        try:
            conn = _open_db()
            try:
                now = time.time()
                with conn:
                    cur = conn.execute(
                        "UPDATE soul_approvals SET state=? "
                        "WHERE approval_id=? AND state=? AND expires_at > ?",
                        (STATE_CONSUMED, approval_id, STATE_APPROVED, now),
                    )
                    if cur.rowcount != 1:
                        return False, None
                    row = conn.execute(
                        "SELECT soul, task_fp, approver, task_id, root_exec_id "
                        "FROM soul_approvals WHERE approval_id=?",
                        (approval_id,),
                    ).fetchone()
                    if row is None:
                        return False, None
                    return True, {
                        "approval_id": approval_id,
                        "soul": row[0],
                        "task_fp": row[1],
                        "approver": row[2],
                        "task_id": row[3],
                        "root_exec_id": row[4],
                    }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("Approval store unavailable during consume: %s", exc)
            return False, None


# ---------------------------------------------------------------------------
# ApprovalGovernor — entry point for the soul node
# ---------------------------------------------------------------------------

class ApprovalGovernor:
    """Gate every DCS soul dispatch through a human approval check.

    This class is the structural barrier Argus Gate 3 requires.  The soul
    node MUST call ``evaluate`` before any subprocess is spawned; it MUST
    NOT bypass this class for any code path that reaches dispatch.

    The eight invariants from §2 of the Gate 3 ruling are enforced here.
    """

    def __init__(self) -> None:
        self._queue = HumanApprovalQueue()

    # ------------------------------------------------------------------
    # Public API used by the soul node
    # ------------------------------------------------------------------

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
        - ``{"action": "APPROVED", "approval_id": ..., "row": {...}}``
          — caller may spawn (after acquiring the lock).
        - ``{"action": "PENDING", "approval_id": ..., "message": ...}``
          — caller must return ApprovalPending to the model.
        - ``{"action": "REFUSED", "reason": ..., "message": ...}``
          — caller must return a named refusal; terminal, no retry.

        Never raises — all exceptions are converted to REFUSED.  This is
        the fail-closed guarantee (Argus §2 MUST NEVER: fail open).
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
            logger.error("ApprovalGovernor internal error (failing closed): %s", exc, exc_info=True)
            self._write_audit(
                decision="REFUSED",
                reason=f"governor_error: {exc}",
                soul=soul,
                root_exec_id=root_exec_id,
                approval_id=approval_id,
            )
            return {
                "action": "REFUSED",
                "reason": "governor_error",
                "message": f"Approval governor error — refusing to prevent fail-open: {exc}",
            }

    # ------------------------------------------------------------------
    # Internal evaluation logic
    # ------------------------------------------------------------------

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
        # 1. Depth gate — only root may dispatch a soul.
        if delegation_depth >= MAX_DISPATCH_DEPTH:
            reason = f"depth_exceeded: delegation_depth={delegation_depth} >= {MAX_DISPATCH_DEPTH}"
            self._write_audit(
                decision="REFUSED",
                reason=reason,
                soul=soul,
                root_exec_id=root_exec_id,
                approval_id=approval_id,
            )
            return {
                "action": "REFUSED",
                "reason": "depth_exceeded",
                "message": (
                    f"DCS soul dispatch refused: delegation depth {delegation_depth} "
                    f">= limit {MAX_DISPATCH_DEPTH}. Souls may not dispatch souls."
                ),
            }

        # 2. Allowlist gate — soul must be in the static roster.
        if soul not in SOUL_ALLOWLIST:
            reason = f"soul_not_allowed: {soul!r}"
            self._write_audit(
                decision="REFUSED",
                reason=reason,
                soul=soul,
                root_exec_id=root_exec_id,
                approval_id=approval_id,
            )
            return {
                "action": "REFUSED",
                "reason": "soul_not_allowed",
                "message": f"Soul {soul!r} is not on the authorized roster.",
            }

        # 3. Path-injection guard — reject traversal or absolute-path content.
        if _contains_path_escape(soul) or _contains_path_escape(task):
            reason = "path_injection_detected"
            self._write_audit(
                decision="REFUSED",
                reason=reason,
                soul=soul,
                root_exec_id=root_exec_id,
                approval_id=approval_id,
            )
            return {
                "action": "REFUSED",
                "reason": "path_injection_detected",
                "message": "Soul or task contains path escape sequences; dispatch refused.",
            }

        # 4. Autonomy gate — 'autonomous' tier is structurally blocked.
        if autonomy == "autonomous":
            reason = "autonomy_tier_blocked"
            self._write_audit(
                decision="REFUSED",
                reason=reason,
                soul=soul,
                root_exec_id=root_exec_id,
                approval_id=approval_id,
            )
            return {
                "action": "REFUSED",
                "reason": "autonomy_tier_blocked",
                "message": "Autonomous tier is structurally blocked from canvas dispatch.",
            }

        # 5. Approval store gate — must be reachable.
        try:
            _open_db().close()
        except Exception as exc:  # noqa: BLE001
            reason = f"store_unreachable: {exc}"
            # Cannot write audit here — store is down; log only.
            logger.error("Approval store unreachable — refusing: %s", exc)
            return {
                "action": "REFUSED",
                "reason": "store_unreachable",
                "message": "Approval store unreachable — refusing to prevent fail-open.",
            }

        # 6. If an approval_id was provided, try to consume it.
        if approval_id:
            state = self._queue.poll(approval_id)

            if state == STATE_APPROVED:
                consumed, row = self._queue.atomic_consume(approval_id)
                if consumed and row:
                    self._write_audit(
                        decision="APPROVED",
                        reason="approval_consumed",
                        soul=soul,
                        root_exec_id=root_exec_id,
                        approval_id=approval_id,
                    )
                    return {"action": "APPROVED", "approval_id": approval_id, "row": row}
                # Consume failed — already consumed or race condition
                self._write_audit(
                    decision="REFUSED",
                    reason="approval_already_consumed",
                    soul=soul,
                    root_exec_id=root_exec_id,
                    approval_id=approval_id,
                )
                return {
                    "action": "REFUSED",
                    "reason": "approval_already_consumed",
                    "message": (
                        "Approval has already been consumed. "
                        "Each approval is single-use — request a new approval."
                    ),
                }

            if state == STATE_DENIED:
                self._write_audit(
                    decision="REFUSED",
                    reason="approval_denied",
                    soul=soul,
                    root_exec_id=root_exec_id,
                    approval_id=approval_id,
                )
                return {
                    "action": "REFUSED",
                    "reason": "approval_denied",
                    "message": "Approval was denied. Denied approvals are never auto-retried.",
                }

            if state in (STATE_EXPIRED, STATE_CONSUMED):
                # Expired or already used — fall through to re-enqueue as new PENDING
                pass

        # 7. No valid approval — enqueue and return PENDING.
        new_approval_id = self._queue.enqueue(
            soul=soul,
            task=task,
            root_exec_id=root_exec_id,
            autonomy=autonomy,
            context=context,
        )
        self._write_audit(
            decision="PENDING",
            reason="awaiting_human_approval",
            soul=soul,
            root_exec_id=root_exec_id,
            approval_id=new_approval_id,
        )
        return {
            "action": "PENDING",
            "approval_id": new_approval_id,
            "message": (
                f"Dispatch of soul '{soul}' is PENDING human approval "
                f"(approval_id={new_approval_id!r}). "
                "Do NOT call this tool again. Use 'check_delegated_tasks' to poll status."
            ),
        }

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

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
        """Write a durable, fsync'd audit record.

        If the write fails, re-raise — the caller converts this to REFUSED
        (C4: write failure is a refusal, never a warning).
        """
        record = {
            "ts": time.time(),
            "decision": decision,
            "reason": reason,
            "soul": soul,
            "root_exec_id": root_exec_id,
            "approval_id": approval_id,
            "task_id": task_id,
        }
        _audit(record)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contains_path_escape(value: str) -> bool:
    """Return True if value contains path traversal or absolute-path indicators.

    Rejects:
    - Directory traversal sequences (``../``, ``..\\``)
    - Null bytes
    - Percent-encoded traversal/separator characters
    - Strings that start with ``/`` or ``\`` (absolute paths)

    Does NOT reject every string containing a forward slash — task briefs
    legitimately contain URLs, Unix-style arguments, and file paths.
    """
    if not isinstance(value, str):
        return True
    traversal_checks = (
        "../",
        ".." + os.sep,
        "\x00",
        "%2e",
        "%2f",
        "%5c",
    )
    lower = value.lower()
    if any(c in lower for c in traversal_checks):
        return True
    # Reject absolute-path inputs (start with / or \).
    if value.startswith("/") or value.startswith("\\"):
        return True
    return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_governor: Optional[ApprovalGovernor] = None


def get_approval_governor() -> ApprovalGovernor:
    """Return the module-level ApprovalGovernor singleton."""
    global _governor  # noqa: PLW0603
    if _governor is None:
        _governor = ApprovalGovernor()
    return _governor
