"""Sarvam AI shared helpers.

Five plugins (translate / transliterate / detect_language / speech_to_text /
text_to_speech) hit ``https://api.sarvam.ai`` with the same
``api-subscription-key`` header, which :class:`SarvamCredential` injects
automatically through ``ctx.connection("sarvam")``. There is no service
singleton here — these are stateless REST calls, so per the plugin
cookbook the folder stays helpers + node files.

The credential itself lives with the other LLM credentials in
``nodes/model/_credentials.py`` because the *same* key also authenticates
Sarvam's OpenAI-compatible chat endpoint (``sarvamChatModel``). One stored
key, one catalogue entry, both surfaces.
"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from core.logging import get_logger
from services.plugin import NodeContext, NodeUserError
from services.pricing import get_pricing_service

logger = get_logger(__name__)

SARVAM_BASE_URL = "https://api.sarvam.ai"

# Sarvam's own language-code vocabulary (BCP-47-ish, always ``-IN``).
# Translate accepts the full 22-language set; transliterate and TTS accept
# the 10-language core plus English. Kept as tuples so the node modules can
# splat them into ``Literal[...]`` without drifting apart.
TRANSLATE_LANGUAGES = (
    "as-IN", "bn-IN", "brx-IN", "doi-IN", "en-IN", "gu-IN", "hi-IN",
    "kn-IN", "kok-IN", "ks-IN", "mai-IN", "ml-IN", "mni-IN", "mr-IN",
    "ne-IN", "od-IN", "pa-IN", "sa-IN", "sat-IN", "sd-IN", "ta-IN",
    "te-IN", "ur-IN",
)

CORE_LANGUAGES = (
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN",
    "od-IN", "pa-IN", "ta-IN", "te-IN",
)


async def post_json(
    ctx: NodeContext, path: str, body: Dict[str, Any]
) -> Dict[str, Any]:
    """POST a JSON body to ``api.sarvam.ai{path}`` and return the payload.

    Drops ``None`` values so an unset optional never reaches the API as an
    explicit null (Sarvam rejects several of those with a 422). Non-2xx
    responses become :class:`NodeUserError` carrying Sarvam's own message
    — these are almost always user-correctable (bad language pair, text
    over the per-model character cap, missing key).
    """
    payload = {k: v for k, v in body.items() if v is not None}
    async with ctx.connection("sarvam") as conn:
        response = await conn.post(
            f"{SARVAM_BASE_URL}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        _raise_for_status(response, path)
        return response.json()


async def post_multipart(
    ctx: NodeContext, path: str, *, files: Any, data: Dict[str, Any]
) -> Dict[str, Any]:
    """POST ``multipart/form-data`` to ``api.sarvam.ai{path}``.

    No ``Content-Type`` header — httpx generates the multipart boundary.
    ``files`` must carry raw bytes rather than a file handle so the
    Connection's auth-retry can replay the request intact.
    """
    form = {k: str(v) for k, v in data.items() if v is not None}
    async with ctx.connection("sarvam") as conn:
        response = await conn.post(
            f"{SARVAM_BASE_URL}{path}", files=files, data=form
        )
        _raise_for_status(response, path)
        return response.json()


def _raise_for_status(response: httpx.Response, path: str) -> None:
    if response.status_code < 400:
        return
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
            detail = detail or str(body.get("message") or body.get("detail") or "")
    except Exception:
        detail = response.text[:400]
    raise NodeUserError(
        f"Sarvam {path} failed ({response.status_code})"
        + (f": {detail}" if detail else "")
    )


async def track_sarvam_usage(
    ctx: NodeContext, action: str, resource_count: int
) -> Dict[str, float]:
    """Record a Sarvam API call in ``api_usage_metrics``.

    ``@Operation(cost=...)`` is declarative metadata that nothing reads at
    runtime, so cost attribution is an explicit call — same shape as
    ``nodes/twitter/_base.track_twitter_usage``. ``resource_count`` is the
    billable unit for the operation: characters for the text and speech
    endpoints, seconds of audio for transcription.

    Never raises: a metrics failure must not fail a successful API call.
    """
    try:
        from services.plugin.deps import get_database

        pricing = get_pricing_service()
        cost_data = pricing.calculate_api_cost("sarvam", action, resource_count)
        await get_database().save_api_usage_metric(
            {
                "session_id": ctx.session_id,
                "node_id": ctx.node_id,
                "workflow_id": ctx.workflow_id,
                "service": "sarvam",
                "operation": cost_data.get("operation", action),
                "endpoint": action,
                "resource_count": resource_count,
                "cost": cost_data.get("total_cost", 0.0),
            }
        )
        return cost_data
    except Exception as e:
        logger.warning(
            "sarvam usage tracking failed", action=action, error=str(e)
        )
        return {}
