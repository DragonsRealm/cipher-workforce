"""``company serve`` -- single-port production runtime.

Runs the app on ONE public port: uvicorn serves the REST API + WebSocket +
the built React SPA (via the ``SERVE_STATIC_CLIENT`` block in
``server/main.py``). Used locally for a production-shaped run AND as the
systemd ``ExecStart`` on a VM provisioned by ``company deploy``.

Optional daemons are backend-owned and spawn on demand — the Node.js
code-exec sidecar (``nodes/code/_runtime.py``), WhatsApp, and the
Temporal dev server all start from the backend, so ``serve`` supervises
exactly one process.

The long-running uvicorn is invoked via the server venv's interpreter
directly (not ``uv run``) so the systemd service has no runtime dependency
on ``uv`` being on PATH.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from cli._common import preflight
from cli.buildenv import validate_build
from cli.colors import console
from cli.platform_ import server_dir, server_venv_python


def serve_command(port: int | None = None) -> None:
    from cli.supervisor import Manager, ServiceSpec

    cfg, root = preflight()
    os.environ.setdefault("PYTHONUTF8", "1")
    validate_build(root, require_client_dist=True)

    # Public port: --port flag > $PORT (Cloud Run / systemd convention) >
    # PYTHON_BACKEND_PORT from the env files.
    bind_port = port or int(os.environ.get("PORT") or cfg.backend_port)

    # Free the port we will bind (clears stale orphans; idempotent).
    from cli.ports import kill_port

    kill_port(bind_port)

    console.print()
    console.print("  [bold]OpenCompany[/] serve (single-port)")
    console.print(f"  App:     http://0.0.0.0:{bind_port}  (API + WebSocket + SPA)")
    console.print()

    specs = [
        ServiceSpec(
            name="server",
            argv=[
                str(server_venv_python(root)),
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(bind_port),
                "--log-level",
                "warning",
            ],
            cwd=server_dir(root),
            env={"SERVE_STATIC_CLIENT": "1", "PORT": str(bind_port)},
            ready_port=bind_port,
        ),
    ]

    manager = Manager()
    manager.add_all(specs)
    rc = asyncio.run(manager.run())
    if rc != 0:
        raise typer.Exit(code=rc)
