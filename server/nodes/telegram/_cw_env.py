"""Load cipher-workforce isolated credentials for Telegram.

This module is the ONLY place that reads the isolated credential env
(~/.cipheros/workforce/.env) for Telegram-specific keys. It is imported
by TelegramService.connect() to apply bot-token defaults before the
encrypted credentials.db layer is consulted.

Design constraints:
- Never reads os.environ directly for secrets.
- Never surfaces token values in log output.
- The isolated env file is the fallback of last resort; credentials.db
  is always preferred when a stored token exists.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CW_ENV_PATH = Path.home() / ".cipheros" / "workforce" / ".env"

_CW_ENV: dict[str, str] | None = None


def _load_cw_env() -> dict[str, str]:
    global _CW_ENV
    if _CW_ENV is not None:
        return _CW_ENV
    env: dict[str, str] = {}
    if not _CW_ENV_PATH.exists():
        _CW_ENV = env
        return env
    try:
        with _CW_ENV_PATH.open() as fh:
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
                        env[key] = val
    except OSError as exc:
        logger.warning("[telegram._cw_env] Could not read isolated env: %s", exc)
    _CW_ENV = env
    return env


def get_cw_env(key: str, default: str = "") -> str:
    """Return a value from the cipher-workforce isolated credential file."""
    return _load_cw_env().get(key, default)


def telegram_bot_token_default() -> str:
    """Return TELEGRAM_BOT_TOKEN from the isolated env, or empty string."""
    return get_cw_env("TELEGRAM_BOT_TOKEN")


def telegram_owner_chat_id_default() -> str:
    """Return TELEGRAM_OWNER_CHAT_ID from the isolated env, or empty string."""
    return get_cw_env("TELEGRAM_OWNER_CHAT_ID")
