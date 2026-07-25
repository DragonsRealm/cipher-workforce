"""Speech plugin — provider-abstracted text-to-speech and speech-to-text.

Two nodes, one provider dropdown each. Everything a vendor needs lives in
this folder: the wire protocol, the two registries, the JSON-backed
capability lookups, dispatch, and the provider implementations. Nothing
under ``services/`` knows a speech provider's name.

What this folder deliberately does *not* own, because it is genuinely
shared:

``services/provider_registry.py``
    The registry mechanism, shared with ``services/llm``.
``services/media/``
    Audio transport and workspace containment — vendor-neutral, and usable
    by any node that produces or consumes media.
``server/config/speech_defaults.json``
    Operator-editable capability data, alongside ``llm_defaults.json`` and
    ``email_providers.json``.

Layout::

    text_to_speech.py / speech_to_text.py   the two nodes
    _protocol.py                            requests, results, errors, protocols
    _registry.py                            one registry per direction
    _config.py                              speech_defaults.json lookups
    _unifier.py                             dispatch + error translation
    _providers/                             one module per vendor
    _credentials.py                         speech-only credentials
    _base.py                                shared node helpers
    _option_loaders.py                      dropdown loaders
"""

from __future__ import annotations

from services.node_output_schemas import register_output_schema
from services.ws_handler_registry import register_option_loader

from ._option_loaders import (
    load_speech_formats,
    load_speech_languages,
    load_speech_models,
    load_speech_sample_rates,
    load_speech_voices,
)
from .speech_to_text import SpeechToTextNode, SpeechToTextOutput
from .text_to_speech import TextToSpeechNode, TextToSpeechOutput

# Runtime output shapes, so the parameter panel can offer these fields as
# drag sources for downstream nodes.
register_output_schema(TextToSpeechNode.type, TextToSpeechOutput)
register_output_schema(SpeechToTextNode.type, SpeechToTextOutput)

# Dropdown loaders. The names must match the `loadOptionsMethod` strings on
# the Params fields or `test_node_spec.py` fails.
register_option_loader("speechModels", load_speech_models)
register_option_loader("speechVoices", load_speech_voices)
register_option_loader("speechLanguages", load_speech_languages)
register_option_loader("speechFormats", load_speech_formats)
register_option_loader("speechSampleRates", load_speech_sample_rates)

__all__ = [
    "SpeechToTextNode",
    "SpeechToTextOutput",
    "TextToSpeechNode",
    "TextToSpeechOutput",
]
