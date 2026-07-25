"""Detect Language — identify what a piece of text is written in.

Replaces the vendor-locked ``sarvamDetectLanguage``. Returns the language and,
where the provider reports it, the script and a confidence.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from services.plugin import (
    ActionNode,
    NodeContext,
    Operation,
    TaskQueue,
    coerce_blank_params,
)

from ..model._credentials import OpenAICredential, SarvamCredential
from . import _config as translate_config
from . import _unifier
from ._base import check_length, provider_api_key, require_provider, track_usage
from ._protocol import DetectRequest
from ._registry import detect_providers

from . import _providers  # noqa: F401  isort:skip

_PROVIDERS: List[str] = detect_providers()


class DetectLanguageParams(BaseModel):
    tool_name: str = Field(
        default="detect_language",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Identify which language a piece of text is written in, and in "
            "which script. Use before translating when the source language is "
            "unknown, or to route text by language."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )

    provider: str = Field(
        default=_PROVIDERS[0] if _PROVIDERS else "sarvam",
        description="Language-identification provider.",
        json_schema_extra={"enum": _PROVIDERS},
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Text whose language should be identified.",
        json_schema_extra={"rows": 4},
    )
    detect_model: str = Field(
        default="",
        description="Provider model. Blank uses the provider default.",
        json_schema_extra={"loadOptionsMethod": "detectModels"},
    )
    provider_options: Dict[str, Any] = Field(
        default_factory=dict,
        description="Vendor-specific options as a JSON object, passed through untouched.",
        json_schema_extra={"editor": "json", "rows": 3},
    )

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_panel_blanks(cls, values: Any) -> Any:
        return coerce_blank_params(cls, values, object_fields=("provider_options",))


class DetectLanguageOutput(BaseModel):
    language: str = ""
    script: Optional[str] = None
    confidence: Optional[float] = None
    provider: str = ""
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class DetectLanguageNode(ActionNode):
    type = "detectLanguage"
    display_name = "Detect Language"
    subtitle = "Language ID"
    group = ("language", "tool")
    description = "Identify the language and script of a piece of text"
    component_kind = "square"
    tool_name = "detect_language"
    tool_description = (
        "Identify which language a piece of text is written in, and in which "
        "script."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (SarvamCredential, OpenAICredential)
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    hide_input_handle = False
    hide_output_handle = False

    Params = DetectLanguageParams
    Output = DetectLanguageOutput

    @Operation("detect")
    async def detect(
        self, ctx: NodeContext, params: DetectLanguageParams
    ) -> DetectLanguageOutput:
        provider = require_provider(params.provider, _PROVIDERS, "language detection")
        model = params.detect_model or translate_config.default_model(
            provider, translate_config.DETECT
        )
        check_length(provider, translate_config.DETECT, model, params.text)

        api_key = await provider_api_key(ctx, provider)
        result = await _unifier.detect(
            provider=provider,
            api_key=api_key,
            request=DetectRequest(
                text=[params.text],
                model=model,
                provider_options=dict(params.provider_options),
            ),
        )

        await track_usage(
            ctx,
            provider=provider,
            operation="detect_language",
            units=result.billed_units or float(len(params.text)),
            unit=result.billed_unit,
        )

        first = result.detections[0] if result.detections else None
        return DetectLanguageOutput(
            language=first.language if first else "",
            script=first.script if first else None,
            confidence=first.confidence if first else None,
            provider=provider,
            request_id=result.request_id,
        )
