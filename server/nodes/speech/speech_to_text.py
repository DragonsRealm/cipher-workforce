"""Speech to Text — one node, many providers.

Replaces the vendor-locked ``sarvamSpeechToText``, and closes two of its
defects in the process:

* **Path traversal.** The old node joined a user-supplied path onto the
  workspace root with no check, so ``audio_file="../../credentials.db"``
  read the encrypted credential store and uploaded it to the provider.
  Every input shape here routes through ``services.media``, which resolves
  under containment.
* **Billing.** The old node charged every transcription as 30 seconds
  because it never measured the clip. This one probes the real duration and
  bills that, or bills nothing when the duration genuinely cannot be
  determined.

The model field is ``stt_model``, not ``model`` -- see the sibling module's
docstring for why that naming is load-bearing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from services.plugin import ActionNode, NodeContext, Operation, TaskQueue

from ..model._credentials import GroqCredential, OpenAICredential, SarvamCredential
from . import _config as speech_config
from . import _unifier
from ._base import (
    coerce_blank_params,
    measure_seconds,
    provider_api_key,
    read_audio_input,
    require_provider,
    track_usage,
)
from ._credentials import DeepgramCredential
from ._protocol import SttRequest
from ._registry import stt_providers

from . import _providers  # noqa: F401  isort:skip

_PROVIDERS: List[str] = stt_providers()


class SpeechToTextParams(BaseModel):
    tool_name: str = Field(
        default="speech_to_text",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Transcribe an audio file to text. Accepts a workspace path or an "
            "audio reference produced by an upstream node. Use when the user "
            "asks what was said in a recording, or wants a transcript."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )

    provider: str = Field(
        default=_PROVIDERS[0] if _PROVIDERS else "openai",
        description="Speech provider to transcribe with.",
        json_schema_extra={"enum": _PROVIDERS},
    )
    # Three shapes reach this field: an AudioRef from an upstream node or the
    # upload route, a bare workspace path, or the legacy base64 envelope the
    # file widget used to emit. ``read_audio_input`` handles all three.
    audio_file: Any = Field(
        default="",
        description="Audio file to transcribe — upload one, or point at a workspace path.",
        json_schema_extra={
            "widget": "file",
            "accept": "audio/*,.wav,.mp3,.m4a,.ogg,.opus,.flac,.aac,.webm,.amr",
        },
    )
    stt_model: str = Field(
        default="",
        description="Provider model id. Leave blank for the provider default.",
        json_schema_extra={"loadOptionsMethod": "speechModels"},
    )
    language: str = Field(
        default="",
        description="Language hint (e.g. en, hi-IN). Leave blank to auto-detect.",
        json_schema_extra={"loadOptionsMethod": "speechLanguages"},
    )
    translate: bool = Field(
        default=False,
        description="Translate to English instead of transcribing in-language.",
    )
    diarize: bool = Field(
        default=False,
        description="Label speakers, where the provider and model support it.",
    )
    timestamps: bool = Field(
        default=False,
        description="Return per-word timing, where the provider and model support it.",
    )
    provider_options: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Vendor-specific options as a JSON object, passed through "
            'untouched — e.g. {"keyterm": ["invoice"]} for Deepgram, '
            '{"mode": "codemix"} for Sarvam, {"response_format": "text"} for OpenAI.'
        ),
        json_schema_extra={"editor": "json", "rows": 4},
    )

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_panel_blanks(cls, values: Any) -> Any:
        # The panel stores "" for any cleared field, which is a hard type
        # error against the three booleans and `provider_options`.
        return coerce_blank_params(cls, values, object_fields=("provider_options",))


class SpeechToTextOutput(BaseModel):
    transcript: str = ""
    language: Optional[str] = None
    language_confidence: Optional[float] = None
    duration_seconds: Optional[float] = None
    words: List[Dict[str, Any]] = Field(default_factory=list)
    segments: List[Dict[str, Any]] = Field(default_factory=list)
    provider: str = ""
    stt_model: str = ""
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class SpeechToTextNode(ActionNode):
    type = "speechToText"
    display_name = "Speech to Text"
    subtitle = "Transcribe Audio"
    group = ("language", "tool")
    description = "Transcribe audio to text using any configured speech provider"
    component_kind = "square"
    tool_name = "speech_to_text"
    tool_description = (
        "Transcribe an audio file to text. Accepts a workspace path or an audio "
        "reference from an upstream node."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (
        OpenAICredential,
        GroqCredential,
        DeepgramCredential,
        SarvamCredential,
    )
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    hide_input_handle = False
    hide_output_handle = False

    Params = SpeechToTextParams
    Output = SpeechToTextOutput

    @Operation("transcribe")
    async def transcribe(
        self, ctx: NodeContext, params: SpeechToTextParams
    ) -> SpeechToTextOutput:
        provider = require_provider(params.provider, _PROVIDERS, "speech-to-text")
        model = params.stt_model or speech_config.default_model(provider, "stt")

        max_bytes = speech_config.capability(
            provider, "stt", "max_upload_bytes", default=None
        )
        filename, blob, path = read_audio_input(
            params.audio_file, ctx, max_bytes=max_bytes
        )

        api_key = await provider_api_key(ctx, provider)
        result = await _unifier.transcribe(
            provider=provider,
            api_key=api_key,
            request=SttRequest(
                audio=blob,
                filename=filename,
                mime_type=_guess_mime(filename),
                model=model,
                language=params.language,
                translate=params.translate,
                diarize=params.diarize,
                timestamps=params.timestamps,
                provider_options=dict(params.provider_options),
            ),
        )

        # Prefer what the provider reported; fall back to probing the file we
        # already have. Either way the figure is measured, never assumed.
        duration = result.duration_seconds
        if duration is None:
            duration = measure_seconds(path, declared_format=filename.rsplit(".", 1)[-1])

        await track_usage(
            ctx,
            provider=provider,
            operation="speech_to_text",
            units=_billable(result.billed_units, result.billed_unit, duration),
            unit=result.billed_unit or "seconds",
        )

        return SpeechToTextOutput(
            transcript=result.text,
            language=result.language,
            language_confidence=result.language_confidence,
            duration_seconds=duration,
            words=[
                {
                    "word": w.word,
                    "start": w.start,
                    "end": w.end,
                    "speaker": w.speaker,
                    "confidence": w.confidence,
                }
                for w in result.words
            ],
            segments=result.segments,
            provider=provider,
            stt_model=model,
            request_id=result.request_id,
        )


def _billable(
    reported: Optional[float], unit: str, duration: Optional[float]
) -> Optional[float]:
    """Resolve the billable quantity in the provider's own unit.

    A provider that reported its own figure wins. Otherwise the measured
    duration is converted into that provider's unit. When nothing could be
    measured this returns ``None`` and no metric is written -- an
    under-count is honest, a fabricated 30 seconds was not.
    """
    if reported is not None:
        return reported
    if duration is None:
        return None
    return duration / 60.0 if unit == "minutes" else duration


def _guess_mime(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
