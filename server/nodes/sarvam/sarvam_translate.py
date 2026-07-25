"""Sarvam Translate — POST /translate (Mayura / Sarvam-Translate)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import post_json, track_sarvam_usage
from ..model._credentials import SarvamCredential

# Per-model input caps from the Sarvam docs. Enforced client-side so the
# LLM gets an actionable message instead of an opaque 422.
_MAX_INPUT = {"mayura:v1": 1000, "sarvam-translate:v1": 2000}


class SarvamTranslateParams(BaseModel):
    tool_name: str = Field(
        default="sarvam_translate",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Translate text between English and 22 Indian languages using Sarvam AI. "
            "Supports formal, colloquial and code-mixed registers."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )
    input: str = Field(
        ...,
        min_length=1,
        description="Text to translate.",
        json_schema_extra={"rows": 4},
    )
    source_language_code: Literal[
        "auto", "as-IN", "bn-IN", "brx-IN", "doi-IN", "en-IN", "gu-IN", "hi-IN",
        "kn-IN", "kok-IN", "ks-IN", "mai-IN", "ml-IN", "mni-IN", "mr-IN",
        "ne-IN", "od-IN", "pa-IN", "sa-IN", "sat-IN", "sd-IN", "ta-IN",
        "te-IN", "ur-IN",
    ] = Field(default="auto", description="Source language, or 'auto' to detect.")
    target_language_code: Literal[
        "as-IN", "bn-IN", "brx-IN", "doi-IN", "en-IN", "gu-IN", "hi-IN",
        "kn-IN", "kok-IN", "ks-IN", "mai-IN", "ml-IN", "mni-IN", "mr-IN",
        "ne-IN", "od-IN", "pa-IN", "sa-IN", "sat-IN", "sd-IN", "ta-IN",
        "te-IN", "ur-IN",
    ] = Field(default="hi-IN", description="Language to translate into.")
    model: Literal["mayura:v1", "sarvam-translate:v1"] = Field(
        default="sarvam-translate:v1",
        description="sarvam-translate:v1 covers 22 languages and 2000 chars; mayura:v1 covers 10 and 1000.",
    )
    mode: Literal["formal", "modern-colloquial", "classic-colloquial", "code-mixed"] = Field(
        default="formal", description="Register of the translation."
    )
    speaker_gender: Optional[Literal["Male", "Female"]] = Field(
        default=None,
        description="Speaker gender, for languages that inflect on it.",
    )
    output_script: Optional[Literal["roman", "fully-native", "spoken-form-in-native"]] = Field(
        default=None,
        description="Transliteration applied to the output. Leave unset for the language's native script.",
    )
    numerals_format: Literal["international", "native"] = Field(
        default="international", description="Render digits as 0-9 or in the native script."
    )

    model_config = {"extra": "ignore"}


class SarvamTranslateOutput(BaseModel):
    translated_text: str = ""
    source_language_code: str = ""
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class SarvamTranslateNode(ActionNode):
    type = "sarvamTranslate"
    display_name = "Sarvam Translate"
    subtitle = "Indic Translation"
    group = ("language", "tool")
    description = "Translate text between English and 22 Indian languages via Sarvam AI"
    component_kind = "square"
    tool_name = "sarvam_translate"
    tool_description = (
        "Translate text between English and 22 Indian languages using Sarvam AI. "
        "Set source_language_code to 'auto' when the input language is unknown. "
        "Returns the translated text and the detected source language."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (SarvamCredential,)
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True

    Params = SarvamTranslateParams
    Output = SarvamTranslateOutput

    @Operation("translate", cost={"service": "sarvam", "action": "translate", "count": 1})
    async def translate(
        self, ctx: NodeContext, params: SarvamTranslateParams
    ) -> SarvamTranslateOutput:
        cap = _MAX_INPUT[params.model]
        if len(params.input) > cap:
            raise NodeUserError(
                f"input is {len(params.input)} characters; {params.model} accepts at most "
                f"{cap}. Split the text or switch to sarvam-translate:v1 (2000)."
            )
        if params.source_language_code == params.target_language_code:
            raise NodeUserError(
                "source_language_code and target_language_code are identical "
                f"({params.target_language_code}) — nothing to translate."
            )

        data = await post_json(
            ctx,
            "/translate",
            {
                "input": params.input,
                "source_language_code": params.source_language_code,
                "target_language_code": params.target_language_code,
                "model": params.model,
                "mode": params.mode,
                "speaker_gender": params.speaker_gender,
                "output_script": params.output_script,
                "numerals_format": params.numerals_format,
            },
        )

        await track_sarvam_usage(ctx, "translate", len(params.input))
        return SarvamTranslateOutput(
            translated_text=data.get("translated_text", ""),
            source_language_code=data.get("source_language_code", ""),
            request_id=data.get("request_id"),
        )
