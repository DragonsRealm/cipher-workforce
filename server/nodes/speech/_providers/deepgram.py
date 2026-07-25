"""Deepgram speech-to-text.

Diverges from every other provider here on all three axes at once, which is
why each is called out explicitly rather than left to the reader:

* **Auth** is ``Authorization: Token <key>``. Not ``Bearer`` -- Deepgram
  documents no Bearer alternative.
* **Options are query parameters.** There is no JSON options body. Putting
  ``model`` or ``diarize`` in a body is ignored silently.
* **Audio is the raw request body** with an ``audio/*`` content type. Not
  multipart, not base64. (A remote URL is the one case that does use a JSON
  body, and that path is deliberately not wired: the node has already read
  the bytes under workspace containment, and letting a workflow hand
  Deepgram an arbitrary URL would be a server-side request forgery vector.)

Multi-value options are repeated keys (``keyterm=a&keyterm=b``), never comma
lists, which httpx produces for list values.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.logging import get_logger
from .. import _config as speech_config
from .._protocol import (
    SttRequest,
    SttResult,
    TranscriptWord,
)
from ._http import (
    HttpSpeechProvider,
    drop_none,
    query_flags,
)
from .._registry import SttProviderSpec, register_stt_provider

logger = get_logger(__name__)

# Options Deepgram accepts that the neutral request already covers. Anything
# else a caller wants goes through provider_options untouched.
_PASSTHROUGH_OPTIONS = (
    "smart_format",
    "punctuate",
    "paragraphs",
    "utterances",
    "filler_words",
    "numerals",
    "profanity_filter",
    "redact",
    "search",
    "summarize",
    "topics",
    "intents",
    "sentiment",
    "detect_entities",
    "dictation",
    "measurements",
    "multichannel",
    "utt_split",
    "encoding",
    "sample_rate",
    "channels",
    "keywords",
    "keyterm",
    "version",
    "tag",
)


class DeepgramProvider(HttpSpeechProvider):
    provider_name = "deepgram"

    def auth_headers(self) -> Dict[str, str]:
        # "Token", not "Bearer". A Bearer prefix authenticates as nobody and
        # returns 401 with an unhelpful body.
        return {"Authorization": f"Token {self.api_key}"}

    async def transcribe(self, req: SttRequest) -> SttResult:
        model = req.model or speech_config.default_model(self.provider_name, "stt")

        params: Dict[str, Any] = {
            "model": model,
            "smart_format": speech_config.capability(
                self.provider_name, "stt", "default_smart_format", default=True
            ),
        }
        if req.language and req.language != "unknown":
            params["language"] = req.language
        else:
            # Deepgram auto-detects only when explicitly asked to.
            params["detect_language"] = True
        if req.diarize:
            params["diarize"] = True
        if req.translate:
            logger.warning(
                "Deepgram does not translate; transcribing in-language instead",
                provider=self.provider_name,
            )

        params.update(
            {k: v for k, v in req.provider_options.items() if k in _PASSTHROUGH_OPTIONS}
        )

        endpoint = speech_config.endpoint(self.provider_name, "stt") or "/listen"
        response = await self.request(
            "POST",
            endpoint,
            params=query_flags(params),
            content=req.audio,
            headers={"Content-Type": req.mime_type or "application/octet-stream"},
        )
        return self._parse(response.json(), req)

    # ------------------------------------------------------------------
    # response parsing
    # ------------------------------------------------------------------

    def _parse(self, payload: Dict[str, Any], req: SttRequest) -> SttResult:
        results = payload.get("results") or {}
        metadata = payload.get("metadata") or {}
        channel = _first_dict(results.get("channels"))
        alternative = _first_dict(channel.get("alternatives")) if channel else {}

        duration = _as_float(metadata.get("duration"))
        words = [
            TranscriptWord(
                word=str(w.get("punctuated_word") or w.get("word") or ""),
                start=_as_float(w.get("start")),
                end=_as_float(w.get("end")),
                speaker=_as_str(w.get("speaker")),
                confidence=_as_float(w.get("confidence")),
            )
            for w in (alternative.get("words") or [])
            if isinstance(w, dict)
        ]

        # `paragraphs.transcript` carries speaker labels and line breaks that
        # the flat `transcript` string loses, so it wins when present.
        text = str(alternative.get("transcript") or "").strip()
        paragraphs = alternative.get("paragraphs")
        if isinstance(paragraphs, dict):
            formatted = str(paragraphs.get("transcript") or "").strip()
            if formatted:
                text = formatted

        utterances = [u for u in (results.get("utterances") or []) if isinstance(u, dict)]

        return SttResult(
            text=text,
            language=_as_str(channel.get("detected_language")) or (req.language or None),
            language_confidence=_as_float(channel.get("language_confidence")),
            duration_seconds=duration,
            words=words,
            segments=utterances,
            request_id=_as_str(metadata.get("request_id")),
            # Deepgram prices per minute, so the unit is converted here
            # rather than leaving central code to work out what "duration"
            # meant for this vendor.
            billed_units=(duration / 60.0) if duration is not None else None,
            billed_unit="minutes",
        )


def _first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


register_stt_provider(
    SttProviderSpec(
        name="deepgram",
        factory=DeepgramProvider,
        sdk_exception_refs=("httpx:HTTPError",),
        client_kwargs={
            "provider_name": "deepgram",
            "base_url": speech_config.base_url("deepgram"),
        },
    )
)


__all__: List[str] = ["DeepgramProvider"]
