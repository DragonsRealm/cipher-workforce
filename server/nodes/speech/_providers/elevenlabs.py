"""ElevenLabs text-to-speech.

The most structurally divergent provider in the v1 set, which makes it the
useful shape test for the abstraction. Three things differ from everyone
else:

* the voice id is a **path segment**, not a body field;
* ``output_format`` is a **query parameter** -- put it in the JSON body and
  the API returns the default format with no error at all;
* auth is a bare ``xi-api-key`` header with no scheme keyword.

``output_format`` also encodes container, sample rate and (for mp3) bitrate
in one underscore-delimited string, so the container and sample rate that
end up on the ``AudioRef`` are parsed back out of it rather than guessed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.logging import get_logger
from .. import _config as speech_config
from .._protocol import (
    AudioPayload,
    TtsRequest,
    TtsResult,
    Voice,
)
from ._http import HttpSpeechProvider, drop_none
from .._registry import TtsProviderSpec, register_tts_provider

logger = get_logger(__name__)

_MIME_BY_CONTAINER = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/L16",
    "opus": "audio/opus",
    "ulaw": "audio/basic",
    "alaw": "audio/basic",
}

# Fields inside `voice_settings`. Sent only when the caller supplies them --
# ElevenLabs' own defaults are better than anything guessed here, and
# whether these are honoured on eleven_v3 is undocumented, so nothing is
# sent speculatively.
_VOICE_SETTING_KEYS = (
    "stability",
    "similarity_boost",
    "style",
    "use_speaker_boost",
    "speed",
)

# Everything ElevenLabs takes as query string rather than body.
_QUERY_KEYS = ("enable_logging", "optimize_streaming_latency")

_VOICES_PAGE_SIZE = 100


class ElevenLabsProvider(HttpSpeechProvider):
    provider_name = "elevenlabs"

    def auth_headers(self) -> Dict[str, str]:
        # Bare key, no scheme keyword. `Authorization: Bearer <key>` is
        # rejected.
        return {"xi-api-key": self.api_key}

    async def synthesize(self, req: TtsRequest) -> TtsResult:
        voice_id = req.voice or speech_config.default_voice(self.provider_name)
        if not voice_id:
            from services.plugin import NodeUserError

            raise NodeUserError(
                "ElevenLabs requires a voice. Pick one from the Voice dropdown "
                "-- there is no account-wide default."
            )

        model = req.model or speech_config.default_model(self.provider_name, "tts")
        output_format = req.output_format or speech_config.capability(
            self.provider_name, "tts", "default_output_format", default="mp3_44100_128"
        )

        options = dict(req.provider_options)
        voice_settings = self._voice_settings(req, options)

        body: Dict[str, Any] = {
            "text": req.text,
            "model_id": model,
            "language_code": req.language or None,
            "voice_settings": voice_settings or None,
        }
        # Whatever is left in options after the query keys and voice
        # settings have been claimed is a body field (seed, previous_text,
        # apply_text_normalization, ...).
        params = {k: options.pop(k) for k in _QUERY_KEYS if k in options}
        body.update(options)

        params["output_format"] = output_format

        path = f"/v1/text-to-speech/{voice_id}"
        response = await self.request(
            "POST", path, json_body=drop_none(body), params=drop_none(params)
        )

        container, sample_rate = _parse_output_format(output_format)
        return TtsResult(
            audio=[
                AudioPayload(
                    data=response.content,
                    format=container,
                    mime_type=_MIME_BY_CONTAINER.get(
                        container, "application/octet-stream"
                    ),
                    sample_rate=sample_rate,
                    channels=1,
                )
            ],
            model=model,
            voice=voice_id,
            request_id=response.headers.get("request-id"),
            billed_units=float(len(req.text)),
            billed_unit="characters",
        )

    async def list_voices(self) -> List[Voice]:
        """Live catalogue via the v2 endpoint.

        Cursor-paginated; the docs are explicit that ``has_more`` plus
        ``next_page_token`` is the supported way to walk it and that
        ``total_count`` should not be relied on. Pages are capped so a large
        enterprise account cannot stall a dropdown load indefinitely.
        """
        endpoint = (
            speech_config.capability(
                self.provider_name, "tts", "voices_endpoint", default="/v2/voices"
            )
            or "/v2/voices"
        )

        voices: List[Voice] = []
        page_token: Optional[str] = None
        for _ in range(10):
            params = drop_none(
                {"page_size": _VOICES_PAGE_SIZE, "next_page_token": page_token}
            )
            payload = (await self.request("GET", endpoint, params=params)).json()
            for entry in payload.get("voices") or []:
                if not isinstance(entry, dict):
                    continue
                voice_id = str(entry.get("voice_id") or "")
                if not voice_id:
                    continue
                voices.append(
                    Voice(
                        id=voice_id,
                        name=str(entry.get("name") or voice_id),
                        description=str(entry.get("description") or "")[:160],
                        preview_url=str(entry.get("preview_url") or ""),
                    )
                )
            if not payload.get("has_more"):
                break
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        else:
            logger.warning(
                "stopped paginating ElevenLabs voices at the page cap",
                provider=self.provider_name,
                collected=len(voices),
            )
        return voices

    def _voice_settings(
        self, req: TtsRequest, options: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Collect voice_settings, clamping speed to the documented API range.

        ElevenLabs publishes two different speed ranges -- the OpenAPI schema
        allows 0.5-2.0 while the best-practices guide recommends 0.7-1.2.
        Neither page acknowledges the other. The schema bound is enforced
        because that is what the API actually accepts; the narrower range is
        left as guidance for the node's help text.
        """
        settings = {
            key: options.pop(key) for key in _VOICE_SETTING_KEYS if key in options
        }
        if req.speed is not None:
            settings["speed"] = req.speed

        speed = settings.get("speed")
        if speed is not None:
            bounds = speech_config.speed_range(self.provider_name) or (0.5, 2.0)
            clamped = max(bounds[0], min(float(speed), bounds[1]))
            if clamped != float(speed):
                logger.warning(
                    "clamped ElevenLabs speed to the supported range",
                    requested=speed,
                    using=clamped,
                )
            settings["speed"] = clamped
        return drop_none(settings)


def _parse_output_format(output_format: str) -> tuple[str, Optional[int]]:
    """Split ``mp3_44100_128`` into ``("mp3", 44100)``.

    The container drives the file extension and mime type on the resulting
    ``AudioRef``; the sample rate matters because ``pcm_*`` is headerless and
    nothing downstream could otherwise work out its rate.
    """
    parts = str(output_format or "").split("_")
    container = parts[0] if parts else ""
    sample_rate: Optional[int] = None
    if len(parts) > 1:
        try:
            sample_rate = int(parts[1])
        except ValueError:
            sample_rate = None
    return container, sample_rate


register_tts_provider(
    TtsProviderSpec(
        name="elevenlabs",
        factory=ElevenLabsProvider,
        sdk_exception_refs=("httpx:HTTPError",),
        client_kwargs={
            "provider_name": "elevenlabs",
            "base_url": speech_config.base_url("elevenlabs"),
        },
    )
)
