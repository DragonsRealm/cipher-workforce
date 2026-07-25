"""Env accessor backed by the repo's canonical env files.

The default value for every OpenCompany env var lives in ONE place:
``<repo>/.env.template`` (overridden by ``<repo>/.env``, overridden by
the process environment — the same precedence ``cli.config.load_config``
uses). The CLI pushes that merged view into ``os.environ`` for every
process it spawns; entry points that bypass the CLI (direct ``uvicorn``
runs, ``python -m services.temporal.worker``, gunicorn, pytest when a
code path is actually exercised) resolve through this helper instead of
carrying fallback literals in code.

Stdlib-only and dependency-free so it is importable from anywhere
(gunicorn config, plugin folders, the stubbed-core test environment).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser — mirrors ``cli.config._load_env_file``
    semantics (skip blanks/comments, first ``=`` splits, strip quotes)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@lru_cache(maxsize=1)
def _file_defaults() -> dict[str, str]:
    merged = _parse_env_file(_REPO_ROOT / ".env.template")
    merged.update(_parse_env_file(_REPO_ROOT / ".env"))
    return merged


def env_value(key: str) -> str:
    """Resolve ``key`` from the process env, then ``.env`` / ``.env.template``.

    Raises ``RuntimeError`` with a pointer to the canonical file when the
    key is configured nowhere — a loud failure instead of a silent
    hardcoded fallback.
    """
    value = os.environ.get(key) or _file_defaults().get(key)
    if not value:
        raise RuntimeError(
            f"{key} is not configured. Set it in the environment or .env; "
            "canonical defaults live in .env.template."
        )
    return value


def env_int(key: str) -> int:
    return int(env_value(key))
