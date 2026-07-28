"""OpenAI-compatible provider registrations.

Nine providers (``xai`` / ``deepseek`` / ``kimi`` / ``mistral`` / ``ollama`` /
``lmstudio`` / ``groq`` / ``cerebras`` / ``sarvam``) all ride the OpenAI Python SDK
with a custom ``base_url``, so they share one factory (``OpenAIProvider``)
and one typed-exception tuple (``openai.OpenAIError``). The per-provider
differences live in ``llm_defaults.json`` (``base_url``, plus the provider
quirks block added in Phase C) — there is no per-provider Python here.

Adding a new OpenAI-compat provider is a JSON edit + one entry in
``_COMPAT_PROVIDERS`` below. Groq + Cerebras registered here in
Phase D (see the ``_COMPAT_PROVIDERS`` comment below); Sarvam followed and
brought the ``supports_model_listing`` flag with it — a provider can be
wire-compatible yet ship no model-list route, which ``OpenAIProvider``
handles generically off that JSON key.
"""

from __future__ import annotations

from typing import Tuple

from core.logging import get_logger
from services.llm.providers.openai import OpenAIProvider
from services.llm.registry import ProviderSpec, register_provider

logger = get_logger(__name__)


# Names match the ``providers.<name>`` keys in ``llm_defaults.json``.
# The compat providers' ``base_url`` is pulled lazily from the JSON
# config. Adding a new compat endpoint requires its JSON entry plus one
# explicit name in this tuple; no provider subclass is required.
#
# Groq + Cerebras moved here in Phase D (May 2026). Both expose
# OpenAI-compatible `/v1` endpoints (``api.groq.com/openai/v1`` /
# ``api.cerebras.ai/v1``) so they share the ``OpenAIProvider`` factory
# with the other compat providers.
_COMPAT_PROVIDERS: Tuple[str, ...] = (
    "xai",
    "deepseek",
    "kimi",
    "mistral",
    "ollama",
    "lmstudio",
    "groq",
    "cerebras",
    # Sarvam serves OpenAI-compatible chat completions and accepts
    # ``Authorization: Bearer`` alongside its native ``api-subscription-key``,
    # so it needs no provider subclass. It is the first provider with no
    # model-list route, which it declares via ``supports_model_listing: false``
    # in llm_defaults.json — handled generically in ``OpenAIProvider``.
    "sarvam",
)


def _load_compat_base_urls() -> dict[str, str]:
    """Read ``providers.<name>.base_url`` from llm_defaults.json.

    Kept independent from ``services.ai`` so provider registration does not
    import the orchestration or legacy Temporal-compatibility layer.
    """
    import json
    from pathlib import Path

    config_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "config"
        / "llm_defaults.json"
    )
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Could not load llm_defaults.json for compat providers", error=str(e))
        return {}
    providers = data.get("providers", {})
    return {
        name: providers.get(name, {}).get("base_url", "")
        for name in _COMPAT_PROVIDERS
    }


def _register_compat_providers() -> None:
    """Register every OpenAI-compatible provider into the global registry.

    Each provider gets the same factory (``OpenAIProvider``) with the
    JSON-declared ``base_url`` pinned via ``client_kwargs``. The unifier
    merges this with the user-configured ``{provider}_proxy`` URL (if
    set) at call time — ``OpenAIProvider.__init__`` prefers ``proxy_url``
    over ``base_url`` so custom endpoints override the JSON default.
    """
    base_urls = _load_compat_base_urls()
    for name in _COMPAT_PROVIDERS:
        base_url = base_urls.get(name, "")
        if not base_url:
            logger.warning(
                "skipping compat provider — no base_url in llm_defaults.json",
                provider=name,
            )
            continue
        register_provider(
            ProviderSpec(
                name=name,
                factory=OpenAIProvider,
                sdk_exception_refs=("openai:OpenAIError",),
                client_kwargs={
                    "base_url": base_url,
                    "provider_name": name,
                },
            )
        )


_register_compat_providers()
