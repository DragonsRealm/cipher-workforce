"""Capability lookups backed by ``server/config/speech_defaults.json``.

The resolver itself lives in
:class:`services.plugin.capabilities.CapabilityConfig`, shared with
``nodes/translate``. This module is the speech-specific instance plus the
free-function API the nodes and loaders import — the same shim relationship
``services/llm/registry.py`` has with ``services/provider_registry``.
"""

from __future__ import annotations

from typing import Any, List, Optional

from services.plugin.capabilities import CapabilityConfig

TTS = "tts"
STT = "stt"

CONFIG = CapabilityConfig("speech_defaults.json")

# Call sites (and tests) that read the parsed document directly, e.g. to
# monkeypatch in a synthetic provider block.
SPEECH_DEFAULTS = CONFIG.data


def reload_defaults() -> None:
    CONFIG.reload()


def provider_block(provider: str) -> dict:
    return CONFIG.provider_block(provider)


def direction_block(provider: str, direction: str) -> dict:
    return CONFIG.section(provider, direction)


def supports_direction(provider: str, direction: str) -> bool:
    return CONFIG.supports_section(provider, direction)


def base_url(provider: str) -> str:
    return CONFIG.base_url(provider)


def credential_id(provider: str) -> str:
    return CONFIG.credential_id(provider)


def capability(
    provider: str,
    direction: str,
    key: str,
    *,
    model: Optional[str] = None,
    default: Any = None,
) -> Any:
    return CONFIG.capability(provider, direction, key, model=model, default=default)


def default_model(provider: str, direction: str) -> str:
    return CONFIG.default_model(provider, direction)


def models(provider: str, direction: str) -> List[str]:
    return CONFIG.models(provider, direction)


def voices(provider: str, *, model: Optional[str] = None) -> List[str]:
    """Static voice ids from config.

    Only a fallback. Providers with a live voices endpoint (ElevenLabs)
    answer the dropdown from the API instead, because their catalogue is
    per-account and changes without a release.
    """
    return CONFIG.listing(provider, TTS, "voices", model=model)


def default_voice(provider: str) -> str:
    return str(CONFIG.capability(provider, TTS, "default_voice", default="") or "")


def output_formats(provider: str, direction: str = TTS) -> List[str]:
    return CONFIG.listing(provider, direction, "output_formats")


def billed_unit(provider: str, direction: str) -> str:
    return CONFIG.billed_unit(provider, direction)


def endpoint(provider: str, direction: str, key: str = "endpoint") -> str:
    return CONFIG.endpoint(provider, direction, key)


def max_input_chars(provider: str, model: Optional[str] = None) -> Optional[int]:
    return CONFIG.max_input_chars(provider, TTS, model)


def speed_range(
    provider: str, model: Optional[str] = None
) -> Optional[tuple[float, float]]:
    value = CONFIG.capability(provider, TTS, "speed_range", model=model)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return None


def supports(
    provider: str, direction: str, key: str, *, model: Optional[str] = None
) -> bool:
    return CONFIG.supports(provider, direction, key, model=model)


def response_formats(provider: str, model: Optional[str] = None) -> List[str]:
    """Allowed ``response_format`` values for a transcription model.

    Model-gated on OpenAI and a 400 when violated, which is why this is
    config rather than something the node discovers at runtime.
    """
    return CONFIG.listing(provider, STT, "response_formats", model=model)


__all__ = [
    "CONFIG",
    "SPEECH_DEFAULTS",
    "STT",
    "TTS",
    "base_url",
    "billed_unit",
    "capability",
    "credential_id",
    "default_model",
    "default_voice",
    "direction_block",
    "endpoint",
    "max_input_chars",
    "models",
    "output_formats",
    "provider_block",
    "reload_defaults",
    "response_formats",
    "speed_range",
    "supports",
    "supports_direction",
    "voices",
]
