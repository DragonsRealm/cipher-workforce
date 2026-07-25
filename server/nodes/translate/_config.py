"""Capability lookups backed by ``server/config/translate_defaults.json``.

Thin instance over :class:`services.plugin.capabilities.CapabilityConfig`,
the same resolver ``nodes/speech`` uses. Sections here are the three
capabilities rather than two directions.
"""

from __future__ import annotations

from typing import Any, List, Optional

from services.plugin.capabilities import CapabilityConfig

TRANSLATE = "translate"
TRANSLITERATE = "transliterate"
DETECT = "detect"

CONFIG = CapabilityConfig("translate_defaults.json")
TRANSLATE_DEFAULTS = CONFIG.data


def reload_defaults() -> None:
    CONFIG.reload()


def base_url(provider: str, *, api_key: str = "") -> str:
    """Base URL, honouring DeepL's free-vs-pro host split.

    A DeepL free key ends in ``:fx`` and is rejected by the pro host with a
    403 that says nothing useful. Selecting the host from the key means a
    user never has to know which tier they hold, and cannot mis-configure it.
    """
    block = CONFIG.provider_block(provider)
    free = block.get("free_base_url")
    if free and api_key.endswith(":fx"):
        return str(free)
    return CONFIG.base_url(provider)


def credential_id(provider: str) -> str:
    return CONFIG.credential_id(provider)


def capability(
    provider: str,
    section: str,
    key: str,
    *,
    model: Optional[str] = None,
    default: Any = None,
) -> Any:
    return CONFIG.capability(provider, section, key, model=model, default=default)


def supports(
    provider: str, section: str, key: str, *, model: Optional[str] = None
) -> bool:
    return CONFIG.supports(provider, section, key, model=model)


def supports_capability(provider: str, section: str) -> bool:
    return CONFIG.supports_section(provider, section)


def default_model(provider: str, section: str) -> str:
    return CONFIG.default_model(provider, section)


def models(provider: str, section: str) -> List[str]:
    return CONFIG.models(provider, section)


def endpoint(provider: str, section: str, key: str = "endpoint") -> str:
    return CONFIG.endpoint(provider, section, key)


def languages(
    provider: str, section: str, *, model: Optional[str] = None
) -> List[str]:
    return CONFIG.languages(provider, section, model=model)


def default_target_language(provider: str, section: str) -> str:
    return str(
        CONFIG.capability(provider, section, "default_target_language", default="") or ""
    )


def max_input_chars(
    provider: str, section: str, model: Optional[str] = None
) -> Optional[int]:
    return CONFIG.max_input_chars(provider, section, model)


def billed_unit(provider: str, section: str) -> str:
    return CONFIG.billed_unit(provider, section)


def listing(
    provider: str, section: str, key: str, *, model: Optional[str] = None
) -> List[Any]:
    return CONFIG.listing(provider, section, key, model=model)


__all__ = [
    "CONFIG",
    "DETECT",
    "TRANSLATE",
    "TRANSLATE_DEFAULTS",
    "TRANSLITERATE",
    "base_url",
    "billed_unit",
    "capability",
    "credential_id",
    "default_model",
    "default_target_language",
    "endpoint",
    "languages",
    "listing",
    "max_input_chars",
    "models",
    "reload_defaults",
    "supports",
    "supports_capability",
]
