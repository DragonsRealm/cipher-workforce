"""Credentials for translate-only providers.

Only DeepL. Sarvam and OpenAI already have credentials under
``nodes/model/_credentials.py`` — the same stored key authenticates their chat
surfaces, and a second class for the same ``id`` would collide in
``CREDENTIAL_REGISTRY``.
"""

from __future__ import annotations

from typing import Any, Dict

from services.plugin.credential import ApiKeyCredential, ProbeResult


class DeepLCredential(ApiKeyCredential):
    """DeepL API key.

    The scheme keyword is ``DeepL-Auth-Key``, which none of the three built-in
    ``key_location`` modes can express — ``header`` sends the raw key with no
    keyword, ``bearer`` sends the wrong one. Hence the :meth:`inject`
    override, which the declarative probe also routes through.

    Free and Pro keys go to *different hosts*: a free key ends in ``:fx`` and
    the Pro host rejects it with a 403 that explains nothing. The probe picks
    the host from the key, so validation succeeds on either tier and the user
    never has to know which they hold.
    """

    id = "deepl"
    display_name = "DeepL"
    category = "AI"
    key_name = "Authorization"
    key_location = "header"
    docs_url = "https://www.deepl.com/pro-api"

    @classmethod
    def inject(
        cls, secrets: Dict[str, Any], request: Dict[str, Any]
    ) -> Dict[str, Any]:
        headers = dict(request.get("headers") or {})
        headers["Authorization"] = f"DeepL-Auth-Key {secrets.get('api_key', '')}"
        return {**request, "headers": headers}

    @classmethod
    async def _probe(cls, api_key: str) -> ProbeResult:
        import httpx

        base = (
            "https://api-free.deepl.com/v2"
            if api_key.endswith(":fx")
            else "https://api.deepl.com/v2"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base}/usage",
                headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            )
            response.raise_for_status()
            usage = response.json()

        used = usage.get("character_count")
        limit = usage.get("character_limit")
        message = "API key validated"
        if isinstance(used, int) and isinstance(limit, int) and limit:
            message = f"API key validated — {used:,} of {limit:,} characters used"
        return ProbeResult(valid=True, message=message)


__all__ = ["DeepLCredential"]
