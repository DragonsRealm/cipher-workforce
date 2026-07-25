"""Capability lookups backed by ``server/config/speech_defaults.json``.

Same contract as :mod:`services.llm.config`: the JSON is loaded once at
import into a module-level dict, failure is soft, and every capability
question is answered from here so no shared code ever branches on a provider
name.

The one idea worth understanding is :func:`capability`. A capability value in
the JSON is either a plain scalar (applies to every model) or a mapping of
model id to value with a ``_default`` fallback. Callers never care which was
written -- they ask for ``(provider, direction, key, model)`` and get a value.
That is what lets OpenAI declare "``response_formats`` is ``json|text`` except
on whisper-1 where it is five formats" without a line of Python.

Lookup order inside a mapping is exact match, then longest prefix match, then
``_default``. Prefix matching earns its keep on dated snapshots -- a key of
``gpt-4o-mini-transcribe`` also answers for
``gpt-4o-mini-transcribe-2025-12-15`` without the JSON tracking every release.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)

TTS = "tts"
STT = "stt"

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "speech_defaults.json"


def _load_speech_defaults() -> Dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        # Soft-fail, matching services/llm/config.py: a malformed config
        # degrades capability lookups to their permissive defaults rather
        # than preventing the process from booting.
        logger.warning("Could not load speech_defaults.json", error=str(exc))
        return {"providers": {}}


SPEECH_DEFAULTS: Dict[str, Any] = _load_speech_defaults()


def reload_defaults() -> None:
    """Re-read the JSON from disk. Mutates in place.

    In-place rather than rebinding because callers may hold a reference to
    the dict (the unifier does), and rebinding would leave them on the stale
    object -- a trap ``services/llm/config.reload_defaults`` still has.
    """
    fresh = _load_speech_defaults()
    SPEECH_DEFAULTS.clear()
    SPEECH_DEFAULTS.update(fresh)


# ---------------------------------------------------------------------------
# Raw block access
# ---------------------------------------------------------------------------


def provider_block(provider: str) -> Dict[str, Any]:
    """The whole ``providers.<name>`` block, or ``{}``."""
    block = SPEECH_DEFAULTS.get("providers", {}).get(provider)
    return block if isinstance(block, dict) else {}


def direction_block(provider: str, direction: str) -> Dict[str, Any]:
    """The ``providers.<name>.<tts|stt>`` block, or ``{}``."""
    block = provider_block(provider).get(direction)
    return block if isinstance(block, dict) else {}


def supports_direction(provider: str, direction: str) -> bool:
    """Whether the JSON declares this provider for this direction.

    Advisory only -- registry membership is the authority (a provider that
    never calls ``register_tts_provider`` cannot be selected regardless of
    what the JSON says). Useful for config-consistency tests.
    """
    return bool(direction_block(provider, direction))


def base_url(provider: str) -> str:
    return str(provider_block(provider).get("base_url") or "")


def credential_id(provider: str) -> str:
    """Credential id string for this provider.

    A *string*, never a ``Credential`` class: the classes live under
    ``nodes/`` and importing one here would invert the layering and break
    ``test_plugin_self_containment.py``. Falls back to the provider name,
    which is the convention every current provider follows.
    """
    return str(provider_block(provider).get("credential_id") or provider)


# ---------------------------------------------------------------------------
# Capability resolution
# ---------------------------------------------------------------------------


def capability(
    provider: str,
    direction: str,
    key: str,
    *,
    model: Optional[str] = None,
    default: Any = None,
) -> Any:
    """Resolve one capability, honouring per-model overrides.

    Returns ``default`` when the key is absent entirely. A declared ``null``
    is returned as ``None`` and is meaningful -- Deepgram's
    ``max_upload_bytes`` is explicitly null because no cap is documented,
    which is different from "not configured".
    """
    block = direction_block(provider, direction)
    if key not in block:
        return default

    value = block[key]
    if not isinstance(value, dict):
        return value

    # A mapping means per-model overrides. Without a model we can still
    # answer from _default, which is what the panel does before the user
    # has picked one.
    if model:
        if model in value:
            return value[model]
        prefix_matches = [k for k in value if k != "_default" and model.startswith(k)]
        if prefix_matches:
            return value[max(prefix_matches, key=len)]
    if "_default" in value:
        return value["_default"]
    return default


def default_model(provider: str, direction: str) -> str:
    return str(capability(provider, direction, "default_model", default="") or "")


def models(provider: str, direction: str) -> List[str]:
    value = capability(provider, direction, "models", default=[])
    return list(value or [])


def voices(provider: str, *, model: Optional[str] = None) -> List[str]:
    """Static voice ids from config.

    Only a fallback. Providers that expose a live voices endpoint
    (ElevenLabs) answer the dropdown from the API instead, because their
    catalogue is per-account and changes without a release.
    """
    value = capability(provider, TTS, "voices", model=model, default=[])
    return list(value or [])


def default_voice(provider: str) -> str:
    return str(capability(provider, TTS, "default_voice", default="") or "")


def output_formats(provider: str, direction: str = TTS) -> List[str]:
    return list(capability(provider, direction, "output_formats", default=[]) or [])


def billed_unit(provider: str, direction: str) -> str:
    """``characters`` / ``seconds`` / ``minutes`` -- for cost attribution."""
    return str(capability(provider, direction, "billed_unit", default="") or "")


def endpoint(provider: str, direction: str, key: str = "endpoint") -> str:
    return str(capability(provider, direction, key, default="") or "")


def max_input_chars(provider: str, model: Optional[str] = None) -> Optional[int]:
    value = capability(provider, TTS, "max_input_chars", model=model)
    return int(value) if isinstance(value, (int, float)) else None


def speed_range(
    provider: str, model: Optional[str] = None
) -> Optional[tuple[float, float]]:
    value = capability(provider, TTS, "speed_range", model=model)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return None


def supports(
    provider: str, direction: str, key: str, *, model: Optional[str] = None
) -> bool:
    """Boolean capability probe.

    Defaults to ``True`` -- permissive, matching ``supports_model_listing``
    in the LLM layer. A provider opts *out* explicitly; forgetting to
    declare a flag never silently disables a working feature.
    """
    return bool(capability(provider, direction, key, model=model, default=True))


def response_formats(provider: str, model: Optional[str] = None) -> List[str]:
    """Allowed ``response_format`` values for a transcription model.

    Model-gated on OpenAI and a 400 when violated, which is why this is
    config rather than something the node discovers at runtime.
    """
    return list(
        capability(provider, STT, "response_formats", model=model, default=[]) or []
    )


__all__ = [
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
