"""Sarvam AI — all three capabilities over dedicated REST endpoints.

Ported from the retired ``sarvamTranslate`` / ``sarvamTransliterate`` /
``sarvamDetectLanguage`` nodes, preserving their wire behaviour. One provider
module serving three capabilities is the norm rather than the exception for
this kind of vendor, which is why the three registries take the same class.

Two carried-over details:

* Explicit nulls are rejected where an absent key is fine, so every body goes
  through ``drop_none``.
* Sarvam accepts one string per call, not an array. The neutral request is a
  list because DeepL's is; here only the first entry is sent, and the node
  keeps input single-valued.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logging import get_logger

from .. import _config as translate_config
from .._protocol import (
    DetectRequest,
    DetectResult,
    DetectedLanguage,
    LanguageOption,
    TranslateRequest,
    TranslateResult,
    TranslatedText,
    TransliterateRequest,
    TransliterateResult,
)
from .._registry import (
    DetectProviderSpec,
    TranslateProviderSpec,
    TransliterateProviderSpec,
    register_detect_provider,
    register_translate_provider,
    register_transliterate_provider,
)
from ._http import HttpTranslateProvider, drop_none

logger = get_logger(__name__)


def _first(text: List[str]) -> str:
    return text[0] if text else ""


class SarvamTextProvider(HttpTranslateProvider):
    provider_name = "sarvam"

    def auth_headers(self) -> Dict[str, str]:
        # Sarvam's non-chat endpoints accept only the native header; Bearer
        # works on its OpenAI-compatible chat route, which is a different
        # surface.
        return {"api-subscription-key": self.api_key}

    async def translate(self, req: TranslateRequest) -> TranslateResult:
        model = req.model or translate_config.default_model(self.provider_name, "translate")
        body: Dict[str, Any] = {
            "input": _first(req.text),
            "source_language_code": req.source_language or "auto",
            "target_language_code": req.target_language
            or translate_config.default_target_language(self.provider_name, "translate"),
            "model": model,
            "mode": req.formality or "formal",
        }
        body.update(req.provider_options)

        endpoint = translate_config.endpoint(self.provider_name, "translate") or "/translate"
        payload = (await self.request("POST", endpoint, json_body=drop_none(body))).json()

        return TranslateResult(
            translations=[
                TranslatedText(
                    text=str(payload.get("translated_text") or ""),
                    detected_source_language=payload.get("source_language_code"),
                )
            ],
            model=model,
            request_id=payload.get("request_id"),
            billed_units=float(len(_first(req.text))),
            billed_unit="characters",
        )

    async def transliterate(self, req: TransliterateRequest) -> TransliterateResult:
        model = req.model or translate_config.default_model(
            self.provider_name, "transliterate"
        )
        body: Dict[str, Any] = {
            "input": _first(req.text),
            "source_language_code": req.source_language or "auto",
            "target_language_code": req.target_language
            or translate_config.default_target_language(self.provider_name, "transliterate"),
            "model": model,
            "spoken_form": req.target_script == "spoken-form-in-native" or None,
        }
        body.update(req.provider_options)

        endpoint = (
            translate_config.endpoint(self.provider_name, "transliterate")
            or "/transliterate"
        )
        payload = (await self.request("POST", endpoint, json_body=drop_none(body))).json()

        return TransliterateResult(
            results=[str(payload.get("transliterated_text") or "")],
            model=model,
            request_id=payload.get("request_id"),
            billed_units=float(len(_first(req.text))),
            billed_unit="characters",
        )

    async def detect(self, req: DetectRequest) -> DetectResult:
        endpoint = translate_config.endpoint(self.provider_name, "detect") or "/text-lid"
        payload = (
            await self.request("POST", endpoint, json_body={"input": _first(req.text)})
        ).json()

        return DetectResult(
            detections=[
                DetectedLanguage(
                    language=str(payload.get("language_code") or ""),
                    script=payload.get("script_code"),
                )
            ],
            request_id=payload.get("request_id"),
            billed_units=float(len(_first(req.text))),
            billed_unit="characters",
        )

    async def languages(self, *, target: bool = True) -> List[LanguageOption]:
        return [
            LanguageOption(code=code)
            for code in translate_config.languages(self.provider_name, "translate")
        ]


_KW = {"provider_name": "sarvam"}
_REFS = ("httpx:HTTPError",)

register_translate_provider(
    TranslateProviderSpec(
        name="sarvam", factory=SarvamTextProvider, sdk_exception_refs=_REFS, client_kwargs=_KW
    )
)
register_transliterate_provider(
    TransliterateProviderSpec(
        name="sarvam", factory=SarvamTextProvider, sdk_exception_refs=_REFS, client_kwargs=_KW
    )
)
register_detect_provider(
    DetectProviderSpec(
        name="sarvam", factory=SarvamTextProvider, sdk_exception_refs=_REFS, client_kwargs=_KW
    )
)
