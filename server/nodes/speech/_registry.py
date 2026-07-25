"""Two provider registries — one per direction.

The mechanism lives in :mod:`services.provider_registry`, shared with
``services/llm``. This module is the speech-specific pair of instances plus
the free-function API provider modules import.

Why two rather than one registry with a capability flag: **membership is the
capability**. A synthesis-only vendor registers into ``tts`` and simply never
appears in the transcription node's dropdown, because that dropdown is
literally ``stt_providers()``. There is no ``supports_stt`` boolean to fall
out of sync with what the provider can actually do, and no way to select a
provider for a direction it cannot serve.
"""

from __future__ import annotations

from typing import List

from services.provider_registry import (
    ProviderRegistry,
    ProviderSpec as _ProviderSpec,
)
from ._protocol import SttProvider, TtsProvider

# Specialized to each direction's protocol so ``ProviderSpec(factory=...)``
# type-checks against the right shape.
TtsProviderSpec = _ProviderSpec[TtsProvider]
SttProviderSpec = _ProviderSpec[SttProvider]

_TTS_REGISTRY: ProviderRegistry[TtsProvider] = ProviderRegistry("text-to-speech")
_STT_REGISTRY: ProviderRegistry[SttProvider] = ProviderRegistry("speech-to-text")


def register_tts_provider(spec: TtsProviderSpec) -> None:
    """Register a synthesis provider. Idempotent for identical specs."""
    _TTS_REGISTRY.register(spec)


def register_stt_provider(spec: SttProviderSpec) -> None:
    """Register a transcription provider. Idempotent for identical specs."""
    _STT_REGISTRY.register(spec)


def get_tts_provider(name: str) -> TtsProviderSpec:
    """Look up a synthesis spec; raises ``NodeUserError`` on a miss."""
    return _TTS_REGISTRY.get(name)


def get_stt_provider(name: str) -> SttProviderSpec:
    """Look up a transcription spec; raises ``NodeUserError`` on a miss."""
    return _STT_REGISTRY.get(name)


def tts_providers() -> List[str]:
    """Sorted synthesis provider names — the TTS node's provider enum."""
    return _TTS_REGISTRY.all()


def stt_providers() -> List[str]:
    """Sorted transcription provider names — the STT node's provider enum."""
    return _STT_REGISTRY.all()


def has_tts_provider(name: str) -> bool:
    return _TTS_REGISTRY.has(name)


def has_stt_provider(name: str) -> bool:
    return _STT_REGISTRY.has(name)


def _reset_for_tests() -> None:
    """Test-only — drop every registered provider in both directions."""
    _TTS_REGISTRY.reset_for_tests()
    _STT_REGISTRY.reset_for_tests()


__all__ = [
    "SttProviderSpec",
    "TtsProviderSpec",
    "get_stt_provider",
    "get_tts_provider",
    "has_stt_provider",
    "has_tts_provider",
    "register_stt_provider",
    "register_tts_provider",
    "stt_providers",
    "tts_providers",
]
