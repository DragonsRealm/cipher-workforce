"""Text to Speech — one node, many providers.

Replaces the vendor-locked ``sarvamTextToSpeech``. The provider is a
parameter rather than a node type, so adding a vendor never adds a node.

Two things about this node are unusual for the codebase and both are
deliberate:

* It declares **several credentials** and picks one at runtime via
  ``ctx.connection(<credential_id>)``. That is why every operation here is
  imperative: the declarative ``routing=`` DSL resolves ``credentials[0]``
  and would silently authenticate every provider with the first key in the
  tuple.
* Its model field is called ``tts_model``, not ``model``. A parameter panel
  effect keyed on the literal names ``model`` and ``api_key`` overwrites
  them with chat-model data whenever a sibling ``provider`` field is
  present, and it does not check that the provider is an LLM provider --
  so a field named ``model`` here would be cleared the moment the user
  picked ElevenLabs.

Audio never leaves this node as bytes. Clips are written into the workflow
workspace and the node returns ``AudioRef`` values, for the reasons in
``services/media/limits.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from services.media import AudioRef, write_audio
from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ..model._credentials import OpenAICredential, SarvamCredential
from . import _config as speech_config
from . import _unifier
from ._base import (
    coerce_blank_params,
    provider_api_key,
    require_provider,
    track_usage,
)
from ._credentials import ElevenLabsCredential
from ._protocol import TtsRequest
from ._registry import tts_providers

# Import for the registration side effect so the provider list below is
# populated before the schema is built.
from . import _providers  # noqa: F401  isort:skip

_PROVIDERS: List[str] = tts_providers()


class TextToSpeechParams(BaseModel):
    tool_name: str = Field(
        default="text_to_speech",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Convert text into spoken audio. Returns a reference to an audio "
            "file saved in the workspace, not the audio itself. Use when the "
            "user asks for speech, narration, a voiceover or an audio version "
            "of some text."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )

    provider: str = Field(
        default=_PROVIDERS[0] if _PROVIDERS else "openai",
        description="Speech provider to synthesize with.",
        json_schema_extra={"enum": _PROVIDERS},
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Text to speak.",
        json_schema_extra={"rows": 4},
    )
    # NOT `model` -- see the module docstring.
    tts_model: str = Field(
        default="",
        description="Provider model id. Leave blank for the provider default.",
        json_schema_extra={"loadOptionsMethod": "speechModels"},
    )
    voice: str = Field(
        default="",
        description="Voice id. Leave blank for the provider default.",
        json_schema_extra={"loadOptionsMethod": "speechVoices"},
    )
    language: str = Field(
        default="",
        description=(
            "Language or locale code. Required by Sarvam (e.g. hi-IN); "
            "auto-detected by the others."
        ),
        json_schema_extra={"loadOptionsMethod": "speechLanguages"},
    )
    speed: Optional[float] = Field(
        default=None,
        description=(
            "Playback rate. Supported ranges differ per provider and values "
            "outside them are clamped."
        ),
    )
    output_format: str = Field(
        default="",
        description="Audio format. Leave blank for the provider default.",
        json_schema_extra={"loadOptionsMethod": "speechFormats"},
    )
    sample_rate: Optional[int] = Field(
        default=None,
        description="Output sample rate in Hz.",
        # Only Sarvam exposes the choice; elsewhere the rate is fixed by
        # `output_format`, so an inert box would imply a control that does
        # not exist.
        json_schema_extra={
            "loadOptionsMethod": "speechSampleRates",
            "displayOptions": {"show": {"provider": ["sarvam"]}},
        },
    )
    provider_options: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Vendor-specific options as a JSON object, passed through "
            'untouched — e.g. {"stability": 0.6} for ElevenLabs, '
            '{"pitch": 0.2} for Sarvam, {"instructions": "..."} for OpenAI.'
        ),
        json_schema_extra={"editor": "json", "rows": 4},
    )

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_panel_blanks(cls, values: Any) -> Any:
        # The panel stores "" for any cleared field, which is a hard type
        # error against `speed`, `sample_rate` and `provider_options`.
        return coerce_blank_params(cls, values, object_fields=("provider_options",))


class TextToSpeechOutput(BaseModel):
    audio: Optional[AudioRef] = Field(
        default=None, description="The generated clip; the first when several."
    )
    files: List[AudioRef] = Field(
        default_factory=list,
        description="Every generated clip. More than one when the provider split long input.",
    )
    chunk_count: int = 0
    provider: str = ""
    tts_model: str = ""
    voice: str = ""
    request_id: Optional[str] = None
    note: Optional[str] = None

    model_config = {"extra": "allow"}


class TextToSpeechNode(ActionNode):
    type = "textToSpeech"
    display_name = "Text to Speech"
    subtitle = "Synthesize Speech"
    group = ("language", "tool")
    description = "Convert text to spoken audio using any configured speech provider"
    component_kind = "square"
    tool_name = "text_to_speech"
    tool_description = (
        "Convert text into spoken audio. Returns a reference to an audio file "
        "saved in the workspace. Pick the provider and voice that suit the "
        "language and tone requested."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    # First multi-credential node in the repo. ``ctx.connection(id)`` is a
    # lookup over this tuple, so every provider's key is reachable and the
    # selected one decides which is used.
    credentials = (OpenAICredential, ElevenLabsCredential, SarvamCredential)
    annotations = {"destructive": False, "readonly": False, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    # `usable_as_tool` otherwise auto-hides both handles, which would break
    # chaining this into Speech to Text on the canvas.
    hide_input_handle = False
    hide_output_handle = False
    ui_hints = {"outputMode": "audio"}

    Params = TextToSpeechParams
    Output = TextToSpeechOutput

    @Operation("synthesize")
    async def synthesize(
        self, ctx: NodeContext, params: TextToSpeechParams
    ) -> TextToSpeechOutput:
        provider = require_provider(params.provider, _PROVIDERS, "text-to-speech")
        model = params.tts_model or speech_config.default_model(provider, "tts")

        self._check_length(provider, model, params.text)

        api_key = await provider_api_key(ctx, provider)
        result = await _unifier.synthesize(
            provider=provider,
            api_key=api_key,
            request=TtsRequest(
                text=params.text,
                model=model,
                voice=params.voice,
                language=params.language,
                speed=params.speed,
                output_format=params.output_format,
                sample_rate=params.sample_rate,
                provider_options=dict(params.provider_options),
            ),
        )

        refs = [
            write_audio(
                payload.data,
                ctx=ctx,
                stem=params.text[:32],
                ext=payload.format or "wav",
                mime_type=payload.mime_type,
                sample_rate=payload.sample_rate,
                channels=payload.channels or 1,
            )
            for payload in result.audio
        ]

        await track_usage(
            ctx,
            provider=provider,
            operation="text_to_speech",
            units=result.billed_units,
            unit=result.billed_unit,
        )

        note = None
        if len(refs) > 1:
            # Each clip carries its own container header, so concatenating
            # the bytes produces a file that plays only the first chunk.
            note = (
                f"{provider} split this text into {len(refs)} clips. They are "
                "separate playable files, not parts of one stream."
            )

        return TextToSpeechOutput(
            audio=refs[0] if refs else None,
            files=refs,
            chunk_count=len(refs),
            provider=provider,
            tts_model=result.model or model,
            voice=result.voice or params.voice,
            request_id=result.request_id,
            note=note,
        )

    @staticmethod
    def _check_length(provider: str, model: str, text: str) -> None:
        """Refuse over-long input before spending a paid call on it."""
        cap = speech_config.max_input_chars(provider, model)
        if cap and len(text) > cap:
            raise NodeUserError(
                f"Text is {len(text)} characters; {provider}/{model} accepts at "
                f"most {cap}. Shorten it or split it across several runs."
            )
