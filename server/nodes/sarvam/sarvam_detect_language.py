"""Sarvam Detect Language — POST /text-lid (language + script ID)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import post_json, track_sarvam_usage
from ..model._credentials import SarvamCredential

_MAX_INPUT = 1000


class SarvamDetectLanguageParams(BaseModel):
    tool_name: str = Field(
        default="sarvam_detect_language",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Identify which Indian language a piece of text is written in, and in which "
            "script. Returns BCP-47 language and ISO 15924 script codes."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )
    input: str = Field(
        ...,
        min_length=1,
        description="Text whose language should be identified (max 1000 characters).",
        json_schema_extra={"rows": 4},
    )

    model_config = {"extra": "ignore"}


class SarvamDetectLanguageOutput(BaseModel):
    # Sarvam returns nulls when it cannot classify the input, so both stay
    # Optional rather than defaulting to a misleading empty string.
    language_code: Optional[str] = None
    script_code: Optional[str] = None
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class SarvamDetectLanguageNode(ActionNode):
    type = "sarvamDetectLanguage"
    display_name = "Sarvam Detect Language"
    subtitle = "Language ID"
    group = ("language", "tool")
    description = "Identify the language and script of Indic or English text"
    component_kind = "square"
    tool_name = "sarvam_detect_language"
    tool_description = (
        "Identify which Indian language a piece of text is written in, and in which script. "
        "Returns a BCP-47 language_code (e.g. hi-IN) and an ISO 15924 script_code (e.g. Deva). "
        "Both may be null when the text is too short or ambiguous to classify. Useful before "
        "calling sarvam_translate with an explicit source language."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (SarvamCredential,)
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True

    Params = SarvamDetectLanguageParams
    Output = SarvamDetectLanguageOutput

    @Operation("detect", cost={"service": "sarvam", "action": "detect_language", "count": 1})
    async def detect(
        self, ctx: NodeContext, params: SarvamDetectLanguageParams
    ) -> SarvamDetectLanguageOutput:
        if len(params.input) > _MAX_INPUT:
            raise NodeUserError(
                f"input is {len(params.input)} characters; /text-lid accepts at most "
                f"{_MAX_INPUT}. Truncate or sample the text before detecting."
            )

        data = await post_json(ctx, "/text-lid", {"input": params.input})

        await track_sarvam_usage(ctx, "detect_language", len(params.input))
        return SarvamDetectLanguageOutput(
            language_code=data.get("language_code"),
            script_code=data.get("script_code"),
            request_id=data.get("request_id"),
        )
