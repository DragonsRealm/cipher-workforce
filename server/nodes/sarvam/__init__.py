"""Sarvam AI service plugins.

Three stateless REST nodes over ``api.sarvam.ai`` — translate,
transliterate and language ID — sharing one ``api-subscription-key``
credential. Node classes register themselves on import via
``BaseNode.__init_subclass__``; the only explicit wiring here is the
output-schema registry, which the parameter panel reads to render
"Input Data & Variables" for downstream nodes.

Speech used to live here too, as ``sarvamTextToSpeech`` and
``sarvamSpeechToText``. Both were retired in favour of the
provider-abstracted ``textToSpeech`` / ``speechToText`` nodes in
``nodes/speech/``, where Sarvam is one provider among several and the
same canvas node can switch vendors without being replaced. The Sarvam
wire logic was ported verbatim into ``nodes/speech/_providers/sarvam.py``.

Sarvam's OpenAI-compatible chat endpoint is a separate plugin
(``nodes/model/sarvam_chat_model``) registered through the standard
LLM-provider path, and every surface shares ``SarvamCredential``.
"""

from services.node_output_schemas import register_output_schema

from .sarvam_detect_language import (
    SarvamDetectLanguageNode,
    SarvamDetectLanguageOutput,
)
from .sarvam_translate import SarvamTranslateNode, SarvamTranslateOutput
from .sarvam_transliterate import (
    SarvamTransliterateNode,
    SarvamTransliterateOutput,
)

register_output_schema(SarvamTranslateNode.type, SarvamTranslateOutput)
register_output_schema(SarvamTransliterateNode.type, SarvamTransliterateOutput)
register_output_schema(SarvamDetectLanguageNode.type, SarvamDetectLanguageOutput)

__all__ = [
    "SarvamDetectLanguageNode",
    "SarvamTranslateNode",
    "SarvamTransliterateNode",
]
