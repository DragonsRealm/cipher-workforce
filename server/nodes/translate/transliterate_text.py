"""Transliterate — script conversion, not translation.

Replaces the vendor-locked ``sarvamTransliterate``. The distinction the node
exists to preserve: transliteration changes the *writing system* while the
words and language stay the same ("namaste" -> "नमस्ते"), where translation
changes the words ("hello" -> "नमस्ते"). Confusing the two is the single most
common misuse, so the tool description says so explicitly for the LLM's
benefit.

DeepL is absent from the provider list here, and unreachable rather than
merely undocumented: it registers only into the translate registry.
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
from ._protocol import TransliterateRequest
from ._registry import transliterate_providers

from . import _providers  # noqa: F401  isort:skip

_PROVIDERS: List[str] = transliterate_providers()


class TransliterateTextParams(BaseModel):
    tool_name: str = Field(
        default="transliterate_text",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Convert text from one script to another WITHOUT translating it — "
            "the words and language stay the same, only the writing system "
            "changes (e.g. 'namaste' to 'नमस्ते'). Use translate_text instead "
            "when the meaning should change language."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )

    provider: str = Field(
        default=_PROVIDERS[0] if _PROVIDERS else "sarvam",
        description="Transliteration provider.",
        json_schema_extra={"enum": _PROVIDERS},
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Text to convert.",
        json_schema_extra={"rows": 4},
    )
    target_language: str = Field(
        default="",
        description="Language whose script to write in.",
        json_schema_extra={"loadOptionsMethod": "transliterateLanguages"},
    )
    source_language: str = Field(
        default="",
        description="Source language. Blank auto-detects.",
        json_schema_extra={"loadOptionsMethod": "transliterateSourceLanguages"},
    )
    target_script: str = Field(
        default="",
        description="Output script style, where the provider offers a choice.",
        json_schema_extra={"loadOptionsMethod": "transliterateScripts"},
    )
    transliterate_model: str = Field(
        default="",
        description="Provider model. Blank uses the provider default.",
        json_schema_extra={"loadOptionsMethod": "transliterateModels"},
    )
    provider_options: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Vendor-specific options as a JSON object, passed through "
            'untouched — e.g. {"numerals_format": "native"} for Sarvam.'
        ),
        json_schema_extra={"editor": "json", "rows": 4},
    )

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_panel_blanks(cls, values: Any) -> Any:
        return coerce_blank_params(cls, values, object_fields=("provider_options",))


class TransliterateTextOutput(BaseModel):
    transliterated_text: str = ""
    provider: str = ""
    transliterate_model: str = ""
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class TransliterateTextNode(ActionNode):
    type = "transliterateText"
    display_name = "Transliterate"
    subtitle = "Convert Script"
    group = ("language", "tool")
    description = "Convert text between writing systems without translating it"
    component_kind = "square"
    tool_name = "transliterate_text"
    tool_description = (
        "Convert text from one script to another WITHOUT translating it. The "
        "words stay the same; only the writing system changes."
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

    Params = TransliterateTextParams
    Output = TransliterateTextOutput

    @Operation("transliterate")
    async def transliterate(
        self, ctx: NodeContext, params: TransliterateTextParams
    ) -> TransliterateTextOutput:
        provider = require_provider(params.provider, _PROVIDERS, "transliteration")
        model = params.transliterate_model or translate_config.default_model(
            provider, translate_config.TRANSLITERATE
        )
        check_length(provider, translate_config.TRANSLITERATE, model, params.text)

        api_key = await provider_api_key(ctx, provider)
        result = await _unifier.transliterate(
            provider=provider,
            api_key=api_key,
            request=TransliterateRequest(
                text=[params.text],
                target_script=params.target_script,
                source_language=params.source_language,
                target_language=params.target_language,
                model=model,
                provider_options=dict(params.provider_options),
            ),
        )

        await track_usage(
            ctx,
            provider=provider,
            operation="transliterate",
            units=result.billed_units or float(len(params.text)),
            unit=result.billed_unit,
        )

        return TransliterateTextOutput(
            transliterated_text=result.text,
            provider=provider,
            transliterate_model=result.model or model,
            request_id=result.request_id,
        )
