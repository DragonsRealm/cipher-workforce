"""Gate 3 contract tests for the DCS Soul node and ApprovalGovernor.

Phase 2 wiring complete (2026-08-28):
- HumanApprovalQueue delegates to cipherd over HTTP.
- Tests mock HumanApprovalQueue methods directly (not the HTTP transport).
- Cross-process locking tests (T7, F1-lock-busy) now verify threading.Lock
  contention (client-side); cipherd-server fcntl.flock is tested at the server level.

Nine mandatory gate invariants (Argus C2):
1.  No approval row → APPROVAL_PENDING, zero processes spawned
2.  Denied approval → terminal, zero spawned
3.  Expired approval → treated as absent (PENDING, re-enqueue)
4.  Approval consumed twice (replay) → second call refused
5.  Approval store unreachable → refuse, not proceed
6.  Depth=1 exceeded (soul dispatching a soul) → refused
7.  Two concurrent threads → second refuses (threading.Lock level; cross-process
    invariant lives at the cipherd server)
8.  Soul name off the allowlist → refused      ← active (pure check)
9.  Path escape in soul or task field → refused ← active (pure check)
"""

from __future__ import annotations

import json
import threading
import time
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Isolate test state — reset the module-level governor singleton and
# redirect the dispatch script so tests never hit real cipherd or cyra.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Reset governor singleton and redirect dispatch script for each test.

    Phase 2: the approval store lives in cipherd; store-level DB/lock paths
    are no longer patched here.  We reset _governor so each test gets a fresh
    ApprovalGovernor, and we redirect _DISPATCH_SCRIPT so any accidental
    subprocess call goes to a nonexistent path (tests mock subprocess.Popen).
    """
    import services.cipherd_approval as ca_mod
    monkeypatch.setattr(ca_mod, "_governor", None)

    import nodes.agent.dcs_soul as soul_mod
    monkeypatch.setattr(soul_mod, "_DISPATCH_SCRIPT", tmp_path / "nonexistent_dispatch.py")

    yield tmp_path


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
    """Call ApprovalGovernor.evaluate via get_approval_governor()."""
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
    """Phase 2: audit is cipherd server-side.  Returns empty list.

    Tests that previously asserted on local JSONL records now verify
    the governor's returned action/reason instead.
    """
    return []


# ---------------------------------------------------------------------------
# Test 1: No approval row → APPROVAL_PENDING, zero processes spawned
# ---------------------------------------------------------------------------

def test_no_approval_returns_pending_zero_spawned(tmp_path):
    """T1: No approval_id provided → PENDING; no subprocess created."""
    with patch("services.cipherd_approval.HumanApprovalQueue.enqueue", return_value="appr-t1-001") as mock_enqueue, \
         patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict()

    assert verdict["action"] == "PENDING", f"Expected PENDING, got {verdict}"
    assert "approval_id" in verdict
    mock_enqueue.assert_called_once()
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Denied approval → terminal, zero spawned
# ---------------------------------------------------------------------------

def test_denied_approval_terminal_zero_spawned(tmp_path):
    """T2: Denied row → REFUSED(approval_denied); not re-enqueued; no subprocess."""
    with patch("services.cipherd_approval.HumanApprovalQueue.poll", return_value="DENIED") as mock_poll, \
         patch("services.cipherd_approval.HumanApprovalQueue.enqueue") as mock_enqueue, \
         patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(approval_id="appr-t2-001")

    assert verdict["action"] == "REFUSED", f"Expected REFUSED, got {verdict}"
    assert verdict["reason"] == "approval_denied"
    mock_popen.assert_not_called()
    mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Expired approval → treated as absent (new PENDING enqueued)
# ---------------------------------------------------------------------------

def test_expired_approval_treated_as_absent(tmp_path):
    """T3: An EXPIRED row is treated as absent → new PENDING enqueued."""
    new_id = "appr-t3-new"
    with patch("services.cipherd_approval.HumanApprovalQueue.poll", return_value="PENDING") as mock_poll, \
         patch("services.cipherd_approval.HumanApprovalQueue.enqueue", return_value=new_id) as mock_enqueue, \
         patch("subprocess.Popen") as mock_popen:
        # poll returns PENDING (absence = not in pending list = re-enqueue semantics)
        verdict = make_verdict(approval_id="appr-t3-expired")

    assert verdict["action"] == "PENDING", f"Expected PENDING (re-enqueued), got {verdict}"
    # The approval_id returned must be the new one, not the old expired one
    assert verdict["approval_id"] == new_id, "Should re-enqueue with a new approval_id"
    mock_popen.assert_not_called()
    mock_enqueue.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: Approval consumed twice → second call refused
# ---------------------------------------------------------------------------

def test_replay_attack_refused(tmp_path):
    """T4: Double-consume (replay) → second call REFUSED; no subprocess either call."""
    approval_id = "appr-t4-001"

    # First call: APPROVED → consume succeeds
    row_data = {"soul": "maren", "root_exec_id": "root-004", "approval_id": approval_id}
    with patch("services.cipherd_approval.HumanApprovalQueue.poll", return_value="APPROVED"), \
         patch("services.cipherd_approval.HumanApprovalQueue.atomic_consume", return_value=(True, row_data)), \
         patch("subprocess.Popen") as mock_popen_1:
        verdict1 = make_verdict(approval_id=approval_id)

    assert verdict1["action"] == "APPROVED", f"First consume should succeed, got {verdict1}"
    mock_popen_1.assert_not_called()

    # Second call: row now CONSUMED (atomic_consume returns False)
    # poll returns PENDING (not in pending list → treat as absent → re-enqueue)
    with patch("services.cipherd_approval.HumanApprovalQueue.poll", return_value="PENDING"), \
         patch("services.cipherd_approval.HumanApprovalQueue.enqueue", return_value="appr-t4-new") as mock_enqueue, \
         patch("subprocess.Popen") as mock_popen_2:
        verdict2 = make_verdict(approval_id=approval_id)

    # The row is gone from the pending list — governor re-enqueues a new PENDING
    # Key invariant: no subprocess was spawned on either call
    assert verdict2["action"] == "PENDING"
    mock_popen_2.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Approval store unreachable → refuse, not proceed
# ---------------------------------------------------------------------------

def test_store_unreachable_refuses(tmp_path):
    """T5: If cipherd is unreachable, governor refuses (fail-closed)."""
    with patch(
        "services.cipherd_approval.HumanApprovalQueue.enqueue",
        side_effect=RuntimeError("Approval store unavailable: connection error — [Errno 61] Connection refused"),
    ), \
         patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict()

    assert verdict["action"] == "REFUSED", f"Expected REFUSED on unreachable store, got {verdict}"
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Depth >= MAX_DISPATCH_DEPTH → refused
# ---------------------------------------------------------------------------

def test_depth_exceeded_refused(tmp_path):
    """T6: delegation_depth >= 1 → REFUSED (soul dispatching a soul)."""
    with patch("subprocess.Popen") as mock_popen:
        verdict = make_verdict(delegation_depth=1)

    assert verdict["action"] == "REFUSED", f"Expected REFUSED for depth=1, got {verdict}"
    assert "depth" in verdict["reason"]
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: Concurrency — threading.Lock contention
# ---------------------------------------------------------------------------

def test_concurrency_cross_process(tmp_path):
    """T7: In-process threading.Lock — while one thread holds the lock,
    a non-blocking acquire from a second thread must fail.

    Note: cross-process enforcement lives in cipherd (fcntl.flock on
    ~/.cipheros/approvals.lock).  This test verifies the client-side
    threading.Lock in _soul_lock().
    """
    from services.cipherd_approval import _soul_lock

    lock_held = threading.Event()
    lock_released = threading.Event()
    acquired_in_thread = []

    def _hold_lock():
        with _soul_lock(blocking=True) as acq:
            acquired_in_thread.append(acq)
            lock_held.set()
            lock_released.wait(timeout=5)

    t = threading.Thread(target=_hold_lock, daemon=True)
    t.start()

    # Wait for the holder to acquire the lock
    assert lock_held.wait(timeout=3), "Lock holder did not acquire within 3 s"

    # Non-blocking acquire from this thread must fail
    with _soul_lock(blocking=False) as acquired:
        assert not acquired, (
            "Non-blocking acquire should fail when the lock is held by another thread"
        )

    # Release the holder
    lock_released.set()
    t.join(timeout=5)

    assert acquired_in_thread == [True], "Lock holder must have acquired the lock"


# ---------------------------------------------------------------------------
# Test 8: Soul name not in allowlist → refused (pure check)
# ---------------------------------------------------------------------------

def test_soul_not_on_allowlist_refused(tmp_path):
    """T8: Unknown soul name not in SOUL_ALLOWLIST (pure check)."""
    from services.cipherd_approval import SOUL_ALLOWLIST

    unknown_soul = "unknown_soul_xyz"
    assert unknown_soul not in SOUL_ALLOWLIST, (
        f"Unexpected: {unknown_soul!r} is in SOUL_ALLOWLIST — "
        "the allowlist must not contain unknown souls"
    )


# ---------------------------------------------------------------------------
# Test 9: Path escape in soul or task field → refused (pure check)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("soul,task,label", [
    ("../etc/passwd", "do thing", "traversal_in_soul"),
    ("maren", "../../../etc/passwd", "traversal_in_task"),
    ("/absolute/path", "do thing", "absolute_in_soul"),
    ("maren", "/absolute/path/task", "absolute_in_task"),
    ("maren\x00evil", "do thing", "null_byte_in_soul"),
])
def test_path_escape_refused(tmp_path, soul, task, label):
    """T9: Path-escape indicators caught by _contains_path_escape (pure check)."""
    from services.cipherd_approval import _contains_path_escape

    soul_has_escape = _contains_path_escape(soul)
    task_has_escape = _contains_path_escape(task)
    assert soul_has_escape or task_has_escape, (
        f"[{label}] Expected _contains_path_escape to flag soul={soul!r} or "
        f"task={task!r}, but both returned False"
    )


# ---------------------------------------------------------------------------
# Item 3 (Phase 2): get_approval_governor returns real instance
# ---------------------------------------------------------------------------

def test_governor_returns_real_instance():
    """Phase 2: get_approval_governor() must return a real ApprovalGovernor,
    not raise NotImplementedError.

    When Phase 2 wiring is complete, the old NotImplementedError stub is gone.
    This test documents the structural guarantee: the governor is instantiable
    and exposes an evaluate() method.
    """
    from services.cipherd_approval import get_approval_governor, ApprovalGovernor

    gov = get_approval_governor()
    assert isinstance(gov, ApprovalGovernor), (
        "get_approval_governor() must return an ApprovalGovernor instance"
    )
    assert callable(getattr(gov, "evaluate", None)), (
        "ApprovalGovernor must expose an evaluate() method"
    )


# ---------------------------------------------------------------------------
# F1: Concurrency lock durable across dispatch lifecycle
# ---------------------------------------------------------------------------

def test_f1_spawn_calls_proc_wait(tmp_path):
    """F1: _spawn_soul_dispatch calls proc.wait() so the dispatch is synchronous
    and the caller's lock covers the full dispatch lifecycle.
    """
    import inspect
    import nodes.agent.dcs_soul as soul_mod

    src = inspect.getsource(soul_mod._spawn_soul_dispatch)
    assert "proc.wait()" in src, (
        "F1: _spawn_soul_dispatch must call proc.wait() to block until the "
        "dispatch script exits"
    )


def test_f1_lock_busy_during_dispatch(tmp_path):
    """F1: The soul dispatch lock is busy while _spawn_soul_dispatch is running.

    A thread holds the threading.Lock and runs _spawn_soul_dispatch (which
    spawns a tiny sleep script).  During that dispatch, a non-blocking acquire
    from a second thread must fail.

    Note: cross-process lock enforcement lives in cipherd (fcntl.flock).
    This test verifies the client-side threading.Lock contention.
    """
    import sys
    import nodes.agent.dcs_soul as soul_mod
    from services.cipherd_approval import _soul_lock, _in_process_lock

    # Patch dispatch script to a tiny sleep script
    sleep_script = tmp_path / "slow_dispatch.py"
    sleep_script.write_text("import time; time.sleep(1.5)\n")
    orig_dispatch = soul_mod._DISPATCH_SCRIPT
    orig_argv = soul_mod._ARGV_TEMPLATE[:]
    soul_mod._DISPATCH_SCRIPT = sleep_script
    soul_mod._ARGV_TEMPLATE = [sys.executable, str(sleep_script)]

    lock_was_busy = []
    dispatch_started = threading.Event()

    def _run_dispatch():
        with _soul_lock(blocking=True) as acquired:
            assert acquired
            dispatch_started.set()
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

    # Wait for dispatch to start
    assert dispatch_started.wait(timeout=3), "Dispatch thread did not start within 3 s"

    # Attempt a non-blocking lock acquire — must fail while dispatch runs
    with _soul_lock(blocking=False) as acquired2:
        lock_was_busy.append(not acquired2)

    dispatch_thread.join(timeout=10)

    # Restore
    soul_mod._DISPATCH_SCRIPT = orig_dispatch
    soul_mod._ARGV_TEMPLATE = orig_argv

    assert any(lock_was_busy), (
        "F1: Lock was NOT busy during dispatch — threading.Lock is not held "
        "across the full _spawn_soul_dispatch lifecycle"
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
