"""Dropdown loaders for the three text-language nodes.

Each reads ``provider`` out of the parameter dict it is handed rather than
declaring ``loadOptionsDependsOn`` — that attribute is lifted by the client
adapter but has no usages anywhere in the codebase, so it is untested
machinery. The parameter dict is how every existing loader works and re-fires
when parameters change.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from core.logging import get_logger

from . import _config as translate_config
from ._registry import CAPABILITIES

logger = get_logger(__name__)

# Which capability a node is asking about, inferred from the model field name
# it declares. Each node has a distinct one, which doubles as the signal.
_MODEL_FIELD_TO_CAPABILITY = {
    "translate_model": translate_config.TRANSLATE,
    "transliterate_model": translate_config.TRANSLITERATE,
    "detect_model": translate_config.DETECT,
}

_NODE_TYPE_TO_CAPABILITY = {
    "translateText": translate_config.TRANSLATE,
    "transliterateText": translate_config.TRANSLITERATE,
    "detectLanguage": translate_config.DETECT,
}


def _capability_for(params: Dict[str, Any]) -> str:
    node_type = str(params.get("node_type") or "")
    if node_type in _NODE_TYPE_TO_CAPABILITY:
        return _NODE_TYPE_TO_CAPABILITY[node_type]
    for field, capability in _MODEL_FIELD_TO_CAPABILITY.items():
        if field in params:
            return capability
    return translate_config.TRANSLATE


def _selected_provider(params: Dict[str, Any], capability: str) -> str:
    provider = str(params.get("provider") or "")
    available = CAPABILITIES[capability][0]()
    if provider in available:
        return provider
    return available[0] if available else ""


def _for_capability(
    capability: str, build: Callable[[str, str, Dict[str, Any]], List[Dict[str, Any]]]
):
    """Bind a builder to one capability, for the per-node loader names."""

    async def loader(params: Dict[str, Any]) -> List[Dict[str, Any]]:
        provider = _selected_provider(params, capability)
        if not provider:
            return []
        return build(provider, capability, params)

    return loader


def _models(provider: str, capability: str, params: Dict[str, Any]):
    default = translate_config.default_model(provider, capability)
    options: List[Dict[str, Any]] = []
    for model in translate_config.models(provider, capability):
        option: Dict[str, Any] = {"value": model, "label": model}
        if model == default:
            option["description"] = "Provider default"
        options.append(option)
    return options


def _languages(provider: str, capability: str, params: Dict[str, Any]):
    model = str(
        params.get("translate_model")
        or params.get("transliterate_model")
        or ""
    )
    return [
        {"value": code, "label": code}
        for code in translate_config.languages(
            provider, capability, model=model or None
        )
    ]


def _source_languages(provider: str, capability: str, params: Dict[str, Any]):
    options = [
        {"value": "", "label": "Auto-detect", "description": "Let the provider decide"}
    ]
    options.extend(_languages(provider, capability, params))
    return options


def _scripts(provider: str, capability: str, params: Dict[str, Any]):
    return [
        {"value": script, "label": script.replace("-", " ")}
        for script in translate_config.listing(provider, capability, "scripts")
    ]


def _formality(provider: str, capability: str, params: Dict[str, Any]):
    """Register / formality, which every vendor names differently.

    DeepL calls the values formality options; Sarvam calls the same idea
    ``mode``. Both are read from their own config key so the node keeps one
    field.
    """
    values = translate_config.listing(
        provider, capability, "formality_options"
    ) or translate_config.listing(provider, capability, "modes")
    if not values:
        return []
    return [{"value": "", "label": "Provider default"}] + [
        {"value": value, "label": value.replace("-", " ")} for value in values
    ]


# One registered loader per (capability, field). Names must match the
# `loadOptionsMethod` strings on the Params fields.
LOADERS = {
    "translateModels": _for_capability(translate_config.TRANSLATE, _models),
    "translateLanguages": _for_capability(translate_config.TRANSLATE, _languages),
    "translateSourceLanguages": _for_capability(
        translate_config.TRANSLATE, _source_languages
    ),
    "translateFormality": _for_capability(translate_config.TRANSLATE, _formality),
    "transliterateModels": _for_capability(translate_config.TRANSLITERATE, _models),
    "transliterateLanguages": _for_capability(
        translate_config.TRANSLITERATE, _languages
    ),
    "transliterateSourceLanguages": _for_capability(
        translate_config.TRANSLITERATE, _source_languages
    ),
    "transliterateScripts": _for_capability(translate_config.TRANSLITERATE, _scripts),
    "detectModels": _for_capability(translate_config.DETECT, _models),
}


__all__ = ["LOADERS"]
