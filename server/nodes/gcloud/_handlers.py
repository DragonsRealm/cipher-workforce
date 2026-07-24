"""Google Cloud WebSocket handlers — the gcloud CLI owns its own auth
(gh / Stripe / cloudflare pattern), including the browser interaction.

``gcloud_login`` spawns gcloud's official login and lets the CLI drive
the whole flow itself::

    gcloud auth login --quiet

gcloud runs Google's OAuth flow with a loopback callback server on a
RANDOM port and OPENS THE DEFAULT BROWSER ITSELF. The handler
deliberately does NOT parse or proxy the authorize URL to the frontend
— no custom login UI; the modal just gets ``{success, message}`` and
the connected badge flips when the background completion broadcasts.

Hazards guarded against (both inherited from the cloudflare plugin):

* **Duplicate flows** — a module-level single-flight guard makes repeat
  Login clicks return "already in progress" instead of spawning a
  second browser tab + competing loopback server.
* **Windows shim orphaning** — ``gcloud.cmd`` is a cmd.exe wrapper
  around the bundled Python; killing it terminates only the wrapper and
  orphans the python child holding the loopback callback socket. The
  completion watcher therefore NEVER kills the process — gcloud exits
  by itself when the flow completes or the user abandons it.

Success gate: ``gcloud auth list --filter=status:ACTIVE`` returning a
non-empty account list under the pinned config dir (gcloud exits 0 in
both auth states, so exit codes are never trusted). On success we write
the synthetic ``cli-managed`` marker OAuth row (flips the catalogue's
``stored`` badge, with the account email as the label) and broadcast
the generic catalogue-invalidation event. Marker + broadcast plumbing
is the shared :mod:`services.cli_agent._cli_auth` module
(claude/codex/github/cloudflare all use it).

OpenCompany never stores or reads the actual credentials — they stay in
gcloud's own store under ``<DATA_DIR>/gcloud/``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from core.logging import get_logger
from services.cli_agent._cli_auth import broadcast_credential_event, mark_logged_in, mark_logged_out
from services.events import run_cli_command

from ._install import ensure_gcloud_cli
from ._service import active_account, login_env

logger = get_logger(__name__)

# --quiet suppresses the "You are about to log in..." Y/n confirmation;
# gcloud still opens the browser and serves the loopback callback.
_LOGIN_ARGS = ["auth", "login", "--quiet"]
_LOGIN_TIMEOUT_SECONDS = 600
# The frontend drops WS requests after 30s. A first-ever login pays the
# cold SDK install (~100 MB download + large extraction) inside this
# handler — answer within this budget no matter what and let the flow
# continue in the background.
_RESPONSE_BUDGET_SECONDS = 22
# Retained head of the CLI's output, used only for the failure log line.
_OUTPUT_CAP_BYTES = 8192


# Strong refs for fire-and-forget tasks — asyncio holds only weak refs
# (the documented discard-set pattern from the asyncio docs).
_background_tasks: set = set()

# Single-flight state: at most one login flow at a time. `task` covers
# the pre-spawn window (install + session probe), `proc` covers the
# browser-flow window until gcloud exits on its own.
_active_login: Dict[str, Any] = {"task": None, "proc": None}


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _login_in_progress() -> bool:
    task = _active_login["task"]
    if task is not None and not task.done():
        return True
    proc = _active_login["proc"]
    return proc is not None and proc.returncode is None


async def _mark_connected(email: Optional[str]) -> None:
    await mark_logged_in("gcloud", email=email)
    logger.info("[GCloud] connected as %s — catalogue marker persisted", email or "<unknown>")
    await broadcast_credential_event("credential.oauth.connected", provider="gcloud")


async def _start_login_flow() -> Dict[str, Any]:
    """Install (if needed) + spawn gcloud's own browser login. Never
    raises — returns the WS response dict. The CLI owns the interaction
    from here: it opens the browser, serves the loopback callback, and
    exits when done; we only watch for the exit in the background."""
    try:
        try:
            binary = str(await ensure_gcloud_cli())
        except Exception as e:
            logger.warning("[GCloud] Google Cloud CLI install failed: %s", e)
            return {
                "success": False,
                "error": f"Google Cloud CLI install failed ({e}). Manual install: https://cloud.google.com/sdk/docs/install",
            }

        # Fast path: a live session already exists under the pinned
        # config dir (a previous flow completed after the modal closed).
        info = await active_account()
        if info:
            email = info.get("account")
            await _mark_connected(email)
            return {"success": True, "message": f"Already logged in{f' as {email}' if email else ''}."}

        # stdin=PIPE left un-written: prompts are disabled via --quiet +
        # CLOUDSDK_CORE_DISABLE_PROMPTS, and a pipe keeps any stray
        # stdin read from seeing instant EOF.
        proc = await asyncio.create_subprocess_exec(
            binary,
            *_LOGIN_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=login_env(),
        )
        _active_login["proc"] = proc

        # Drain both pipes for the process lifetime so gcloud never
        # blocks on a full pipe buffer while its callback server waits.
        # Only a small head is retained — solely for the failure log.
        output: List[str] = []

        async def drain(stream: Optional[asyncio.StreamReader]) -> None:
            if stream is None:
                return
            kept = 0
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if kept < _OUTPUT_CAP_BYTES:
                    text = chunk.decode(errors="replace")
                    output.append(text)
                    kept += len(text)

        _spawn_background(drain(proc.stdout))
        _spawn_background(drain(proc.stderr))
        _spawn_background(_complete_login(proc, output))

        logger.info(
            "[GCloud] gcloud auth login spawned (pid=%s) — gcloud opens the browser itself; awaiting completion in background (timeout=%ss)",
            proc.pid,
            _LOGIN_TIMEOUT_SECONDS,
        )
        return {
            "success": True,
            "message": "gcloud is opening your default browser — complete the Google sign-in there.",
        }
    except Exception as e:
        logger.exception("[GCloud] login flow raised unexpectedly: %s", e)
        return {"success": False, "error": f"Google Cloud login failed: {e}"}


async def handle_gcloud_login(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Single-flight + answer within the frontend's request window no
    matter what (vercel's cold-install pending pattern)."""
    if _login_in_progress():
        logger.info("[GCloud] login request ignored — a flow is already in progress")
        return {
            "success": True,
            "pending": True,
            "message": (
                "A Google Cloud login is already in progress — complete it in the browser "
                "window gcloud opened (it may still be preparing)."
            ),
        }

    logger.info("[GCloud] login flow starting (gcloud auth login)")
    flow = _spawn_background(_start_login_flow())
    _active_login["task"] = flow
    try:
        return await asyncio.wait_for(asyncio.shield(flow), timeout=_RESPONSE_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.info(
            "[GCloud] login still preparing after %ss (cold SDK install) — continuing in background",
            _RESPONSE_BUDGET_SECONDS,
        )
        return {
            "success": True,
            "pending": True,
            "message": (
                "The Google Cloud CLI is being installed (~100 MB on first use) — "
                "the browser will open automatically when it is ready."
            ),
        }


async def _complete_login(proc: asyncio.subprocess.Process, output: List[str]) -> None:
    """Await gcloud's loopback-callback flow; gate success on the
    ``auth list`` probe (exit codes are not trusted — gcloud exits 0
    either way); then the marker + broadcast.

    Never kills the process: on Windows the entry point is a
    ``gcloud.cmd`` shim, and killing the wrapper orphans the python
    child holding the loopback callback socket. gcloud exits by itself
    when the flow completes or is abandoned.
    """
    try:
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=_LOGIN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "[GCloud] login still running after %ss — leaving it to finish on its own (killing the shim would orphan the callback server)",
                _LOGIN_TIMEOUT_SECONDS,
            )
            return

        info = await active_account()
        if not info:
            tail = "".join(output).strip().splitlines()
            banner = " | ".join(ln.strip() for ln in tail[-5:] if ln.strip()) or "(no output)"
            logger.warning(
                "[GCloud] login exited (code=%s) but 'gcloud auth list' reports no active account. CLI said: %s",
                returncode,
                banner,
            )
            return

        await _mark_connected(info.get("account"))
    except Exception as e:
        logger.exception("[GCloud] login completion raised unexpectedly: %s", e)
    finally:
        if _active_login["proc"] is proc:
            _active_login["proc"] = None


async def handle_gcloud_logout(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """``gcloud auth revoke --all`` (best-effort — hits Google's
    revocation endpoint, and exits non-zero when nothing is stored),
    drop the catalogue marker, broadcast so the modal flips
    immediately."""
    logger.info("[GCloud] logout starting")
    from ._service import resolve_gcloud_light

    binary = resolve_gcloud_light()
    result: Dict[str, Any] = {"success": True}
    if binary:
        result = await run_cli_command(
            binary=binary,
            argv=["auth", "revoke", "--all", "--quiet"],
            timeout=30.0,
            env=login_env(),
        )
        if not result.get("success"):
            logger.warning("[GCloud] 'gcloud auth revoke' failed (marker still removed): %s", result.get("error"))
            result = {"success": True, "message": "gcloud reported no stored session; catalogue marker removed"}
    await mark_logged_out("gcloud")
    await broadcast_credential_event("credential.oauth.disconnected", provider="gcloud")
    logger.info("[GCloud] logout complete: marker removed + catalogue broadcast sent")
    return result


async def handle_gcloud_status(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Session snapshot straight from the CLI — no side effects."""
    info = await active_account()
    connected = info is not None
    status: Dict[str, Any] = {"connected": connected, "logged_in": connected}
    if info and info.get("account"):
        status["email"] = info["account"]
    return {"success": True, "status": status}


WS_HANDLERS = {
    "gcloud_login": handle_gcloud_login,
    "gcloud_logout": handle_gcloud_logout,
    "gcloud_status": handle_gcloud_status,
}
