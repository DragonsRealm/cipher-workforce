"""The one file that knows how a Discord account maps onto stored credentials.

API keys are stored under a ``{session_id}_{provider}`` key, so the session_id
column is a free scoping axis. A Discord bot account claims one scope:

    account_id "default"     -> session_id "default"
    account_id "<app_id>"    -> session_id "discord:<app_id>"

``"default"`` is the row the unmodified credentials modal already writes, so a
single-bot install needs no account concept at all and multi-account is purely
additive.

Everything else in the plugin asks this module for secrets and never touches
session ids. ``ctx.connection()`` is deliberately not used for the account
path: it resolves one fixed scope, so it can only ever reach the default
account.

Not to be confused with ``credential_customer_id`` (services/plugin/context.py),
which is per-execution-context tenancy -- two nodes in one workflow could never
target two different bots through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

CREDENTIAL_ID = "discord"
DEFAULT_ACCOUNT = "default"

# Stored per account scope. The token uses the bare credential id so the
# existing modal, which writes provider="discord", lands on the default
# account without special-casing.
TOKEN_KEY = CREDENTIAL_ID
APPLICATION_ID_KEY = "discord_application_id"
PUBLIC_KEY_KEY = "discord_public_key"
LABEL_KEY = "discord_label"

_EXTRA_KEYS = (APPLICATION_ID_KEY, PUBLIC_KEY_KEY, LABEL_KEY)


@dataclass(frozen=True)
class AccountRef:
    """What the account dropdown and the status panel need to show a bot."""

    account_id: str
    label: str
    application_id: str
    has_token: bool
    has_public_key: bool


def storage_scope(account_id: str) -> str:
    """Map an account id onto the session_id its credentials live under."""
    resolved = (account_id or "").strip()
    if resolved in ("", DEFAULT_ACCOUNT):
        return DEFAULT_ACCOUNT
    return f"{CREDENTIAL_ID}:{resolved}"


def account_id_from_scope(scope: str) -> str:
    """Inverse of :func:`storage_scope`."""
    prefix = f"{CREDENTIAL_ID}:"
    return scope[len(prefix) :] if scope.startswith(prefix) else scope


async def resolve_secrets(account_id: str = DEFAULT_ACCOUNT) -> Dict[str, Any]:
    """Read one account's stored credential fields.

    Raises:
        PermissionError: annotated with provider/reason/auth so the framework
            emits its credential envelope and the modal lights up the right
            provider, matching ``ApiKeyCredential.resolve``.
    """
    from services.plugin.deps import get_auth_service

    auth = get_auth_service()
    scope = storage_scope(account_id)

    token = await auth.get_api_key(TOKEN_KEY, scope)
    if not token:
        err = PermissionError(
            f"No Discord bot token stored for account '{account_id or DEFAULT_ACCOUNT}'. "
            "Add one via the Credentials modal."
        )
        err.provider = CREDENTIAL_ID  # type: ignore[attr-defined]
        err.reason = "missing"  # type: ignore[attr-defined]
        err.auth = "api_key"  # type: ignore[attr-defined]
        raise err

    secrets: Dict[str, Any] = {"token": token}
    for key in _EXTRA_KEYS:
        value = await auth.get_api_key(key, scope)
        if value:
            secrets[key] = value
    return secrets


async def list_accounts() -> List[AccountRef]:
    """Enumerate every account with a stored token.

    Scopes come from the credential store rather than a separate index, so
    there is nothing that can disagree with the rows themselves.
    """
    from services.plugin.deps import get_auth_service

    auth = get_auth_service()
    accounts: List[AccountRef] = []

    for scope in await auth.list_key_scopes(TOKEN_KEY):
        account_id = account_id_from_scope(scope)
        application_id = await auth.get_api_key(APPLICATION_ID_KEY, scope) or ""
        label = await auth.get_api_key(LABEL_KEY, scope) or ""
        public_key = await auth.get_api_key(PUBLIC_KEY_KEY, scope) or ""
        accounts.append(
            AccountRef(
                account_id=account_id,
                label=label or application_id or account_id,
                application_id=application_id,
                has_token=True,
                has_public_key=bool(public_key),
            )
        )
    return accounts


__all__ = [
    "APPLICATION_ID_KEY",
    "AccountRef",
    "CREDENTIAL_ID",
    "DEFAULT_ACCOUNT",
    "LABEL_KEY",
    "PUBLIC_KEY_KEY",
    "TOKEN_KEY",
    "account_id_from_scope",
    "list_accounts",
    "resolve_secrets",
    "storage_scope",
]
