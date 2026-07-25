"""Three provider registries — one per capability.

Over the shared mechanism in :mod:`services.provider_registry`, same as
``nodes/speech/_registry.py``. Speech owns two (one per direction); this owns
three, because translation, transliteration and language identification are
separately-supported capabilities rather than facets of one.

Membership is the capability. Each node's provider enum is literally the
matching ``*_providers()`` list, so a translate-only vendor never appears in
the transliteration dropdown and nothing has to remember to keep a
``supports_transliterate`` flag truthful.
"""

from __future__ import annotations

from typing import List

from services.provider_registry import (
    ProviderRegistry,
    ProviderSpec as _ProviderSpec,
)

from ._protocol import DetectProvider, TranslateProvider, TransliterateProvider

TranslateProviderSpec = _ProviderSpec[TranslateProvider]
TransliterateProviderSpec = _ProviderSpec[TransliterateProvider]
DetectProviderSpec = _ProviderSpec[DetectProvider]

_TRANSLATE: ProviderRegistry[TranslateProvider] = ProviderRegistry("translate")
_TRANSLITERATE: ProviderRegistry[TransliterateProvider] = ProviderRegistry(
    "transliterate"
)
_DETECT: ProviderRegistry[DetectProvider] = ProviderRegistry("detect-language")


def register_translate_provider(spec: TranslateProviderSpec) -> None:
    _TRANSLATE.register(spec)


def register_transliterate_provider(spec: TransliterateProviderSpec) -> None:
    _TRANSLITERATE.register(spec)


def register_detect_provider(spec: DetectProviderSpec) -> None:
    _DETECT.register(spec)


def get_translate_provider(name: str) -> TranslateProviderSpec:
    return _TRANSLATE.get(name)


def get_transliterate_provider(name: str) -> TransliterateProviderSpec:
    return _TRANSLITERATE.get(name)


def get_detect_provider(name: str) -> DetectProviderSpec:
    return _DETECT.get(name)


def translate_providers() -> List[str]:
    return _TRANSLATE.all()


def transliterate_providers() -> List[str]:
    return _TRANSLITERATE.all()


def detect_providers() -> List[str]:
    return _DETECT.all()


# Capability name -> (registry accessor, spec getter). Lets the shared node
# helpers and the option loaders stay capability-agnostic instead of
# branching three ways.
CAPABILITIES = {
    "translate": (translate_providers, get_translate_provider),
    "transliterate": (transliterate_providers, get_transliterate_provider),
    "detect": (detect_providers, get_detect_provider),
}


def _reset_for_tests() -> None:
    _TRANSLATE.reset_for_tests()
    _TRANSLITERATE.reset_for_tests()
    _DETECT.reset_for_tests()


__all__ = [
    "CAPABILITIES",
    "DetectProviderSpec",
    "TranslateProviderSpec",
    "TransliterateProviderSpec",
    "detect_providers",
    "get_detect_provider",
    "get_translate_provider",
    "get_transliterate_provider",
    "register_detect_provider",
    "register_translate_provider",
    "register_transliterate_provider",
    "translate_providers",
    "transliterate_providers",
]
