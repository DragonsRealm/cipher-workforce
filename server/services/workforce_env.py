"""Isolated credential loader for cipher-workforce capability plane.

Reads from ``~/.cipheros/workforce/.env`` (chmod 600, never committed).
Never reads from ``os.environ`` or process-level environment.

Design constraints (Orion D4, Argus Gate 3):
- ``CIPHER_APPROVAL_TOKEN`` must NOT enter the CW process. This file
  enforces that by loading only ``WORKFORCE_*`` keys and explicitly
  refusing blocked prefixes.
- No fallback to ``os.environ``: if the isolated env file is absent or a
  key is missing, callers receive ``None`` — never a leaked process env var.

Argus-flagged: this file is the sole credential-read surface for Phase 3
capabilities. Any change to the loader, key names, or file path requires
Argus re-proof before merge.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Keys that workforce_env is NEVER permitted to surface, even if they somehow
# appear in the isolated file (belt-and-suspenders against a misconfigured file
# that re-exports the process env via 'export' statements from another source).
_BLOCKED_PREFIXES = (
    "CIPHER_",
    "ANTHROPIC_",
    "STRIPE_",
    "GOOGLE_",  # Google keys for UpStage/production — Dragon-gated; see D4
    "OPENAI_",
)

_WORKFORCE_ENV_PATH = Path.home() / ".cipheros" / "workforce" / ".env"


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict, stripping 'export ' prefixes.

    Lines starting with '#' or blank are ignored.
    Values are NOT interpolated or shell-expanded.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip optional 'export ' prefix (Argus Addendum-5 fix pattern)
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Remove surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _load_env() -> dict[str, str]:
    """Load and cache the workforce env file.

    Cache is invalidated by calling ``reload_workforce_env()``.
    """
    raw = _parse_dotenv(_WORKFORCE_ENV_PATH)
    # Enforce blocked prefixes — fail closed, not silently
    safe: dict[str, str] = {}
    for k, v in raw.items():
        if any(k.startswith(p) for p in _BLOCKED_PREFIXES):
            continue  # silently drop; never surface
        safe[k] = v
    return safe


def reload_workforce_env() -> None:
    """Invalidate the env cache (e.g. after the file is updated)."""
    _load_env.cache_clear()


def get_workforce_credential(key: str) -> Optional[str]:
    """Return a credential from the isolated workforce env file.

    Args:
        key: The env key to look up (e.g. ``WORKFORCE_EMAIL_ADDRESS``).

    Returns:
        The value if present and non-empty, else ``None``.

    Never raises. Never falls back to ``os.environ``.
    """
    env = _load_env()
    value = env.get(key, "")
    return value if value else None


def assert_workforce_env_file_exists() -> None:
    """Raise ``FileNotFoundError`` if the isolated env file is missing.

    Called at service startup to surface a clear error rather than silently
    returning ``None`` for every credential lookup.
    """
    if not _WORKFORCE_ENV_PATH.exists():
        raise FileNotFoundError(
            f"Workforce credential file not found: {_WORKFORCE_ENV_PATH}\n"
            "Create it with chmod 600 and populate the required WORKFORCE_* keys."
        )
