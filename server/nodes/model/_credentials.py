"""LLM provider credentials (Wave 11.E.1 — per-domain).

One :class:`ApiKeyCredential` per provider. Used by the chat-model
plugins in this folder (openai, anthropic, gemini, openrouter, groq,
cerebras, deepseek, kimi, mistral, ollama, lmstudio) plus the xAI
credential referenced by agent plugins. At execution time the plugin's
The native SDK client pulls the key directly from
:mod:`services.auth`; this class is the Credentials-modal + discovery
manifest, not the runtime client.

Local servers (Ollama, LM Studio) follow the same shape as the cloud
credentials but their api_key is optional — many users run them on
localhost with no auth. The existing ``{provider}_proxy`` mechanism
in :func:`services.ai.AIService.create_model` already handles the
"override base_url + use placeholder api_key" path; the credential
class only needs to return a placeholder when nothing is stored so
the central "API key is required" check in ``execute_chat`` passes.
"""

from __future__ import annotations

from typing import Any, Dict

from services.plugin.credential import ApiKeyCredential, ProbeResult


class _LLMApiKey(ApiKeyCredential):
    """Shared defaults. Subclasses only set id / display_name / icon.

    The :meth:`_probe` override calls ``ai_service.fetch_models`` —
    every cloud LLM provider in this file inherits it, so adding a new
    OpenAI-compatible provider is purely declarative (id + base_url in
    JSON; no validator code). The local-server credential override
    (:class:`_LocalLLM`) supersedes ``validate`` entirely because its
    side-effect ordering differs (URL stored under ``{id}_proxy``
    before the probe + per-model context registration after).
    """

    category = "AI"
    key_name = "Authorization"
    key_location = "bearer"

    @classmethod
    async def _probe(cls, api_key: str) -> ProbeResult:
        """Default LLM probe: fetch the provider's model list.

        Hits ``GET /v1/models`` (or the provider equivalent) via
        :meth:`AIService.fetch_models`. Returns a populated
        :class:`ProbeResult` on success; raises ``httpx``/``openai``
        exceptions for the base ``Credential.validate`` to classify.
        """
        from services.plugin.deps import get_ai_service

        ai_service = get_ai_service()
        models = await ai_service.fetch_models(cls.id, api_key)
        return ProbeResult(
            valid=True,
            message="API key validated",
            models=models,
        )


# NOTE: ``icon`` is intentionally NOT declared on the LLM credentials
# below. ``cls.icon`` is documentation-only — nothing reads it at
# runtime; the catalogue (``server/config/credential_providers.json``
# ``icon_ref`` field) is the FE-visible source of truth. Each LLM
# provider's icon_ref uses ``lobehub:<brand>`` (the @lobehub/icons
# React component). Per RFC F7 cleanup, the stale ``asset:<key>``
# declarations were dropped — they pointed at frontend SVGs that
# didn't exist for any LLM provider.


class OpenAICredential(_LLMApiKey):
    id = "openai"
    display_name = "OpenAI"
    docs_url = "https://platform.openai.com/api-keys"


class AnthropicCredential(_LLMApiKey):
    id = "anthropic"
    display_name = "Anthropic"
    docs_url = "https://console.anthropic.com/settings/keys"
    # Anthropic uses ``x-api-key`` not Bearer.
    key_name = "x-api-key"
    key_location = "header"


class GeminiCredential(_LLMApiKey):
    id = "gemini"
    display_name = "Google Gemini"
    docs_url = "https://ai.google.dev/gemini-api/docs/api-key"
    key_name = "key"
    key_location = "query"


class OpenRouterCredential(_LLMApiKey):
    id = "openrouter"
    display_name = "OpenRouter"
    docs_url = "https://openrouter.ai/keys"


class GroqCredential(_LLMApiKey):
    id = "groq"
    display_name = "Groq"
    docs_url = "https://console.groq.com/keys"


class CerebrasCredential(_LLMApiKey):
    id = "cerebras"
    display_name = "Cerebras"
    docs_url = "https://cloud.cerebras.ai/"


class DeepSeekCredential(_LLMApiKey):
    id = "deepseek"
    display_name = "DeepSeek"
    docs_url = "https://platform.deepseek.com/api_keys"


class KimiCredential(_LLMApiKey):
    id = "kimi"
    display_name = "Kimi (Moonshot)"
    docs_url = "https://platform.moonshot.cn"


class MistralCredential(_LLMApiKey):
    id = "mistral"
    display_name = "Mistral AI"
    docs_url = "https://console.mistral.ai/api-keys/"


class XaiCredential(_LLMApiKey):
    id = "xai"
    display_name = "xAI (Grok)"
    docs_url = "https://console.x.ai"


class SarvamCredential(_LLMApiKey):
    """One subscription key for every Sarvam API.

    Sarvam's chat endpoint is OpenAI-compatible and accepts the key as a
    Bearer token, which is what the openai SDK sends — so the chat path
    never reads ``key_name``. Its other APIs (translate / transliterate /
    text-lid / speech-to-text / text-to-speech, all under
    ``server/nodes/sarvam/``) accept ONLY ``api-subscription-key``, and
    those nodes authenticate through ``ctx.connection("sarvam")`` ->
    :meth:`ApiKeyCredential.inject`. Declaring the native header here
    means a single stored key serves both surfaces.

    Same override shape as :class:`AnthropicCredential`'s ``x-api-key``.
    """

    id = "sarvam"
    display_name = "Sarvam AI"
    docs_url = "https://dashboard.sarvam.ai"
    key_name = "api-subscription-key"
    key_location = "header"


class _LocalLLM(_LLMApiKey):
    """Base for local-server credentials (Ollama, LM Studio).

    Same shape as :class:`_LLMApiKey`, but ``resolve()`` returns the
    documented Ollama placeholder when no key is stored instead of
    raising. The user's custom server address rides on the existing
    ``{id}_proxy`` credential — :func:`services.ai.AIService.create_model`
    already reads it and OpenAIProvider already overrides ``base_url``
    + forces ``api_key="ollama"``. Nothing else to wire.
    """

    @classmethod
    async def resolve(cls, *, user_id: str = "owner") -> Dict[str, Any]:
        from services.plugin.deps import get_auth_service

        api_key = await get_auth_service().get_api_key(cls.id)
        return {"api_key": api_key or "ollama"}

    @classmethod
    async def validate(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Probe the user's local server via the official SDK.

        Overrides the base ``Credential.validate`` because local-LLM
        side-effect ordering genuinely differs from the cloud case:
        the user's URL is persisted under ``{cls.id}_proxy`` BEFORE
        the probe runs, the placeholder ``api_key="ollama"`` is
        stored under ``cls.id`` only on success, and per-model context
        is registered in the model registry. Delegates to the
        SDK-typed probe in ``_local_validator.py`` which already owns
        that full flow.
        """
        from ._local_validator import validate_local_llm

        return await validate_local_llm(dict(data, provider=cls.id))


class OllamaCredential(_LocalLLM):
    id = "ollama"
    display_name = "Ollama"
    icon = "lobehub:ollama"
    docs_url = "https://ollama.com/download"


class LMStudioCredential(_LocalLLM):
    id = "lmstudio"
    display_name = "LM Studio"
    icon = "lobehub:lmstudio"
    docs_url = "https://lmstudio.ai/docs/local-server"
