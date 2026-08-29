"""Shared env-key allowlist for spawned subprocess environments.

NEVER pass ``os.environ`` directly to a child process. Use
``build_safe_env()`` instead: it selects only the keys on this
allowlist, plus any manifest-declared extras, keeping ``CIPHER_*``,
``ANTHROPIC_*``, ``STRIPE_*``, and ``GOOGLE_*`` out of child envs.

Single source of truth — consumed by both
  * server/nodes/filesystem/_backend.py (WorkspaceBackend)
  * server/services/process_service.py (ProcessService.start)

Adding a key here is a deliberate, reviewable choice; leaking secrets
by widening the allowlist is harder to miss in diff.
"""
from __future__ import annotations

import os
from typing import FrozenSet, Mapping

# Keys that subprocess children legitimately need from the host env.
# Extend this set only with keys that are safe to expose to user-authored
# workflow code and have no secret-bearing meaning.
SAFE_ENV_KEYS: FrozenSet[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
    }
)

# Prefixes that must NEVER appear in a child env, regardless of allowlist.
# Tested by test_env_scrubbing.py.
_BLOCKED_PREFIXES: tuple[str, ...] = (
    "CIPHER_",
    "ANTHROPIC_",
    "STRIPE_",
    "GOOGLE_",
)


def build_safe_env(extra_keys: FrozenSet[str] | None = None) -> dict[str, str]:
    """Return a dict of safe env vars for spawning child processes.

    Args:
        extra_keys: Additional key names declared by the node's manifest
                    (e.g. PORT, LANG override). These are additive to
                    SAFE_ENV_KEYS but are still checked against blocked
                    prefixes at test time.

    Returns:
        Dict containing only the allowed subset of os.environ.
    """
    allowed = SAFE_ENV_KEYS | (extra_keys or frozenset())
    result: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in allowed:
            result[k] = v
    return result
