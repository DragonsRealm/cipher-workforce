"""Generic provider-plugin registry, shared by every provider layer.

Extracted from ``services/llm/registry.py`` when ``services/speech`` needed
the same machinery. Nothing here knows what a provider *does* — it holds a
name, a factory, and the typed SDK exceptions that layer's dispatcher must
translate. ``services/llm`` and ``services/speech`` each own a
:class:`ProviderRegistry` instance; speech owns two, one per direction.

The one subtle piece is ``sdk_exception_refs``. They are
``"module:ClassName"`` strings rather than classes so that *registering* a
provider never imports its SDK. That is load-bearing for startup time:
eagerly importing openai + anthropic + google.genai at registration cost
roughly 7 s warm and 45 s cold, and is recorded as an anti-pattern in
docs-internal/performance.md. By the time anything reads the resolved
classes, the factory has already built a client, so resolution is a
``sys.modules`` hit.
"""

from __future__ import annotations

import functools
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, List, Tuple, Type, TypeVar

from core.logging import get_logger
from services.plugin import NodeUserError

logger = get_logger(__name__)

ProviderT = TypeVar("ProviderT")


@functools.lru_cache(maxsize=None)
def resolve_exception_ref(ref: str) -> Type[BaseException]:
    """Resolve a ``"module:ClassName"`` ref to the exception class."""
    obj = pkgutil.resolve_name(ref)
    if not (isinstance(obj, type) and issubclass(obj, BaseException)):
        raise TypeError(f"{ref!r} did not resolve to an exception class: {obj!r}")
    return obj


@dataclass(frozen=True)
class ProviderSpec(Generic[ProviderT]):
    """Declarative spec for one provider plugin.

    Fields:
        name: registry key — matches the ``provider`` string used by nodes
            and the ``providers.<name>`` block in the layer's JSON config.
        factory: callable returning a provider instance, invoked as
            ``factory(api_key=..., **client_kwargs)``.
        sdk_exception_refs: ``"module:ClassName"`` refs naming the typed
            errors this provider's SDK raises. Refs, not classes — see the
            module docstring.
        client_kwargs: static keyword arguments merged into every
            instantiation. How OpenAI-compatible providers pin ``base_url``
            from JSON instead of from Python.
    """

    name: str
    factory: Callable[..., ProviderT]
    sdk_exception_refs: Tuple[str, ...]
    client_kwargs: Dict[str, Any] = field(default_factory=dict)

    @property
    def sdk_exception_types(self) -> Tuple[Type[BaseException], ...]:
        """Resolved typed SDK error classes, for one ``except`` clause."""
        return tuple(resolve_exception_ref(ref) for ref in self.sdk_exception_refs)


class ProviderRegistry(Generic[ProviderT]):
    """One registry per provider layer and direction.

    ``label`` appears in log lines and error messages, so it should read
    naturally in "Unknown {label} provider: 'x'".
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._specs: Dict[str, ProviderSpec[ProviderT]] = {}

    @property
    def label(self) -> str:
        return self._label

    def register(self, spec: ProviderSpec[ProviderT]) -> None:
        """Register a provider. Idempotent for identical specs.

        Re-registration with an identical spec is a no-op so uvicorn reload
        cycles are harmless; re-registration with a *different* spec raises
        so accidental shadowing is loud rather than silent.
        """
        # Check the raw refs, NOT the resolving property — reading the
        # property here would import every SDK at registration time and
        # defeat the entire lazy-ref design.
        if not spec.sdk_exception_refs:
            raise ValueError(
                f"ProviderSpec({spec.name!r}) declares empty sdk_exception_refs. "
                "Every provider must surface its typed SDK error class so the "
                "dispatcher can translate it into NodeUserError."
            )
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(
                f"{self._label} provider {spec.name!r} already registered with a "
                f"different spec. Existing: {existing!r}; new: {spec!r}."
            )
        self._specs[spec.name] = spec
        logger.debug("registered provider", layer=self._label, provider=spec.name)

    def get(self, name: str) -> ProviderSpec[ProviderT]:
        """Look up a spec, raising ``NodeUserError`` on a miss.

        ``NodeUserError`` rather than ``KeyError`` so an unknown provider
        surfaces through ``BaseNode.execute()`` as one WARN line with no
        traceback — the same contract as every other user-correctable
        failure in the framework.
        """
        spec = self._specs.get(name)
        if spec is None:
            raise NodeUserError(
                f"Unknown {self._label} provider: {name!r}. "
                f"Registered providers: {sorted(self._specs)}"
            )
        return spec

    def all(self) -> List[str]:
        return sorted(self._specs)

    def has(self, name: str) -> bool:
        """Cheap membership probe — does not raise."""
        return name in self._specs

    def reset_for_tests(self) -> None:
        """Test-only. Never call from runtime code."""
        self._specs.clear()


__all__ = ["ProviderRegistry", "ProviderSpec", "resolve_exception_ref"]
