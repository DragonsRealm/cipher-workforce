"""DeepL — translation only.

The single-capability provider in this set, and therefore the one that proves
the three-registry design: it registers into ``translate`` and is structurally
unreachable from the transliterate and detect nodes. With one registry plus
capability flags it would have appeared in all three dropdowns and failed at
runtime in two of them.

Two wire details worth knowing:

* Auth is ``Authorization: DeepL-Auth-Key <key>``. Not Bearer.
* ``text`` is an **array**, and the response is a parallel array — so the
  batch shape is native here rather than something to emulate.

DeepL also reports ``billed_characters`` per translation, which is better than
counting input ourselves: it reflects what the invoice will actually say.
"""

from __future__ import annotations

from typing import Dict, List

from core.logging import get_logger

from .. import _config as translate_config
from .._protocol import (
    LanguageOption,
    TranslateRequest,
    TranslateResult,
    TranslatedText,
)
from .._registry import TranslateProviderSpec, register_translate_provider
from ._http import HttpTranslateProvider, drop_none

logger = get_logger(__name__)


class DeepLProvider(HttpTranslateProvider):
    provider_name = "deepl"

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"DeepL-Auth-Key {self.api_key}"}

    async def translate(self, req: TranslateRequest) -> TranslateResult:
        body = {
            "text": req.text,
            "target_lang": (req.target_language or "EN-US").upper(),
            "source_lang": req.source_language.upper() or None,
            "context": req.context or None,
            "model_type": req.model or None,
            "formality": req.formality or None,
            # DeepL wants "0"/"1" here rather than a JSON boolean.
            "preserve_formatting": "1" if req.preserve_formatting else None,
        }
        body.update(req.provider_options)

        endpoint = (
            translate_config.endpoint(self.provider_name, "translate") or "/translate"
        )
        payload = (
            await self.request("POST", endpoint, json_body=drop_none(body))
        ).json()

        entries = payload.get("translations") or []
        return TranslateResult(
            translations=[
                TranslatedText(
                    text=str(entry.get("text") or ""),
                    detected_source_language=entry.get("detected_source_language"),
                )
                for entry in entries
                if isinstance(entry, dict)
            ],
            model=req.model or translate_config.default_model(self.provider_name, "translate"),
            # The provider's own figure beats counting input characters.
            billed_units=float(
                sum(int(e.get("billed_characters") or 0) for e in entries if isinstance(e, dict))
            )
            or None,
            billed_unit="characters",
        )

    async def languages(self, *, target: bool = True) -> List[LanguageOption]:
        """Live catalogue — DeepL adds languages without notice.

        Falls back to the configured list rather than leaving the dropdown
        empty when the call fails; a stale list still lets the user proceed.
        """
        endpoint = (
            translate_config.capability(
                self.provider_name, "translate", "languages_endpoint", default="/languages"
            )
            or "/languages"
        )
        try:
            payload = (
                await self.request(
                    "GET", endpoint, params={"type": "target" if target else "source"}
                )
            ).json()
            options = [
                LanguageOption(
                    code=str(entry.get("language") or ""),
                    name=str(entry.get("name") or entry.get("language") or ""),
                )
                for entry in payload or []
                if isinstance(entry, dict) and entry.get("language")
            ]
            if options:
                return options
        except Exception as exc:
            logger.warning(
                "DeepL language listing failed; using the configured list",
                error=str(exc),
            )
        return [
            LanguageOption(code=code)
            for code in translate_config.languages(self.provider_name, "translate")
        ]


register_translate_provider(
    TranslateProviderSpec(
        name="deepl",
        factory=DeepLProvider,
        sdk_exception_refs=("httpx:HTTPError",),
        client_kwargs={"provider_name": "deepl"},
    )
)
