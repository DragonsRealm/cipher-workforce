"""Credentials for speech-only providers.

Only two live here. OpenAI, Groq and Sarvam already have credentials under
``nodes/model/_credentials.py`` because the same stored key authenticates
their chat surfaces, and a second class for the same ``id`` would collide in
``CREDENTIAL_REGISTRY``. The speech nodes import those rather than
redeclaring them -- the same cross-plugin import ``nodes/sarvam/`` already
does.
"""

from __future__ import annotations

from typing import Any, Dict

from services.plugin.credential import ApiKeyCredential


class ElevenLabsCredential(ApiKeyCredential):
    """ElevenLabs API key.

    Sent as a bare ``xi-api-key`` header with no scheme keyword;
    ``Authorization: Bearer`` is rejected. The probe lists one voice, which
    is the cheapest authenticated call the API offers.
    """

    id = "elevenlabs"
    display_name = "ElevenLabs"
    category = "AI"
    key_name = "xi-api-key"
    key_location = "header"
    docs_url = "https://elevenlabs.io/app/settings/api-keys"
    probe_url = "https://api.elevenlabs.io/v2/voices"
    probe_params = {"page_size": 1}


class DeepgramCredential(ApiKeyCredential):
    """Deepgram API key.

    Deepgram's scheme keyword is ``Token``, not ``Bearer``, which none of
    the three built-in ``key_location`` modes can express: ``header`` would
    send the raw key with no keyword and ``bearer`` would send the wrong
    one. Hence the :meth:`inject` override -- and it is load-bearing for the
    declarative probe too, which builds its request through the same method.
    """

    id = "deepgram"
    display_name = "Deepgram"
    category = "AI"
    key_name = "Authorization"
    key_location = "header"
    docs_url = "https://console.deepgram.com"
    probe_url = "https://api.deepgram.com/v1/projects"

    @classmethod
    def inject(
        cls, secrets: Dict[str, Any], request: Dict[str, Any]
    ) -> Dict[str, Any]:
        headers = dict(request.get("headers") or {})
        headers["Authorization"] = f"Token {secrets.get('api_key', '')}"
        return {**request, "headers": headers}


__all__ = ["DeepgramCredential", "ElevenLabsCredential"]
