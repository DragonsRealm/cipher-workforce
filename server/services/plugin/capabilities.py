"""JSON-backed per-provider capability lookups.

A plugin that fronts several interchangeable vendors needs the same thing
every time: a JSON file under ``server/config/`` describing what each provider
supports, resolved per capability and often per model, so that no shared code
ever branches on a provider name.

``nodes/speech`` established the shape; ``nodes/translate`` needed it
identically. Rather than keep two copies of the resolution ladder in step by
hand, it lives here — the same argument that put ``ProviderSpec`` in
``services/provider_registry``.

The idea worth understanding is :meth:`CapabilityConfig.capability`. A value in
the JSON is either a plain scalar (applies to every model) or a mapping of
model id to value with a ``_default`` fallback. Callers never care which was
written — they ask for ``(provider, section, key, model)`` and get a value.
That is what lets OpenAI declare "``response_formats`` is ``json|text`` except
on whisper-1 where it is five formats" without a line of Python.

Lookup order inside a mapping is exact match, then **longest** prefix match,
then ``_default``. Prefix matching earns its keep on dated snapshots: a key of
``gpt-4o-mini-transcribe`` also answers for
``gpt-4o-mini-transcribe-2025-12-15`` without the JSON tracking every release.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


class CapabilityConfig:
    """One provider-capability JSON file.

    ``sections`` are the per-provider sub-blocks — directions for speech
    (``tts`` / ``stt``), capabilities for translate (``translate`` /
    ``transliterate`` / ``detect``). The class does not care what they mean.
    """

    def __init__(self, filename: str):
        self._path = _CONFIG_DIR / filename
        self._data: Dict[str, Any] = {}
        self.reload()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """(Re)read the file. Mutates in place.

        In place rather than rebinding, because callers may hold a reference
        to the underlying dict and rebinding would leave them on the stale
        object — a trap ``services/llm/config.reload_defaults`` still has.
        """
        self._data.clear()
        self._data.update(self._load())

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self._path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            # Soft-fail: a malformed config degrades every lookup to its
            # permissive default rather than preventing the process booting.
            logger.warning(
                "could not load capability config",
                path=str(self._path),
                error=str(exc),
            )
            return {"providers": {}}

    @property
    def data(self) -> Dict[str, Any]:
        """The raw parsed document, for top-level keys outside `providers`."""
        return self._data

    # ------------------------------------------------------------------
    # blocks
    # ------------------------------------------------------------------

    def provider_block(self, provider: str) -> Dict[str, Any]:
        block = self._data.get("providers", {}).get(provider)
        return block if isinstance(block, dict) else {}

    def section(self, provider: str, section: str) -> Dict[str, Any]:
        block = self.provider_block(provider).get(section)
        return block if isinstance(block, dict) else {}

    def supports_section(self, provider: str, section: str) -> bool:
        """Whether the JSON declares this provider for this capability.

        **Advisory only** — registry membership is the authority. A provider
        that never calls its ``register_*`` cannot be selected regardless of
        what the JSON says. Useful for config-consistency tests.
        """
        return bool(self.section(provider, section))

    def base_url(self, provider: str) -> str:
        return str(self.provider_block(provider).get("base_url") or "")

    def credential_id(self, provider: str) -> str:
        """Credential id **string**, never a ``Credential`` class.

        The classes live under ``nodes/``; importing one into a config layer
        would invert the layering. Falls back to the provider name, which is
        the convention most providers follow.
        """
        return str(self.provider_block(provider).get("credential_id") or provider)

    # ------------------------------------------------------------------
    # capability resolution
    # ------------------------------------------------------------------

    def capability(
        self,
        provider: str,
        section: str,
        key: str,
        *,
        model: Optional[str] = None,
        default: Any = None,
    ) -> Any:
        """Resolve one capability, honouring per-model overrides.

        Returns ``default`` only when the key is absent entirely. A declared
        ``null`` comes back as ``None`` and is meaningful — "we checked and
        there is no limit" is different from "not configured".
        """
        block = self.section(provider, section)
        if key not in block:
            return default

        value = block[key]
        if not isinstance(value, dict):
            return value

        if model:
            if model in value:
                return value[model]
            prefixes = [k for k in value if k != "_default" and model.startswith(k)]
            if prefixes:
                return value[max(prefixes, key=len)]
        if "_default" in value:
            return value["_default"]
        return default

    def supports(
        self,
        provider: str,
        section: str,
        key: str,
        *,
        model: Optional[str] = None,
    ) -> bool:
        """Boolean probe, defaulting **permissive**.

        A provider opts *out* explicitly; forgetting to declare a flag never
        silently disables a working feature.
        """
        return bool(self.capability(provider, section, key, model=model, default=True))

    def listing(
        self,
        provider: str,
        section: str,
        key: str,
        *,
        model: Optional[str] = None,
    ) -> List[Any]:
        """A list-valued capability, always a list even when absent."""
        return list(self.capability(provider, section, key, model=model, default=[]) or [])

    def default_model(self, provider: str, section: str) -> str:
        return str(self.capability(provider, section, "default_model", default="") or "")

    def models(self, provider: str, section: str) -> List[str]:
        return self.listing(provider, section, "models")

    def endpoint(self, provider: str, section: str, key: str = "endpoint") -> str:
        return str(self.capability(provider, section, key, default="") or "")

    def max_input_chars(
        self, provider: str, section: str, model: Optional[str] = None
    ) -> Optional[int]:
        value = self.capability(provider, section, "max_input_chars", model=model)
        return int(value) if isinstance(value, (int, float)) else None

    def billed_unit(self, provider: str, section: str) -> str:
        unit = self.capability(provider, section, "billed_unit", default="")
        if unit:
            return str(unit)
        # Fall back to a provider-level declaration, for providers whose
        # billing model is the same across every capability they serve.
        return str(self.provider_block(provider).get("billed_unit") or "")

    def languages(
        self,
        provider: str,
        section: str,
        *,
        model: Optional[str] = None,
        fallback_key: str = "common_languages",
    ) -> List[str]:
        """Language codes, falling back to the document-level shortlist.

        A provider with a genuinely closed set declares its own and that
        wins; open providers borrow the shared list, which is a convenience
        for the dropdown rather than a constraint.
        """
        codes = self.capability(provider, section, "languages", model=model)
        if codes:
            return list(codes)
        return list(self._data.get(fallback_key) or [])


__all__ = ["CapabilityConfig"]
