"""OpenAI's speech wire format, and everyone who speaks it.

One class serves OpenAI (both directions) and Groq (transcription only).
Groq hosts the identical route at ``/openai/v1/audio/transcriptions``, so the
difference between them is a base URL and a model list -- both of which live
in ``speech_defaults.json``. Adding another OpenAI-compatible transcription
host is a JSON block plus one tuple entry at the bottom of this file, with no
new Python.

That Groq's instance also carries a ``synthesize`` method it cannot serve is
harmless and is the point of the two-registry design: Groq is never
registered for synthesis, so nothing can select it for synthesis, and no
``supports_tts`` flag has to be kept honest.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.logging import get_logger
from .. import _config as speech_config
from .._protocol import (
    AudioPayload,
    SttRequest,
    SttResult,
    TranscriptWord,
    TtsRequest,
    TtsResult,
    Voice,
)
from ._http import HttpSpeechProvider, drop_none
from .._registry import (
    SttProviderSpec,
    TtsProviderSpec,
    register_stt_provider,
    register_tts_provider,
)

logger = get_logger(__name__)

# Container -> mime. ``pcm`` is deliberately headerless: OpenAI documents it
# as raw 24 kHz 16-bit mono, so it gets the sample rate attached on the
# payload instead of a container mime type that would imply a header.
_MIME_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/L16",
}

_PCM_SAMPLE_RATE = 24000


class OpenAISpeechProvider(HttpSpeechProvider):
    """Speech over OpenAI's ``/audio/*`` routes."""

    provider_name = "openai"

    # ------------------------------------------------------------------
    # synthesis
    # ------------------------------------------------------------------

    async def synthesize(self, req: TtsRequest) -> TtsResult:
        model = req.model or speech_config.default_model(self.provider_name, "tts")
        fmt = req.output_format or speech_config.capability(
            self.provider_name, "tts", "default_output_format", default="mp3"
        )

        body: Dict[str, Any] = {
            "input": req.text,
            "model": model,
            "voice": req.voice
            or speech_config.default_voice(self.provider_name)
            or "alloy",
            "response_format": fmt,
            "speed": req.speed,
        }
        # `instructions` is silently ignored on tts-1 / tts-1-hd rather than
        # rejected, so it is filtered by capability instead of being sent
        # hopefully.
        instructions = req.provider_options.get("instructions")
        if instructions and speech_config.supports(
            self.provider_name, "tts", "supports_instructions", model=model
        ):
            body["instructions"] = instructions

        body.update(
            {
                k: v
                for k, v in req.provider_options.items()
                if k not in {"instructions"}
            }
        )

        endpoint = speech_config.endpoint(self.provider_name, "tts") or "/audio/speech"
        response = await self.request("POST", endpoint, json_body=drop_none(body))

        return TtsResult(
            audio=[
                AudioPayload(
                    data=response.content,
                    format=fmt,
                    mime_type=_MIME_BY_FORMAT.get(fmt, "application/octet-stream"),
                    sample_rate=_PCM_SAMPLE_RATE if fmt == "pcm" else None,
                    channels=1 if fmt == "pcm" else None,
                )
            ],
            model=model,
            voice=str(body.get("voice") or ""),
            request_id=response.headers.get("x-request-id"),
            billed_units=float(len(req.text)),
            billed_unit="characters",
        )

    async def list_voices(self) -> List[Voice]:
        """Static catalogue -- OpenAI publishes no voices endpoint.

        Model-scoped because ``tts-1`` supports only nine of the thirteen
        voices; passing the model in via ``provider_options`` is not possible
        here, so the widest set is returned and the node validates.
        """
        return [
            Voice(id=voice, name=voice.title())
            for voice in speech_config.voices(self.provider_name)
        ]

    # ------------------------------------------------------------------
    # transcription
    # ------------------------------------------------------------------

    async def transcribe(self, req: SttRequest) -> SttResult:
        model = req.model or speech_config.default_model(self.provider_name, "stt")
        response_format = self._response_format(req, model)

        translate = req.translate and speech_config.supports(
            self.provider_name, "stt", "supports_translate", model=model
        )
        if req.translate and not translate:
            logger.warning(
                "model cannot translate; transcribing in-language instead",
                provider=self.provider_name,
                model=model,
            )

        endpoint_key = "translate_endpoint" if translate else "endpoint"
        endpoint = (
            speech_config.endpoint(self.provider_name, "stt", endpoint_key)
            or "/audio/transcriptions"
        )

        form: Dict[str, Any] = {
            "model": model,
            "response_format": response_format,
        }
        # The translation route always outputs English, so a language hint
        # is meaningless there and rejected by some hosts.
        if not translate and req.language and req.language != "unknown":
            form["language"] = req.language
        if req.prompt and speech_config.supports(
            self.provider_name, "stt", "supports_prompt", model=model
        ):
            form["prompt"] = req.prompt
        form.update(req.provider_options)

        files = {"file": (req.filename, req.audio, req.mime_type)}
        if req.timestamps and response_format == "verbose_json":
            # Form-encoded arrays need the bracket suffix; httpx expands a
            # list value into repeated fields.
            form["timestamp_granularities[]"] = ["word", "segment"]

        response = await self.request(
            "POST", endpoint, data=drop_none(form), files=files
        )

        if response_format == "text":
            # Plain-text mode carries no metadata at all -- no duration, so
            # no billable unit either. The node falls back to probing the
            # audio it already has on disk.
            return SttResult(text=response.text.strip(), billed_unit="seconds")
        return self._parse_json_transcript(response.json())

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _response_format(self, req: SttRequest, model: str) -> str:
        """Pick a response format this model actually accepts.

        OpenAI gates the allowed set per model and returns 400 on a
        violation, so an unsupported request is downgraded here rather than
        failing the workflow. Word timestamps need ``verbose_json``, which
        in practice means whisper-1.
        """
        allowed = speech_config.response_formats(self.provider_name, model)
        requested = req.provider_options.get("response_format") or ""

        if req.timestamps and not requested:
            requested = "verbose_json"
        if not requested:
            requested = speech_config.capability(
                self.provider_name,
                "stt",
                "default_response_format",
                model=model,
                default="json",
            )

        if allowed and requested not in allowed:
            fallback = "json" if "json" in allowed else allowed[0]
            logger.warning(
                "response_format not supported by this model; downgrading",
                provider=self.provider_name,
                model=model,
                requested=requested,
                using=fallback,
            )
            return fallback
        return requested

    def _parse_json_transcript(self, payload: Dict[str, Any]) -> SttResult:
        words = [
            TranscriptWord(
                word=str(w.get("word") or ""),
                start=_as_float(w.get("start")),
                end=_as_float(w.get("end")),
                speaker=_as_str(w.get("speaker")),
            )
            for w in payload.get("words") or []
            if isinstance(w, dict)
        ]
        duration = _as_float(payload.get("duration"))
        return SttResult(
            text=str(payload.get("text") or "").strip(),
            language=_as_str(payload.get("language")),
            duration_seconds=duration,
            words=words,
            segments=[s for s in payload.get("segments") or [] if isinstance(s, dict)],
            request_id=_as_str((payload.get("x_groq") or {}).get("id"))
            if isinstance(payload.get("x_groq"), dict)
            else None,
            billed_units=self._billed_seconds(duration),
            billed_unit="seconds",
        )

    def _billed_seconds(self, duration: float | None) -> float | None:
        """Apply a provider's minimum billed duration, if it declares one."""
        if duration is None:
            return None
        floor = speech_config.capability(
            self.provider_name, "stt", "min_billed_seconds", default=None
        )
        if isinstance(floor, (int, float)):
            return max(float(duration), float(floor))
        return float(duration)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


# ---------------------------------------------------------------------------
# Plugin self-registration
# ---------------------------------------------------------------------------
#
# ``httpx:HTTPError`` is the common ancestor of connect, timeout and
# status errors, so one lazy ref covers every transport failure. It stays a
# ``"module:Class"`` string for the same reason the LLM specs do -- see
# services/provider_registry.py.

_COMPAT_STT_PROVIDERS: Tuple[str, ...] = ("openai", "groq")


def _register() -> None:
    register_tts_provider(
        TtsProviderSpec(
            name="openai",
            factory=OpenAISpeechProvider,
            sdk_exception_refs=("httpx:HTTPError",),
            client_kwargs={
                "provider_name": "openai",
                "base_url": speech_config.base_url("openai"),
            },
        )
    )

    for name in _COMPAT_STT_PROVIDERS:
        base = speech_config.base_url(name)
        if not base:
            logger.warning(
                "skipping compat speech provider -- no base_url in speech_defaults.json",
                provider=name,
            )
            continue
        register_stt_provider(
            SttProviderSpec(
                name=name,
                factory=OpenAISpeechProvider,
                sdk_exception_refs=("httpx:HTTPError",),
                client_kwargs={"provider_name": name, "base_url": base},
            )
        )


_register()
