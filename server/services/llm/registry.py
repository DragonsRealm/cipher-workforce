"""Provider registry — every LLM provider self-registers here at import time.

Plugin-shape backbone for the chat-model service layer. Each provider module
calls ``register_provider(ProviderSpec(...))`` at top level, and the unifier
reads this registry to dispatch chat / fetch_models calls and to translate
typed SDK exceptions into ``NodeUserError``. No per-provider Python lives
inside ``services/ai.py`` after this layer is wired.

The mechanism itself now lives in :mod:`services.provider_registry`, shared
with ``services/speech`` — this module is the LLM-specific instance plus the
free-function API that provider modules and tests already import. The public
surface (``ProviderSpec``, ``register_provider``, ``get_provider``,
``all_providers``, ``has_provider``, ``_reset_for_tests``) is unchanged.
"""

from __future__ import annotations

from typing import List

from services.llm.protocol import LLMProvider
from services.provider_registry import (
    ProviderRegistry,
    ProviderSpec as _ProviderSpec,
    resolve_exception_ref as _resolve_exc,  # noqa: F401 — re-exported for tests
)

# Re-exported under the historical name. Generic in the provider type, so
# ``ProviderSpec(factory=...)`` still type-checks against ``LLMProvider``.
ProviderSpec = _ProviderSpec[LLMProvider]

_LLM_REGISTRY: ProviderRegistry[LLMProvider] = ProviderRegistry("LLM")

# Compatibility alias bound to the SAME dict object the registry mutates.
# Several tests swap a provider's factory in place via ``_REGISTRY[name] = spec``
# to exercise typed-error translation and client-cache policy without touching
# the real SDKs. Keeping the name (and the identity) means this extraction is
# invisible to them — the success criterion for the refactor was that
# ``tests/llm/`` passes untouched.
_REGISTRY = _LLM_REGISTRY._specs


def register_provider(spec: ProviderSpec) -> None:
    """Register a provider plugin. Idempotent for identical specs."""
    _LLM_REGISTRY.register(spec)


def get_provider(name: str) -> ProviderSpec:
    """Look up a provider spec by name; raises ``NodeUserError`` on a miss."""
    return _LLM_REGISTRY.get(name)


def all_providers() -> List[str]:
    """Return sorted list of registered provider names."""
    return _LLM_REGISTRY.all()


def has_provider(name: str) -> bool:
    """Cheap membership probe — does not raise on miss."""
    return _LLM_REGISTRY.has(name)


def _reset_for_tests() -> None:
    """Test-only — drop every registered provider."""
    _LLM_REGISTRY.reset_for_tests()


__all__ = [
    "ProviderSpec",
    "all_providers",
    "get_provider",
    "has_provider",
    "register_provider",
]
