"""Sarvam AI service plugins.

Five stateless REST nodes over ``api.sarvam.ai`` — translate,
transliterate, language ID, speech-to-text and text-to-speech — sharing
one ``api-subscription-key`` credential. Node classes register themselves
on import via ``BaseNode.__init_subclass__``; the only explicit wiring
here is the output-schema registry, which the parameter panel reads to
render "Input Data & Variables" for downstream nodes.

Sarvam's OpenAI-compatible chat endpoint is a separate plugin
(``nodes/model/sarvam_chat_model``) registered through the standard
LLM-provider path, and both surfaces share ``SarvamCredential``.
"""

from services.node_output_schemas import register_output_schema

from .sarvam_detect_language import (
    SarvamDetectLanguageNode,
    SarvamDetectLanguageOutput,
)
from .sarvam_speech_to_text import (
    SarvamSpeechToTextNode,
    SarvamSpeechToTextOutput,
)
from .sarvam_text_to_speech import (
    SarvamTextToSpeechNode,
    SarvamTextToSpeechOutput,
)
from .sarvam_translate import SarvamTranslateNode, SarvamTranslateOutput
from .sarvam_transliterate import (
    SarvamTransliterateNode,
    SarvamTransliterateOutput,
)

register_output_schema(SarvamTranslateNode.type, SarvamTranslateOutput)
register_output_schema(SarvamTransliterateNode.type, SarvamTransliterateOutput)
register_output_schema(SarvamDetectLanguageNode.type, SarvamDetectLanguageOutput)
register_output_schema(SarvamSpeechToTextNode.type, SarvamSpeechToTextOutput)
register_output_schema(SarvamTextToSpeechNode.type, SarvamTextToSpeechOutput)

__all__ = [
    "SarvamDetectLanguageNode",
    "SarvamSpeechToTextNode",
    "SarvamTextToSpeechNode",
    "SarvamTranslateNode",
    "SarvamTransliterateNode",
]
