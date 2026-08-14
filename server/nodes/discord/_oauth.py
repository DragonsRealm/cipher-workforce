"""Discord OAuth2 client for user-context authorisation.

Subclasses the shared PKCE base, which owns the state store, code-verifier
generation, code exchange, refresh and revocation. Only the endpoints, the
scope set and the user-info translation are Discord-specific.

Distinct from the bot token: that authenticates the *app*, this authorises a
*user*. They are stored under different credential ids so connecting one
never overwrites the other.

Scopes are the minimum that identifies the user. ``email`` is deliberately
not requested -- nothing here needs it, and asking widens the consent screen
and the blast radius of a leaked token for no gain.
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional

import httpx

from core.logging import get_logger
from services.plugin.oauth import OAuth2PKCEClient

logger = get_logger(__name__)

USER_INFO_URL = "https://discord.com/api/v10/users/@me"

CLIENT_ID_KEY = "discord_client_id"
CLIENT_SECRET_KEY = "discord_client_secret"


class DiscordOAuth(OAuth2PKCEClient):
    provider: ClassVar[str] = "discord"
    authorization_endpoint: ClassVar[str] = "https://discord.com/oauth2/authorize"
    token_endpoint: ClassVar[str] = "https://discord.com/api/oauth2/token"
    revocation_endpoint: ClassVar[str] = "https://discord.com/api/oauth2/token/revoke"

    DEFAULT_SCOPES: ClassVar[List[str]] = ["identify", "guilds"]

    async def fetch_user_info(self, access_token: str) -> Dict[str, Any]:
        """Translate Discord's user object into the unified shape.

        Bearer here, not Bot: this token represents a user, and the bot
        prefix would be rejected.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                USER_INFO_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": "DiscordBot (https://github.com/trohitg/opencompany, 1.0)",
                },
            )
            response.raise_for_status()
            user = response.json()

        username = user.get("username") or ""
        return {
            "id": str(user.get("id") or ""),
            "username": username,
            "name": user.get("global_name") or username,
            "email": user.get("email") or "",
        }


async def build_oauth(
    *,
    redirect_uri: Optional[str] = None,
    **_kwargs: Any,
) -> DiscordOAuth:
    """Build a client from stored application credentials."""
    from services.plugin.deps import get_auth_service

    auth_service = get_auth_service()
    client_id = await auth_service.get_api_key(CLIENT_ID_KEY) or ""
    client_secret = await auth_service.get_api_key(CLIENT_SECRET_KEY) or ""
    return DiscordOAuth(
        client_id=client_id,
        client_secret=client_secret or None,
        redirect_uri=redirect_uri or "",
    )


__all__ = ["CLIENT_ID_KEY", "CLIENT_SECRET_KEY", "DiscordOAuth", "build_oauth"]
