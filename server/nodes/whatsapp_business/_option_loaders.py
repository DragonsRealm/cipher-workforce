"""``loadOptionsMethod`` loaders for the WhatsApp Business plugin.

Registered from ``__init__.py``. These run outside node execution -- the
parameter panel calls them directly -- so they build their own HTTP request
rather than going through ``ctx.connection``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx

from core.logging import get_logger

from ._base import GRAPH_API_VERSION, GRAPH_BASE_URL

logger = get_logger(__name__)

# Approved is the only status that can actually be sent. The others are shown
# with a marker rather than hidden, because "my template is missing from the
# dropdown" is a worse debugging experience than seeing it listed as pending.
_SENDABLE = "APPROVED"


async def load_templates(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Message templates for the template-name selector."""
    from services.plugin.deps import get_auth_service

    auth = get_auth_service()
    token = await auth.get_api_key("whatsapp_business")
    waba_id = await auth.get_api_key("whatsapp_business_waba_id")
    if not token or not waba_id:
        return []

    url = f"{GRAPH_BASE_URL}/{GRAPH_API_VERSION}/{waba_id}/message_templates"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "name,status,language,category", "limit": 100},
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # A dropdown that cannot load must not break the panel; an empty list
        # leaves the field free-text, which still sends.
        logger.warning("whatsapp business: template list unavailable", error=str(exc))
        return []

    options: List[Dict[str, Any]] = []
    for item in payload.get("data") or []:
        name = item.get("name")
        if not name:
            continue
        status = str(item.get("status") or "").upper()
        language = item.get("language") or ""
        suffix = f" ({language})" if language else ""
        if status != _SENDABLE:
            suffix += f" [{status.title()}]"
        options.append({"value": name, "label": f"{name}{suffix}"})

    options.sort(key=lambda option: option["label"].lower())
    return options


__all__ = ["load_templates"]
