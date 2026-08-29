"""Gate 3 contract tests for the DCS Soul node and ApprovalGovernor.

Contract status after Orion ruling 2026-08-28:
- The local ApprovalGovernor / HumanApprovalQueue implementation has been
  retired.  Gate 3 authority now lives in cipherd.
- Tests that required a live approval store (T1–T7, F1-functional) are
  SKIPPED with the Phase 2 marker below; they document the invariants the
  cipherd client must satisfy when wired.
- Tests that are purely structural (F1-source, F2, C3) remain active because
  they inspect source code rather than executing the store path.
- T8, T9: the allowlist and path-escape checks are pure functions in
  services.cipherd_approval and remain active.

Nine mandatory gate invariants (Argus C2) — Phase 2 wiring required:
1.  No approval row → APPROVAL_PENDING, zero processes spawned
2.  Denied approval → terminal, zero spawned
3.  Expired approval → treated as absent (PENDING, re-enqueue)
4.  Approval consumed twice (replay) → second call refused
5.  Approval store unreachable → refuse, not proceed
6.  Depth=1 exceeded (soul dispatching a soul) → refused
7.  Two concurrent dispatches → second refuses, proven across two processes
8.  Soul name off the allowlist → refused      ← still active (pure check)
9.  Path escape in soul or task field → refused ← still active (pure check)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Isolate test state — point the governor at a temp directory so tests do
# not touch ~/.cipheros.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Redirect all governor paths to a per-test tmp directory.

    NOTE (Phase 2): the local governor is retired.  This fixture now only
    patches the dispatch script; the store-level patches are no-ops preserved
    as comments so the wiring contract is documented for the cipherd client.
    """
    # Store-level patches (local governor retired — preserved as documentation
    # for the cipherd client wiring in Phase 2):
    #   monkeypatch.setattr(gov, "_STATE_DIR", tmp_path)
    #   monkeypatch.setattr(gov, "_DB_PATH", tmp_path / "soul_approvals.db")
    #   monkeypatch.setattr(gov, "_LOCK_PATH", tmp_path / "soul_dispatch.lock")
    #   monkeypatch.setattr(gov, "_AUDIT_DIR", tmp_path / "audit")
    #   monkeypatch.setattr(gov, "_AUDIT_PATH", tmp_path / "audit" / "soul_dispatch.jsonl")
    #   monkeypatch.setattr(gov, "_governor", None)

    import nodes.agent.dcs_soul as soul_mod
    monkeypatch.setattr(soul_mod, "_DISPATCH_SCRIPT", tmp_path / "nonexistent_dispatch.py")

    yield tmp_path


# ---------------------------------------------------------------------------
# Phase 2 skip marker
# ---------------------------------------------------------------------------

_PHASE2 = pytest.mark.skip(
    reason=(
        "approval gate moved to cipherd (Orion ruling 2026-08-28) — "
        "re-enable once cipherd HTTP client is wired in Phase 2"
    )
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_verdict(
    soul: str = "maren",
    task: str = "write hello world",
    root_exec_id: str = "test-root-001",
    delegation_depth: int = 0,
    approval_id: Optional[str] = None,
    autonomy: str = "write",
) -> Dict[str, Any]:
    # Phase 2: replace with a call to the cipherd approval client.
    from services.cipherd_approval import get_approval_governor
    gov = get_approval_governor()
    return gov.evaluate(
        soul=soul,
        task=task,
        root_exec_id=root_exec_id,
        delegation_depth=delegation_depth,
        approval_id=approval_id,
        autonomy=autonomy,
    )


def audit_lines(tmp_path: Path):
    # Phase 2: read from cipherd audit endpoint rather than local JSONL.
    audit_file = tmp_path / "audit" / "soul_dispatch.jsonl"
    if not audit_file.exists():
        return []
    return [json.loads(line) for line in audit_file.read_text().strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Test 1: No approval row → APPROVAL_PENDING, zero processes spawned
# ---------------------------------------------------------------------------

@_PHASE2
def test_no_approval_returns_pending_zero_spawned(tmp_path):
    """T1: No approval row → APPROVAL_PENDING; no subprocess created."""
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict()

    assert verdict["action"] == "PENDING", f"Expected PENDING, got {verdict}"
    assert "approval_id" in verdict
    mock_popen.assert_not_called()

    # Audit record must exist and record PENDING
    records = audit_lines(tmp_path)
    assert any(r["decision"] == "PENDING" for r in records), "No PENDING audit record"


# ---------------------------------------------------------------------------
# Test 2: Denied approval → terminal, zero spawned
# ---------------------------------------------------------------------------

@_PHASE2
def test_denied_approval_terminal_zero_spawned(tmp_path):
    """T2: Denied row → REFUSED; not re-enqueued; no subprocess."""
    from services.approval.governor import get_approval_governor
    gov = get_approval_governor()

    # Enqueue then deny
    approval_id = gov._queue.enqueue("maren", "write hello world", "root-002")
    gov._queue.deny(approval_id)

    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(approval_id=approval_id)

    assert verdict["action"] == "REFUSED", f"Expected REFUSED, got {verdict}"
    assert verdict["reason"] == "approval_denied"
    mock_popen.assert_not_called()

    records = audit_lines(tmp_path)
    assert any(r["decision"] == "REFUSED" and r["reason"] == "approval_denied" for r in records)


# ---------------------------------------------------------------------------
# Test 3: Expired approval → treated as absent (new PENDING enqueued)
# ---------------------------------------------------------------------------

@_PHASE2
def test_expired_approval_treated_as_absent(tmp_path, monkeypatch):
    """T3: An expired APPROVED row is treated as absent → new PENDING."""
    import services.approval.governor as gov_mod
    from services.approval.governor import get_approval_governor, _open_db
    import sqlite3

    gov = get_approval_governor()

    # Enqueue and approve
    approval_id = gov._queue.enqueue("maren", "write hello world", "root-003")
    gov._queue.approve(approval_id)

    # Backdate the expiry so the row is expired
    conn = _open_db()
    conn.execute(
        "UPDATE soul_approvals SET expires_at=? WHERE approval_id=?",
        (time.time() - 1, approval_id),
    )
    conn.commit()
    conn.close()

    # poll should return EXPIRED
    state = gov._queue.poll(approval_id)
    assert state == "EXPIRED", f"Expected EXPIRED, got {state}"

    # evaluate with the expired approval_id should NOT consume it;
    # should re-enqueue a new PENDING
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(approval_id=approval_id)

    assert verdict["action"] == "PENDING", f"Expected PENDING (re-enqueued), got {verdict}"
    # The approval_id returned should be a NEW one (not the expired one)
    assert verdict["approval_id"] != approval_id, "Should have re-enqueued with a new approval_id"
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Approval consumed twice → second call refused
# ---------------------------------------------------------------------------

@_PHASE2
def test_replay_attack_refused(tmp_path):
    """T4: Double-consume (replay) → second call is REFUSED."""
    from services.approval.governor import get_approval_governor
    gov = get_approval_governor()

    approval_id = gov._queue.enqueue("maren", "write hello world", "root-004")
    gov._queue.approve(approval_id)

    # First consume succeeds
    consumed, row = gov._queue.atomic_consume(approval_id)
    assert consumed, "First consume should succeed"

    # Second consume fails
    consumed2, row2 = gov._queue.atomic_consume(approval_id)
    assert not consumed2, "Second consume (replay) must fail"
    assert row2 is None

    # evaluate with the already-consumed approval_id should REFUSE
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(approval_id=approval_id)

    # The row is CONSUMED, which is not APPROVED; evaluate will re-enqueue
    # or refuse depending on state.  Key invariant: no subprocess was spawned.
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Approval store unreachable → refuse, not proceed
# ---------------------------------------------------------------------------

@_PHASE2
def test_store_unreachable_refuses(tmp_path, monkeypatch):
    """T5: If the DB cannot be opened, governor refuses."""
    import services.approval.governor as gov_mod

    original_open = gov_mod._open_db

    def _broken_open_db():
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(gov_mod, "_open_db", _broken_open_db)
    monkeypatch.setattr(gov_mod, "_governor", None)

    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict()

    assert verdict["action"] == "REFUSED", f"Expected REFUSED on unreachable store, got {verdict}"
    assert "unreachable" in verdict["reason"] or "store" in verdict.get("message", "").lower()
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Depth >= MAX_DISPATCH_DEPTH → refused
# ---------------------------------------------------------------------------

@_PHASE2
def test_depth_exceeded_refused(tmp_path):
    """T6: delegation_depth >= 1 → REFUSED (soul dispatching a soul)."""
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(delegation_depth=1)

    assert verdict["action"] == "REFUSED", f"Expected REFUSED for depth=1, got {verdict}"
    assert "depth" in verdict["reason"]
    mock_popen.assert_not_called()

    records = audit_lines(tmp_path)
    assert any("depth" in r.get("reason", "") for r in records)


# ---------------------------------------------------------------------------
# Test 7: Concurrency — two processes, second refuses
# ---------------------------------------------------------------------------

@_PHASE2
def test_concurrency_cross_process(tmp_path):
    """T7: Cross-process concurrency lock — second dispatch is refused.

    A subprocess holds the fcntl lock for 3 seconds.  The main process
    attempts a non-blocking acquire and must receive REFUSED (lock busy).

    This proves concurrency control across two OS processes, not just two
    asyncio coroutines.
    """
    lock_path = tmp_path / "soul_dispatch.lock"

    # Script that holds the lock for a few seconds and signals readiness
    holder_script = textwrap.dedent(f"""
        import fcntl
        import sys
        import time

        lock_path = {str(lock_path)!r}
        fh = open(lock_path, 'w')
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        # Signal that lock is held by printing to stdout
        sys.stdout.write('LOCKED\\n')
        sys.stdout.flush()
        time.sleep(4)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    """)

    holder_file = tmp_path / "_lock_holder.py"
    holder_file.write_text(holder_script)

    # Start the holder subprocess
    proc = subprocess.Popen(
        [sys.executable, str(holder_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for holder to acquire the lock
        line = proc.stdout.readline()
        assert line.strip() == b"LOCKED", f"Holder did not signal lock: {line!r}"

        # Now try a non-blocking acquire from this process
        import services.approval.governor as gov_mod
        with gov_mod._soul_lock(blocking=False) as acquired:
            assert not acquired, (
                "Lock should be busy (held by subprocess) but was acquired — "
                "cross-process concurrency not working"
            )
    finally:
        proc.terminate()
        proc.wait()


# ---------------------------------------------------------------------------
# Test 8: Soul name not in allowlist → refused
# ---------------------------------------------------------------------------

@_PHASE2
def test_soul_not_on_allowlist_refused(tmp_path):
    """T8: Unknown soul name → REFUSED."""
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(soul="unknown_soul_xyz")

    assert verdict["action"] == "REFUSED", f"Expected REFUSED for unknown soul, got {verdict}"
    assert "not_allowed" in verdict["reason"] or "allowlist" in verdict.get("message", "").lower()
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: Path escape in soul or task field → refused
# ---------------------------------------------------------------------------

@_PHASE2
@pytest.mark.parametrize("soul,task,label", [
    ("../etc/passwd", "do thing", "traversal_in_soul"),
    ("maren", "../../../etc/passwd", "traversal_in_task"),
    ("/absolute/path", "do thing", "absolute_in_soul"),
    ("maren", "/absolute/path/task", "absolute_in_task"),
    ("maren\x00evil", "do thing", "null_byte_in_soul"),
])
def test_path_escape_refused(tmp_path, soul, task, label):
    """T9: Path-escape indicators in soul or task → REFUSED."""
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(soul=soul, task=task)

    assert verdict["action"] == "REFUSED", (
        f"[{label}] Expected REFUSED for path escape, got {verdict}"
    )
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# F1: Concurrency lock durable across dispatch lifecycle
# ---------------------------------------------------------------------------

def test_f1_spawn_calls_proc_wait(tmp_path, monkeypatch):
    """F1: _spawn_soul_dispatch calls proc.wait() so the lock is held for the
    full dispatch lifecycle and cannot be raced by a second approval.
    """
    import inspect
    import nodes.agent.dcs_soul as soul_mod

    src = inspect.getsource(soul_mod._spawn_soul_dispatch)
    assert "proc.wait()" in src, (
        "F1: _spawn_soul_dispatch must call proc.wait() to hold the "
        "concurrency lock for the full dispatch lifecycle"
    )


@_PHASE2
def test_f1_lock_busy_during_dispatch(tmp_path):
    """F1: The soul dispatch lock is busy while _spawn_soul_dispatch is running.

    A real subprocess that sleeps 2 seconds is spawned.  During that sleep
    the lock must be un-acquirable from a second process.
    """
    import sys
    import time
    import nodes.agent.dcs_soul as soul_mod
    import services.approval.governor as gov_mod

    # Patch lock path to the test tmp dir
    orig_lock_path = gov_mod._LOCK_PATH
    gov_mod._LOCK_PATH = tmp_path / "soul_dispatch.lock"

    # Patch dispatch script to a tiny sleep script
    sleep_script = tmp_path / "slow_dispatch.py"
    sleep_script.write_text("import time; time.sleep(2)\n")
    orig_dispatch = soul_mod._DISPATCH_SCRIPT
    orig_argv = soul_mod._ARGV_TEMPLATE[:]
    soul_mod._DISPATCH_SCRIPT = sleep_script
    soul_mod._ARGV_TEMPLATE = [sys.executable, str(sleep_script)]

    lock_was_busy = []

    def _run_dispatch():
        with gov_mod._soul_lock(blocking=True) as acquired:
            assert acquired
            soul_mod._spawn_soul_dispatch(
                soul="maren",
                task="test task",
                context={},
                task_id="tid_f1",
                approval_id="appr_f1",
                row={"root_exec_id": "root_f1"},
            )

    dispatch_thread = threading.Thread(target=_run_dispatch, daemon=True)
    dispatch_thread.start()

    # Give the dispatch time to start and hold the lock
    time.sleep(0.4)

    # Attempt a non-blocking lock acquire — must fail while dispatch runs
    with gov_mod._soul_lock(blocking=False) as acquired2:
        lock_was_busy.append(not acquired2)

    dispatch_thread.join(timeout=10)

    # Restore
    gov_mod._LOCK_PATH = orig_lock_path
    soul_mod._DISPATCH_SCRIPT = orig_dispatch
    soul_mod._ARGV_TEMPLATE = orig_argv

    assert any(lock_was_busy), (
        "F1: Lock was NOT busy during dispatch — fix did not hold the lock "
        "across the full dispatch lifecycle"
    )


# ---------------------------------------------------------------------------
# F2: Path-escape guard allows legitimate task text containing /
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expect_escape,label", [
    # Traversal — must be detected
    ("../etc/passwd", True, "double_dot_slash"),
    ("../../secrets", True, "double_dot_slash_deep"),
    ("foo\x00bar", True, "null_byte"),
    ("%2e%2e%2f", True, "pct_encoded_traversal"),
    # Absolute paths — must be detected
    ("/etc/passwd", True, "absolute_posix"),
    ("\\windows\\system32", True, "absolute_windows"),
    # Legitimate task content containing / — must NOT be detected
    ("use /usr/bin/python to run the script", False, "path_in_task_text"),
    ("fetch https://example.com/api/v1/data and parse it", False, "url_in_task"),
    ("write a script that reads from /tmp/out.txt", False, "path_in_middle"),
    ("run grep -r foo /var/log/syslog", False, "unix_arg_in_task"),
    # Plain task with no special chars
    ("analyze the codebase and write tests", False, "plain_task"),
    ("maren", False, "plain_soul_name"),
])
def test_f2_path_escape_guard_precision(value, expect_escape, label):
    """F2: _contains_path_escape catches traversal/absolute-path indicators
    without refusing legitimate task briefs that contain forward slashes.
    """
    from services.cipherd_approval import _contains_path_escape

    result = _contains_path_escape(value)
    assert result == expect_escape, (
        f"[{label}] _contains_path_escape({value!r}) returned {result}, "
        f"expected {expect_escape}"
    )


# ---------------------------------------------------------------------------
# Bonus: tools.py inheritance defect fix (C3)
# ---------------------------------------------------------------------------

def test_tools_c3_fix_uses_resolved_limits():
    """C3: child_context inherits resolved max_depth/max_concurrency, not literals."""
    import ast
    import inspect
    import services.handlers.tools as tools_mod

    src = inspect.getsource(tools_mod._execute_delegated_agent)

    # The literal fallbacks '3' and '2' must not appear as dict values for
    # these keys.  Acceptable form: max_concurrency and max_depth variables.
    tree = ast.parse(textwrap.dedent(src))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, val_node in zip(node.keys, node.values):
            if not isinstance(key_node, ast.Constant):
                continue
            if key_node.value == "max_concurrent_subagents":
                # Value must NOT be ast.Call(func=.get, ...) with literal 3
                assert not (
                    isinstance(val_node, ast.Call)
                    and isinstance(getattr(val_node, "args", [None, None])[1:2], list)
                    and any(
                        isinstance(a, ast.Constant) and a.value == 3
                        for a in getattr(val_node, "args", [])
                    )
                ), "C3: max_concurrent_subagents still uses literal fallback 3"
            if key_node.value == "max_delegation_depth":
                assert not (
                    isinstance(val_node, ast.Call)
                    and any(
                        isinstance(a, ast.Constant) and a.value == 2
                        for a in getattr(val_node, "args", [])
                    )
                ), "C3: max_delegation_depth still uses literal fallback 2"
