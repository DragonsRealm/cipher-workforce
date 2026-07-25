"""Sarvam AI speech, in both directions. Indic-first.

Ported from the two vendor-locked nodes this layer replaces
(``sarvamTextToSpeech`` / ``sarvamSpeechToText``), preserving their wire
behaviour exactly. Two properties of this API shape the code:

* **Synthesis returns base64 inside JSON**, not raw bytes, and the ``audios``
  key is an **array**. Long input comes back as several standalone clips.
  They are not byte-concatenatable -- each carries its own container header
  -- so the array is surfaced as several payloads and the node writes one
  ``AudioRef`` per clip. Flattening them into one file would produce audio
  that plays only its first chunk.
* **Explicit nulls are rejected.** Sending ``"pitch": null`` is a 422 where
  omitting the key is fine, so every body goes through ``drop_none``.

Parameter support also splits hard by model: ``bulbul:v3`` takes
``temperature`` and rejects ``pitch`` / ``loudness`` / ``enable_preprocessing``
outright, while ``bulbul:v2`` is the mirror image. That gating lives here
because it is wire-format knowledge; the user-facing validation (character
caps, speaker membership) lives in the node where ``NodeUserError`` reads
naturally.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from .. import _config as speech_config
from .._protocol import (
    AudioPayload,
    SpeechError,
    SpeechErrorCategory,
    SttRequest,
    SttResult,
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

# Codec name -> file extension. Three of these differ from the codec string,
# which matters because the extension ends up in the workspace filename.
_CODEC_EXT = {"linear16": "pcm", "mulaw": "ulaw", "alaw": "alaw"}

_MIME_BY_CODEC = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "opus": "audio/opus",
    "linear16": "audio/L16",
    "mulaw": "audio/basic",
    "alaw": "audio/basic",
}

# Body fields only bulbul:v2 accepts. v3 rejects them rather than ignoring.
_V2_ONLY = ("pitch", "loudness", "enable_preprocessing")
# Body fields only bulbul:v3 accepts.
_V3_ONLY = ("temperature",)


class SarvamSpeechProvider(HttpSpeechProvider):
    provider_name = "sarvam"

    def auth_headers(self) -> Dict[str, str]:
        # Sarvam's speech routes accept only the native header. (Bearer works
        # on their OpenAI-compatible chat route, which is a different
        # surface and not this one.)
        return {"api-subscription-key": self.api_key}

    # ------------------------------------------------------------------
    # synthesis
    # ------------------------------------------------------------------

    async def synthesize(self, req: TtsRequest) -> TtsResult:
        model = req.model or speech_config.default_model(self.provider_name, "tts")
        is_v3 = model.endswith("v3")
        codec = req.output_format or speech_config.capability(
            self.provider_name, "tts", "default_output_format", default="wav"
        )

        body: Dict[str, Any] = {
            "text": req.text,
            "target_language_code": req.language
            or speech_config.capability(
                self.provider_name, "tts", "default_language", default="hi-IN"
            ),
            "model": model,
            "speaker": req.voice or speech_config.default_voice(self.provider_name),
            "pace": req.speed,
            "speech_sample_rate": req.sample_rate
            or speech_config.capability(
                self.provider_name, "tts", "default_sample_rate", default=24000
            ),
            "output_audio_codec": codec,
        }

        # Whatever this model rejects is dropped rather than forwarded; the
        # opposite model's exclusive fields would 400 the whole request.
        # Fields it accepts but the caller omitted simply stay absent, and
        # Sarvam's own defaults apply.
        rejected = _V2_ONLY if is_v3 else _V3_ONLY
        for key, value in req.provider_options.items():
            if key in rejected:
                logger.debug(
                    "dropping parameter the selected Sarvam model rejects",
                    model=model,
                    parameter=key,
                )
                continue
            body[key] = value

        endpoint = (
            speech_config.endpoint(self.provider_name, "tts") or "/text-to-speech"
        )
        payload = (
            await self.request("POST", endpoint, json_body=drop_none(body))
        ).json()

        chunks = payload.get("audios") or []
        if not chunks:
            raise SpeechError(
                message="Sarvam returned no audio for this request.",
                provider=self.provider_name,
                category=SpeechErrorCategory.SERVER,
            )

        ext = _CODEC_EXT.get(codec, codec)
        mime = _MIME_BY_CODEC.get(codec, "application/octet-stream")
        sample_rate = body.get("speech_sample_rate")

        audio = [
            AudioPayload(
                data=_decode(chunk, index),
                format=ext,
                mime_type=mime,
                sample_rate=int(sample_rate) if sample_rate else None,
                channels=1,
            )
            for index, chunk in enumerate(chunks)
        ]

        return TtsResult(
            audio=audio,
            model=model,
            voice=str(body.get("speaker") or ""),
            request_id=payload.get("request_id"),
            billed_units=float(len(req.text)),
            billed_unit="characters",
        )

    async def list_voices(self) -> List[Voice]:
        """Static per-model catalogue -- Sarvam publishes no voices endpoint.

        The v2 and v3 speaker sets are disjoint, so this returns the default
        (v3) set and the node re-reads config for the selected model. Sending
        a v2 speaker to v3 is a 400.
        """
        return [
            Voice(id=speaker, name=speaker.title())
            for speaker in speech_config.voices(self.provider_name)
        ]

    # ------------------------------------------------------------------
    # transcription
    # ------------------------------------------------------------------

    async def transcribe(self, req: SttRequest) -> SttResult:
        model = req.model or speech_config.default_model(self.provider_name, "stt")
        mode = req.provider_options.get("mode") or (
            "translate" if req.translate else "transcribe"
        )

        endpoint_key = (
            "translate_endpoint"
            if req.translate and mode == "translate"
            else "endpoint"
        )
        endpoint = (
            speech_config.endpoint(self.provider_name, "stt", endpoint_key)
            or "/speech-to-text"
        )

        form = drop_none(
            {
                "model": model,
                "language_code": req.language or "unknown",
                "mode": mode,
                "input_audio_codec": req.provider_options.get("input_audio_codec"),
            }
        )
        # Bytes rather than a handle: Connection-style auth retry replays the
        # same kwargs, and a consumed file object would replay as empty.
        files = {"file": (req.filename, req.audio, req.mime_type)}

        payload = (
            await self.request(
                "POST",
                endpoint,
                data={k: str(v) for k, v in form.items()},
                files=files,
            )
        ).json()

        return SttResult(
            text=str(payload.get("transcript") or "").strip(),
            language=payload.get("language_code"),
            language_confidence=_as_float(payload.get("language_probability")),
            # The synchronous endpoint never returns timestamps or
            # diarization -- those exist only on the batch job API -- so no
            # duration comes back either. The node fills billed_units from
            # the audio it already probed, which is what replaced the flat
            # 30-second charge the old node applied to every clip.
            duration_seconds=None,
            request_id=payload.get("request_id"),
            billed_units=None,
            billed_unit="seconds",
        )


def _decode(chunk: Any, index: int) -> bytes:
    try:
        return base64.b64decode(str(chunk), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SpeechError(
            message=f"Sarvam audio chunk {index + 1} was not valid base64: {exc}",
            provider="sarvam",
            category=SpeechErrorCategory.SERVER,
        ) from exc


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_CLIENT_KWARGS = {
    "provider_name": "sarvam",
    "base_url": speech_config.base_url("sarvam"),
}

register_tts_provider(
    TtsProviderSpec(
        name="sarvam",
        factory=SarvamSpeechProvider,
        sdk_exception_refs=("httpx:HTTPError",),
        client_kwargs=_CLIENT_KWARGS,
    )
)

register_stt_provider(
    SttProviderSpec(
        name="sarvam",
        factory=SarvamSpeechProvider,
        sdk_exception_refs=("httpx:HTTPError",),
        client_kwargs=_CLIENT_KWARGS,
    )
)
