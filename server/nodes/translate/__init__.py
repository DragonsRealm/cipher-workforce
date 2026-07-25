"""Translate plugin — provider-abstracted text-language capabilities.

Three nodes, each with a `provider` dropdown, replacing the vendor-locked
``sarvamTranslate`` / ``sarvamTransliterate`` / ``sarvamDetectLanguage``.

Built on the same pattern as ``nodes/speech/`` and sharing its machinery:
``services/provider_registry`` for registration and
``services/plugin/capabilities`` for the JSON-backed lookups. Speech owns two
registries (one per direction); this owns **three**, one per capability,
because they are separately supported — DeepL translates and nothing else,
while an LLM does all three.

Nothing under ``services/`` knows a vendor name. No media transport is
involved: text results are small, so none of ``services/media`` applies.

Layout::

    translate_text.py / transliterate_text.py / detect_language.py
    _protocol.py       requests, results, errors, three Protocols
    _registry.py       one registry per capability
    _config.py         translate_defaults.json lookups
    _unifier.py        dispatch + error translation
    _providers/        deepl (translate only), sarvam (all three), llm (all three)
    _credentials.py    DeepL only — the rest are shared with nodes/model
    _base.py, _option_loaders.py
"""

from __future__ import annotations

from services.node_output_schemas import register_output_schema
from services.ws_handler_registry import register_option_loader

from ._option_loaders import LOADERS
from .detect_language import DetectLanguageNode, DetectLanguageOutput
from .translate_text import TranslateTextNode, TranslateTextOutput
from .transliterate_text import TransliterateTextNode, TransliterateTextOutput

register_output_schema(TranslateTextNode.type, TranslateTextOutput)
register_output_schema(TransliterateTextNode.type, TransliterateTextOutput)
register_output_schema(DetectLanguageNode.type, DetectLanguageOutput)

for _name, _loader in LOADERS.items():
    register_option_loader(_name, _loader)

__all__ = [
    "DetectLanguageNode",
    "DetectLanguageOutput",
    "TranslateTextNode",
    "TranslateTextOutput",
    "TransliterateTextNode",
    "TransliterateTextOutput",
]
