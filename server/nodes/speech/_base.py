"""Shared helpers for the two speech nodes.

Credential resolution, audio input handling, and cost attribution -- the
parts both nodes need and neither should own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple, Union, get_args, get_origin

from core.logging import get_logger
from services.media import coerce_file_param, read_media_bytes, resolve_media
from services.media.inspect import inspect_audio
from services.plugin import NodeContext, NodeUserError

from . import _config as speech_config

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Parameter coercion
# ---------------------------------------------------------------------------
#
# The parameter panel stores "" for a field the user has cleared or never
# filled, whatever the declared type. For a `str` field that is harmless, but
# `""` against `Optional[float]`, `bool` or `Dict[str, Any]` is a hard
# validation error -- "Input should be a valid dictionary" being the one that
# surfaced first.
#
# It is not enough to fix the dict field: `speed`, `sample_rate`, `translate`,
# `diarize` and `timestamps` all fail the same way. So the rule is general --
# a blank string against a field that cannot hold a string means "unset", and
# is dropped so the field's own default applies.
#
# Same class of problem the repo already solves per-node with
# `AndroidServiceParams._coerce_parameters` and `WriteTodosParams._coerce_todos`;
# this is the reusable form.


def _accepts_str(annotation: Any) -> bool:
    """Whether a blank string is a legitimate value for this annotation.

    `Dict[str, Any]` is the case worth spelling out: its `get_args` are
    `(str, Any)`, so a naive "is str among the args" check would wrongly
    conclude a dict field accepts a string. Container origins are rejected
    before the args are consulted.
    """
    if annotation is Any or annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is Union:
        return any(_accepts_str(arg) for arg in get_args(annotation))
    if origin is not None:
        return False
    return False


def coerce_blank_params(cls: Any, values: Any, *, object_fields: Sequence[str] = ()) -> Any:
    """Normalize panel-supplied blanks before Pydantic validates them.

    Fields named in ``object_fields`` additionally accept a JSON **object**
    string, because the panel has no object widget and renders them as a text
    input -- so a user who types `{"instructions": "cheerful"}` gets what they
    plainly meant rather than a type error.
    """
    if not isinstance(values, dict):
        return values

    cleaned: Dict[str, Any] = {}
    for key, value in values.items():
        if key in object_fields:
            cleaned[key] = _coerce_object(key, value)
            continue

        field = cls.model_fields.get(key)
        blank = isinstance(value, str) and not value.strip()
        if blank and field is not None and not _accepts_str(field.annotation):
            continue  # drop -> the field default applies
        cleaned[key] = value
    return cleaned


def _coerce_object(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(
                f"{key} must be a JSON object, e.g. "
                '{"instructions": "speak cheerfully"}. Could not parse it: '
                f"{exc}."
            ) from exc
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(
            f"{key} must be a JSON object, not a {type(parsed).__name__}."
        )
    raise ValueError(f"{key} must be a JSON object, not a {type(value).__name__}.")


async def provider_api_key(ctx: NodeContext, provider: str) -> str:
    """Resolve the stored key for ``provider`` through its credential.

    Goes through ``ctx.connection(...)`` rather than reading the auth
    service directly so a missing key raises the framework's annotated
    ``PermissionError`` -- which ``BaseNode.execute`` turns into a
    credential envelope plus a CloudEvents broadcast, so the Credentials
    modal lights up the right provider.
    """
    credential_id = speech_config.credential_id(provider)
    async with ctx.connection(credential_id) as conn:
        secrets = await conn.credentials()
    api_key = str(secrets.get("api_key") or "")
    if not api_key:
        raise NodeUserError(
            f"No API key stored for '{credential_id}'. Add one in the "
            "Credentials modal."
        )
    return api_key


def require_provider(provider: str, available: Sequence[str], direction: str) -> str:
    """Validate a provider selection against what is actually registered."""
    if provider in available:
        return provider
    raise NodeUserError(
        f"'{provider}' is not a {direction} provider. Available: "
        f"{', '.join(available)}."
    )


def read_audio_input(
    value: Any, ctx: NodeContext, *, max_bytes: Optional[int] = None
) -> Tuple[str, bytes, Optional[Path]]:
    """Resolve an audio parameter to ``(filename, bytes, path_or_None)``.

    The path comes back when one exists because it is what makes real
    duration billing possible -- ``inspect_audio`` needs a file. A legacy
    base64 upload has no path, and rather than inventing a duration for it
    the node bills nothing. That is the fix for the old Sarvam node, which
    charged every clip as 30 seconds because it never measured.

    All three input shapes route through ``services.media``, so the
    traversal that let ``audio_file="../../credentials.db"`` read the
    credential store is closed here by construction.
    """
    kwargs: Dict[str, Any] = {"ctx": ctx}
    if max_bytes:
        kwargs["max_bytes"] = max_bytes

    path: Optional[Path] = None
    if isinstance(value, str) and value.strip():
        # A path or workspace-relative string: resolve it so we keep the
        # location for probing, then read through the contained reader.
        path = resolve_media(value, ctx=ctx)
        filename, blob = read_media_bytes(value, **kwargs)
    elif isinstance(value, dict) and value.get("kind") == "audio":
        from services.media import AudioRef

        ref = AudioRef.model_validate(value)
        path = resolve_media(ref, ctx=ctx)
        filename, blob = read_media_bytes(ref, **kwargs)
    else:
        filename, blob = coerce_file_param(value, **kwargs)

    return filename, blob, path


def measure_seconds(path: Optional[Path], declared_format: str = "") -> Optional[float]:
    """Real duration for billing, or ``None`` when it cannot be measured.

    Never raises and never guesses: ``inspect_audio`` degrades to an empty
    probe on an unknown container, and a missing duration means the caller
    skips per-second attribution rather than inventing a figure.
    """
    if path is None:
        return None
    return inspect_audio(path, declared_format=declared_format).duration_seconds


async def track_usage(
    ctx: NodeContext,
    *,
    provider: str,
    operation: str,
    units: Optional[float],
    unit: str,
) -> None:
    """Record an API usage metric. Never raises.

    ``service`` is the provider id rather than a generic ``"speech"`` so
    per-provider dashboards group correctly, and the unit is whatever that
    provider actually bills in -- characters, seconds or minutes. Both come
    from the provider module, because only it knows.

    A provider with no ``operation_map`` entry in ``pricing.json`` yields a
    zero cost silently, which is why every provider added here needs both
    an ``api_pricing`` and an ``operation_map`` block.
    """
    if units is None or units <= 0:
        return
    try:
        from services.plugin.deps import get_database
        from services.pricing import get_pricing_service

        cost_data = get_pricing_service().calculate_api_cost(
            provider, operation, units
        )
        await get_database().save_api_usage_metric(
            {
                "session_id": ctx.raw.get("session_id", "default"),
                "node_id": ctx.node_id,
                "workflow_id": ctx.workflow_id,
                "service": provider,
                "operation": cost_data.get("operation", operation),
                "endpoint": operation,
                "resource_count": units,
                "cost": cost_data.get("total_cost", 0.0),
            }
        )
    except Exception as exc:
        # Cost attribution must never fail a workflow that already did the
        # paid work.
        logger.warning(
            "failed to record speech usage",
            provider=provider,
            operation=operation,
            error=str(exc),
        )


__all__ = [
    "coerce_blank_params",
    "measure_seconds",
    "provider_api_key",
    "read_audio_input",
    "require_provider",
    "track_usage",
]
