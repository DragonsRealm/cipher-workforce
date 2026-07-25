"""Node.js executor runtime — supervises the Express/tsx sidecar.

The JS/TS code-executor nodes dispatch to a persistent Node.js HTTP
server (source tree at ``server/nodejs/``, esbuild-bundled to
``dist/index.js`` by ``company build``). Historically only ``company
serve`` spawned it as a CLI ServiceSpec, so JS/TS nodes were dead in
``dev`` / ``start`` modes. It is now backend-owned like every other
optional daemon: spawned on demand from the plugin's own
:func:`ensure_started` (first JS/TS node execution), torn down by the
lifespan's ``shutdown_all_supervisors()``.

Same :class:`BaseProcessSupervisor` idiom as ``nodes/whatsapp/_runtime``
and ``services/temporal/_runtime`` — including the TCP probe-before-spawn
from the Temporal runtime, so an externally managed executor (or one
started by another backend process, e.g. a standalone Temporal worker)
is detected and left alone.

Config is plugin-owned: the ``NODEJS_EXECUTOR_*`` env vars are read
here directly (pushed into ``os.environ`` from ``.env`` by the CLI) —
core ``Settings`` carries no executor fields.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from services._supervisor import BaseProcessSupervisor

# Sub-second per-attempt probe — a stalled sidecar fails health fast.
_PROBE_TIMEOUT_SECONDS = 1.0


async def _probe_tcp_port(port: int, host: str = "localhost") -> bool:
    """True iff a TCP connection to ``host:port`` succeeds within
    :data:`_PROBE_TIMEOUT_SECONDS`. Mirrors the Temporal runtime's
    loopback readiness check. ``localhost`` (not ``127.0.0.1``) so the
    probe resolves the same way the HTTP client and the sidecar's own
    bind do — Node binds the IPv6 loopback ``[::1]`` on Windows."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, OSError):
            pass
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False


def executor_port() -> int:
    from core.env_defaults import env_int

    return env_int("NODEJS_EXECUTOR_PORT")


def _sidecar_dir() -> Path:
    # <server>/nodes/code/_runtime.py -> <server>/nodejs
    return Path(__file__).resolve().parents[2] / "nodejs"


class NodeJSExecutorRuntime(BaseProcessSupervisor):
    name = "nodejs-executor"

    pipe_streams = True
    terminate_grace_seconds = 5.0
    graceful_shutdown = sys.platform == "win32"

    # ---- BaseProcessSupervisor overrides ---------------------------------

    async def _pre_spawn(self) -> None:
        if shutil.which("node") is None:
            raise RuntimeError(
                "Node.js not found on PATH — the JS/TS executor sidecar "
                "requires the same Node 22+ install as the rest of OpenCompany."
            )
        if not (_sidecar_dir() / "dist" / "index.js").is_file():
            raise RuntimeError(
                "Node.js executor bundle missing (server/nodejs/dist/index.js). "
                "Run `company build` to produce it."
            )

    def binary_path(self) -> Path:
        return Path(shutil.which("node") or "node")

    def argv(self) -> list[str]:
        return [str(self.binary_path()), str(_sidecar_dir() / "dist" / "index.js")]

    def cwd(self) -> Path:
        return _sidecar_dir()

    def env(self) -> dict[str, str]:
        return {**os.environ, "NODEJS_EXECUTOR_PORT": str(executor_port())}

    async def health_check(self) -> bool:
        if not self.is_running():
            return False
        return await _probe_tcp_port(executor_port())

    async def ensure_started(self) -> None:
        """Start the sidecar unless something already serves the port,
        then wait until it accepts connections.

        A listening port means a previously spawned instance (possibly by
        another backend process) or an externally managed executor — both
        are left alone. Idempotent like ``start()``.
        """
        if await _probe_tcp_port(executor_port()):
            return
        await self.start()
        for _ in range(50):  # ~15 s ceiling; the bundled sidecar boots in <2 s
            if await _probe_tcp_port(executor_port()):
                return
            await asyncio.sleep(0.3)
        raise RuntimeError(
            f"Node.js executor did not become ready on port {executor_port()}"
        )

    def _extra_status(self) -> dict:
        base = super()._extra_status()
        return {**base, "port": executor_port()}


def get_nodejs_executor_runtime() -> NodeJSExecutorRuntime:
    """Return the Node.js executor runtime singleton."""
    return NodeJSExecutorRuntime.get_instance()
