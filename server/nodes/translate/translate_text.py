"""Translate — one node, many providers.

Replaces the vendor-locked ``sarvamTranslate``. The provider is a parameter,
so switching from Sarvam to DeepL never means swapping the node out of a
workflow.

The field is ``translate_model``, not ``model``: a parameter-panel effect
keyed on the literal names ``model`` and ``api_key`` overwrites them with
chat-model data whenever a sibling ``provider`` field exists, and it does not
check that the provider is an LLM provider.
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
from ._credentials import DeepLCredential
from ._protocol import TranslateRequest
from ._registry import translate_providers

from . import _providers  # noqa: F401  isort:skip

_PROVIDERS: List[str] = translate_providers()


class TranslateTextParams(BaseModel):
    tool_name: str = Field(
        default="translate_text",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Translate text into another language. Use when the user asks for "
            "a translation, or when text must be understood in a different "
            "language before further processing."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )

    provider: str = Field(
        default=_PROVIDERS[0] if _PROVIDERS else "openai",
        description="Translation provider.",
        json_schema_extra={"enum": _PROVIDERS},
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Text to translate.",
        json_schema_extra={"rows": 4},
    )
    target_language: str = Field(
        default="",
        description="Language to translate into. Blank uses the provider default.",
        json_schema_extra={"loadOptionsMethod": "translateLanguages"},
    )
    source_language: str = Field(
        default="",
        description="Source language. Blank auto-detects.",
        json_schema_extra={"loadOptionsMethod": "translateSourceLanguages"},
    )
    # NOT `model` — see the module docstring.
    translate_model: str = Field(
        default="",
        description="Provider model. Blank uses the provider default.",
        json_schema_extra={"loadOptionsMethod": "translateModels"},
    )
    formality: str = Field(
        default="",
        description="Register or formality, where the provider supports it.",
        json_schema_extra={"loadOptionsMethod": "translateFormality"},
    )
    context: str = Field(
        default="",
        description=(
            "Extra context to disambiguate the text. Not translated itself."
        ),
        json_schema_extra={"rows": 2},
    )
    preserve_formatting: bool = Field(
        default=False,
        description="Keep original line breaks and punctuation as-is.",
    )
    provider_options: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Vendor-specific options as a JSON object, passed through "
            'untouched — e.g. {"glossary_id": "..."} for DeepL, '
            '{"numerals_format": "native"} for Sarvam.'
        ),
        json_schema_extra={"editor": "json", "rows": 4},
    )

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_panel_blanks(cls, values: Any) -> Any:
        return coerce_blank_params(cls, values, object_fields=("provider_options",))


class TranslateTextOutput(BaseModel):
    translated_text: str = ""
    detected_source_language: Optional[str] = None
    provider: str = ""
    translate_model: str = ""
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class TranslateTextNode(ActionNode):
    type = "translateText"
    display_name = "Translate"
    subtitle = "Translate Text"
    group = ("language", "tool")
    description = "Translate text using any configured translation provider"
    component_kind = "square"
    tool_name = "translate_text"
    tool_description = (
        "Translate text into another language. Pick the provider that best "
        "covers the language pair."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (DeepLCredential, SarvamCredential, OpenAICredential)
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    # `usable_as_tool` otherwise auto-hides both handles.
    hide_input_handle = False
    hide_output_handle = False

    Params = TranslateTextParams
    Output = TranslateTextOutput

    @Operation("translate")
    async def translate(
        self, ctx: NodeContext, params: TranslateTextParams
    ) -> TranslateTextOutput:
        provider = require_provider(params.provider, _PROVIDERS, "translation")
        model = params.translate_model or translate_config.default_model(
            provider, translate_config.TRANSLATE
        )
        check_length(provider, translate_config.TRANSLATE, model, params.text)

        api_key = await provider_api_key(ctx, provider)
        result = await _unifier.translate(
            provider=provider,
            api_key=api_key,
            request=TranslateRequest(
                text=[params.text],
                target_language=params.target_language
                or translate_config.default_target_language(
                    provider, translate_config.TRANSLATE
                ),
                source_language=params.source_language,
                model=model,
                formality=params.formality,
                context=params.context,
                preserve_formatting=params.preserve_formatting,
                provider_options=dict(params.provider_options),
            ),
        )

        await track_usage(
            ctx,
            provider=provider,
            operation="translate",
            units=result.billed_units or float(len(params.text)),
            unit=result.billed_unit,
        )

        first = result.translations[0] if result.translations else None
        return TranslateTextOutput(
            translated_text=first.text if first else "",
            detected_source_language=first.detected_source_language if first else None,
            provider=provider,
            translate_model=result.model or model,
            request_id=result.request_id,
        )
