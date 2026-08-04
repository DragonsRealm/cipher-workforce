"""Credential for Meta's official WhatsApp Business Platform Cloud API.

Distinct from the ``whatsapp`` credential in ``nodes/whatsapp/``, which pairs
a *personal* account over an unofficial Go bridge. Same product name, wholly
different auth model, so they are separate ids and separate catalogue entries.

The brand icon must live beside this file as ``whatsapp_business.svg`` --
``Credential.get_icon_path`` resolves ``<cls.id>.svg`` co-located with the
class, and nothing else is consulted.
"""

from __future__ import annotations

import httpx

from services.plugin.credential import ApiKeyCredential, ProbeResult


class WhatsAppBusinessCredential(ApiKeyCredential):
    """Graph API bearer token plus the fields the webhook and API need.

    ``apiKey`` is deliberately **not** required at the catalogue level. The
    Connect button gates on required fields only, so marking the token
    required would block the browser sign-up path before it can supply one.
    Runtime resolution still fails loudly when neither path has produced a
    token.
    """

    id = "whatsapp_business"
    display_name = "WhatsApp Business"
    category = "Social"
    key_name = "Authorization"
    key_location = "bearer"
    extra_fields = (
        # HMAC key for X-Hub-Signature-256. This is the Meta *App Secret*,
        # not a per-webhook secret -- Meta issues no per-endpoint secret.
        "whatsapp_business_app_secret",
        # Echoed back during the GET subscription handshake.
        "whatsapp_business_verify_token",
        # Needed to list phone numbers and message templates.
        "whatsapp_business_waba_id",
        # The business phone number messages are sent FROM.
        "whatsapp_business_phone_number_id",
    )
    docs_url = "https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started"

    @classmethod
    async def _probe(cls, api_key: str) -> ProbeResult:
        """Validate the token without sending a message.

        ``GET /{waba-id}/phone_numbers`` is authenticated, read-only and free,
        and it doubles as the call that discovers which numbers the token can
        actually send from -- so a successful probe also proves the WABA id is
        right, which a bare token check would not.
        """
        from services.plugin.deps import get_auth_service

        auth = get_auth_service()
        waba_id = await auth.get_api_key("whatsapp_business_waba_id")
        if not waba_id:
            return ProbeResult(
                valid=False,
                message=(
                    "Add your WhatsApp Business Account ID as well -- the token "
                    "cannot be checked without it."
                ),
            )

        from ._base import GRAPH_API_VERSION, GRAPH_BASE_URL

        url = f"{GRAPH_BASE_URL}/{GRAPH_API_VERSION}/{waba_id}/phone_numbers"
        async with httpx.AsyncClient(timeout=cls.probe_timeout_seconds) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"fields": "id,display_phone_number,verified_name,quality_rating"},
            )
        response.raise_for_status()
        return cls._handle_probe_response(response)

    @classmethod
    def _handle_probe_response(cls, response: httpx.Response) -> ProbeResult:
        payload = response.json()
        numbers = payload.get("data") or []
        if not numbers:
            return ProbeResult(
                valid=False,
                message=(
                    "The token is accepted but that WhatsApp Business Account has "
                    "no phone numbers. Add one in the Meta dashboard first."
                ),
            )

        display = numbers[0].get("display_phone_number") or "?"
        name = numbers[0].get("verified_name")
        label = f"{display} ({name})" if name else display
        suffix = "" if len(numbers) == 1 else f" (+{len(numbers) - 1} more)"
        return ProbeResult(
            valid=True,
            message=f"Connected to {label}{suffix}",
            extra={
                "phone_numbers": [
                    {
                        "id": entry.get("id"),
                        "display_phone_number": entry.get("display_phone_number"),
                        "verified_name": entry.get("verified_name"),
                        # Surfaced verbatim. Meta's developer docs show these
                        # values but never define them, so inventing semantics
                        # here would be guessing.
                        "quality_rating": entry.get("quality_rating"),
                    }
                    for entry in numbers
                ]
            },
        )


__all__ = ["WhatsAppBusinessCredential"]
