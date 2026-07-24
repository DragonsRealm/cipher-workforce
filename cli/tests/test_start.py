"""Smoke tests for ``cli.commands.start``."""

from __future__ import annotations

from pathlib import Path

from cli.commands import start
from cli.config import Config, load_config


def _cfg() -> Config:
    # Use the real env-file loader so test config mirrors production
    # behaviour (``.env.template`` -> ``.env`` -> ``os.environ``).
    # No hardcoded values: ``.env.template`` is the single source of
    # truth, same as ``cli.commands.start`` at runtime.
    return load_config()


# ``start`` supervises exactly one process: uvicorn, spawned via the
# server venv's interpreter (see ``cli.platform_.server_venv_python``).
# The backend serves the built SPA itself (SERVE_STATIC_CLIENT) and
# owns the Temporal dev server + other optional daemons from its
# lifespan, so no client/temporal specs exist on the CLI side.


def test_build_specs_is_backend_only(tmp_path: Path):
    cfg = _cfg()
    specs = start._build_specs(tmp_path, cfg)
    assert {s.name for s in specs} == {"server"}


def test_build_specs_backend_uses_venv_interpreter(tmp_path: Path):
    from cli.platform_ import server_venv_python

    cfg = _cfg()
    specs = start._build_specs(tmp_path, cfg)
    server = next(s for s in specs if s.name == "server")
    assert server.argv[0] == str(server_venv_python(tmp_path))
    assert server.argv[1:4] == ["-m", "uvicorn", "main:app"]


def test_build_specs_assigns_ready_ports(tmp_path: Path):
    cfg = _cfg()
    specs = start._build_specs(tmp_path, cfg)
    by_name = {s.name: s for s in specs}
    assert by_name["server"].ready_port == cfg.backend_port
