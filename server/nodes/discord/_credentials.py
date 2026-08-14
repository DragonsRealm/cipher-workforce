"""Discord bot-token credential."""

from __future__ import annotations

from typing import Any, Dict

import httpx

from services.plugin.credential import ApiKeyCredential, OAuth2Credential, ProbeResult

from ._accounts import APPLICATION_ID_KEY, LABEL_KEY, PUBLIC_KEY_KEY

# Application flags. The message-content intent is privileged: without it the
# gateway delivers empty `content` on most messages and reports no error at
# all, which surfaces as "the Discord node returns blank text". Reading it at
# validation time is the only cheap way to warn before that happens.
_FLAG_MESSAGE_CONTENT = 1 << 18
_FLAG_MESSAGE_CONTENT_LIMITED = 1 << 19


class DiscordBotCredential(ApiKeyCredential):
    id = "discord"
    display_name = "Discord Bot"
    category = "Social"
    key_name = "Authorization"
    key_location = "header"
    extra_fields = (APPLICATION_ID_KEY, PUBLIC_KEY_KEY, LABEL_KEY)
    docs_url = "https://discord.com/developers/docs/intro"

    @classmethod
    def inject(cls, secrets: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
        """Attach ``Authorization: Bot <token>``.

        The inherited implementation offers header / query / bearer, and
        "bearer" hardcodes the literal ``Bearer `` prefix. Discord bot auth
        uses ``Bot ``, and sending the wrong scheme is a silent 401.
        """
        headers = dict(request.get("headers") or {})
        headers["Authorization"] = f"Bot {secrets.get('api_key', '')}"
        return {**request, "headers": headers}

    @classmethod
    async def _probe(cls, api_key: str) -> ProbeResult:
        """Identify the bot, and report whether message content is available.

        Two calls: ``/users/@me`` proves the token, ``/applications/@me``
        carries the flags. The second is best-effort -- a token valid for the
        first but not the second is still a usable token.
        """
        from ._base import API_BASE_URL, API_VERSION, USER_AGENT

        headers = {"Authorization": f"Bot {api_key}", "User-Agent": USER_AGENT}
        base = f"{API_BASE_URL}/{API_VERSION}"

        async with httpx.AsyncClient(timeout=cls.probe_timeout_seconds) as client:
            response = await client.get(f"{base}/users/@me", headers=headers)
            response.raise_for_status()
            user = response.json()

            flags = 0
            application_id = ""
            try:
                app_response = await client.get(f"{base}/applications/@me", headers=headers)
                if app_response.status_code == 200:
                    application = app_response.json()
                    flags = int(application.get("flags") or 0)
                    application_id = str(application.get("id") or "")
            except httpx.HTTPError:
                pass

        username = user.get("username") or "unknown"
        has_message_content = bool(flags & (_FLAG_MESSAGE_CONTENT | _FLAG_MESSAGE_CONTENT_LIMITED))

        message = f"Connected as {username}"
        if not has_message_content:
            message += (
                ". Note: the Message Content intent is not enabled, so inbound "
                "message text will be empty. Enable it under Bot > Privileged "
                "Gateway Intents in the Developer Portal."
            )

        return ProbeResult(
            valid=True,
            message=message,
            extra={
                "bot_id": str(user.get("id") or ""),
                "bot_username": username,
                "application_id": application_id or str(user.get("id") or ""),
                "has_message_content_intent": has_message_content,
            },
        )


class DiscordUserCredential(OAuth2Credential):
    """Tokens for a user who authorised the app, distinct from the bot.

    A separate credential id so connecting a user never overwrites the bot
    token, and so a node can state which identity it acts as. Requests carry
    ``Bearer`` here, unlike the bot's ``Bot`` scheme.
    """

    id = "discord_oauth"
    display_name = "Discord (User)"
    category = "Social"
    authorization_url = "https://discord.com/oauth2/authorize"
    token_url = "https://discord.com/api/oauth2/token"
    client_id_api_key = "discord_client_id"
    client_secret_api_key = "discord_client_secret"
    # Deliberately narrow: nothing here needs email, and asking for it widens
    # both the consent screen and the damage a leaked token can do.
    scopes = ("identify", "guilds")
    docs_url = "https://discord.com/developers/docs/topics/oauth2"
    # The bot's mark; there is no separate user-flow artwork.
    icon = "/api/schemas/credentials/discord/icon"


__all__ = ["DiscordBotCredential", "DiscordUserCredential"]
