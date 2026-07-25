"""Sarvam Transliterate — POST /transliterate (script conversion)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import post_json, track_sarvam_usage
from ..model._credentials import SarvamCredential


class SarvamTransliterateParams(BaseModel):
    tool_name: str = Field(
        default="sarvam_transliterate",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Convert text between scripts while preserving pronunciation — e.g. romanised "
            "Hindi to Devanagari. This is NOT translation; meaning is unchanged."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )
    input: str = Field(
        ...,
        min_length=1,
        description="Text to transliterate.",
        json_schema_extra={"rows": 4},
    )
    source_language_code: Literal[
        "auto", "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN",
        "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
    ] = Field(default="auto", description="Source language, or 'auto' to detect.")
    target_language_code: Literal[
        "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN",
        "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
    ] = Field(default="hi-IN", description="Script to transliterate into.")
    numerals_format: Literal["international", "native"] = Field(
        default="international", description="Render digits as 0-9 or in the native script."
    )
    spoken_form: bool = Field(
        default=False,
        description="Expand numbers, dates and abbreviations into how they are spoken.",
    )
    spoken_form_numerals_language: Literal["english", "native"] = Field(
        default="native",
        description="Language used when spelling out numerals in spoken form.",
        json_schema_extra={"displayOptions": {"show": {"spoken_form": [True]}}},
    )

    model_config = {"extra": "ignore"}


class SarvamTransliterateOutput(BaseModel):
    transliterated_text: str = ""
    source_language_code: str = ""
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class SarvamTransliterateNode(ActionNode):
    type = "sarvamTransliterate"
    display_name = "Sarvam Transliterate"
    subtitle = "Script Conversion"
    group = ("language", "tool")
    description = "Convert text between Indic scripts and Roman while preserving pronunciation"
    component_kind = "square"
    tool_name = "sarvam_transliterate"
    tool_description = (
        "Convert text between scripts while preserving pronunciation — e.g. romanised Hindi "
        "('namaste') to Devanagari, or the reverse. This is NOT translation: the words and "
        "meaning stay the same, only the script changes. Use sarvam_translate to change language."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (SarvamCredential,)
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True

    Params = SarvamTransliterateParams
    Output = SarvamTransliterateOutput

    @Operation("transliterate", cost={"service": "sarvam", "action": "transliterate", "count": 1})
    async def transliterate(
        self, ctx: NodeContext, params: SarvamTransliterateParams
    ) -> SarvamTransliterateOutput:
        if params.source_language_code == params.target_language_code:
            raise NodeUserError(
                "source_language_code and target_language_code are identical "
                f"({params.target_language_code}) — nothing to transliterate."
            )

        data = await post_json(
            ctx,
            "/transliterate",
            {
                "input": params.input,
                "source_language_code": params.source_language_code,
                "target_language_code": params.target_language_code,
                "numerals_format": params.numerals_format,
                "spoken_form": params.spoken_form,
                "spoken_form_numerals_language": (
                    params.spoken_form_numerals_language if params.spoken_form else None
                ),
            },
        )

        await track_sarvam_usage(ctx, "transliterate", len(params.input))
        return SarvamTransliterateOutput(
            transliterated_text=data.get("transliterated_text", ""),
            source_language_code=data.get("source_language_code", ""),
            request_id=data.get("request_id"),
        )
