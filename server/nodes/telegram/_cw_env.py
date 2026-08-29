"""Load cipher-workforce isolated credentials for Telegram.

This module is the ONLY place that reads credential env files for Telegram-
specific keys. It is imported by TelegramService.connect() to apply bot-token
defaults before the encrypted credentials.db layer is consulted.

Credential lookup priority (highest to lowest):
  1. ~/.cipheros/workforce/.env  — cipher-workforce isolated env (phase-1 spec)
  2. ~/.config/cipher-os/secrets.env — canonical CIPHER OS secrets env
     (where the live Telegram-Cyra bridge bot token already lives)

The first file that contains the key wins. If neither file has the key,
an empty string is returned.

Design constraints:
- Never reads os.environ directly for secrets.
- Never surfaces token values in log output.
- credentials.db is always preferred when a stored token exists (this
  module is the fallback of last resort).

Key mapping from secrets.env → cipher-workforce names:
  TELEGRAM_BOT_TOKEN      — same key in both files
  TELEGRAM_CHAT_ID        — maps to TELEGRAM_OWNER_CHAT_ID here
  TELEGRAM_OWNER_CHAT_ID  — explicit override in workforce env

Do NOT spin up a parallel bot. Do NOT generate new tokens. The cipher-
workforce Telegram integration shares the existing live bot.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Priority-ordered list of credential env files.
# Earlier entries win on key conflict.
_CREDENTIAL_ENV_PATHS: list[Path] = [
    Path.home() / ".cipheros" / "workforce" / ".env",
    Path.home() / ".config" / "cipher-os" / "secrets.env",
]

# Keys in the canonical secrets.env that map to different names in the
# cipher-workforce Telegram credential surface.
_KEY_ALIASES: dict[str, list[str]] = {
    # workforce name -> list of canonical aliases (checked in order)
    "TELEGRAM_OWNER_CHAT_ID": ["TELEGRAM_OWNER_CHAT_ID", "TELEGRAM_CHAT_ID"],
}

_CW_ENV: dict[str, str] | None = None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    # Strip trailing inline comments (space + hash).
                    if " #" in val:
                        val = val[: val.index(" #")].strip()
                    if key:
                        result[key] = val
    except OSError as exc:
        logger.debug("[telegram._cw_env] Could not read %s: %s", path, exc)
    return result


def _load_cw_env() -> dict[str, str]:
    global _CW_ENV
    if _CW_ENV is not None:
        return _CW_ENV
    merged: dict[str, str] = {}
    # Load in REVERSE priority order so higher-priority files win (last write wins).
    for path in reversed(_CREDENTIAL_ENV_PATHS):
        if path.exists():
            merged.update(_parse_env_file(path))
    _CW_ENV = merged
    return merged


def get_cw_env(key: str, default: str = "") -> str:
    """Return a value from the cipher-workforce credential env files.

    Checks key aliases so canonical names (e.g. TELEGRAM_CHAT_ID from
    secrets.env) resolve to the workforce-canonical name.
    """
    env = _load_cw_env()
    # Try the canonical key first.
    if key in env and env[key]:
        return env[key]
    # Try any registered aliases for this key.
    for alias in _KEY_ALIASES.get(key, []):
        if alias != key and alias in env and env[alias]:
            return env[alias]
    return default


def telegram_bot_token_default() -> str:
    """Return TELEGRAM_BOT_TOKEN from the credential env files, or empty string."""
    return get_cw_env("TELEGRAM_BOT_TOKEN")


def telegram_owner_chat_id_default() -> str:
    """Return TELEGRAM_OWNER_CHAT_ID (or TELEGRAM_CHAT_ID alias) from the
    credential env files, or empty string."""
    return get_cw_env("TELEGRAM_OWNER_CHAT_ID")
