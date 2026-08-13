"""Plugin-owned dispatch registry for the social-messaging facade.

Closes the cross-plugin reach from ``nodes/social/_base.py:478`` —
``from nodes.whatsapp._service import handle_whatsapp_send`` — which
made the ``social`` plugin depend on the ``whatsapp`` plugin's
internals and violated the "framework knows no plugin names" rule.

Each social platform plugin (whatsapp, telegram, slack, discord, …)
registers a send handler keyed by the platform identifier from its
own ``__init__.py``. The social node queries the registry instead of
importing platform internals — same Wave-11.I plugin-self-registration
pattern as ``register_filter_builder`` / ``register_poll_coroutine_factory``
/ ``register_ws_handlers`` / ``register_canary_trigger_type``.

Handler signature
-----------------

::

    handler(payload: Dict[str, Any]) -> Awaitable[Dict[str, Any]]

``payload`` is **socialSend-shaped**, not platform-shaped: it is the node's
own parameters plus a resolved ``recipient`` and the normalised
``channel`` / ``recipient_type`` / ``message_type``. Translating that onto
the platform's native parameter names is the *handler's* job.

That direction is deliberate. The social node previously did the mapping
itself, which meant one platform's parameter names (``media_url``,
``vcard``, ``is_reply``) lived in the platform-neutral module and every new
platform would have added a branch there. Registering an adapter instead
keeps each platform's vocabulary inside its own plugin, so
``handle_social_send`` names no platform at all.

The handler returns the platform's native result dict; the social node
passes it through after checking ``success``.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from services.plugin.registry import IdempotentRegistry


# (payload: Dict, ctx: NodeContext) -> Awaitable[Dict]
SocialSendHandler = Callable[[Dict[str, Any], Any], Awaitable[Dict[str, Any]]]


_REGISTRY: IdempotentRegistry[str, SocialSendHandler] = IdempotentRegistry("social_send_handler")


def register_social_send_handler(platform: str, handler: SocialSendHandler) -> None:
    """Publish a send handler for one social platform.

    Idempotent on re-import (same callable for the same platform key
    is a no-op). A different callable for an existing platform raises
    ``ValueError`` to surface plugin namespace collisions at import time.

    Args:
        platform: Lower-case platform identifier (``"whatsapp"``,
            ``"telegram"``, …). Matches the value the social node's
            ``channel`` parameter holds at runtime.
        handler: Async function accepting a socialSend-shaped
            ``payload: Dict`` and the node's ``NodeContext``, returning
            the platform's native result dict. The handler maps the
            generic payload onto its own platform's keys — see the
            module docstring.
    """
    _REGISTRY.register(platform, handler)


def get_social_send_handler(platform: str) -> Optional[SocialSendHandler]:
    """Return the handler for ``platform``, or ``None`` if unregistered.

    A ``None`` return surfaces as a clear "unsupported platform" error
    at the social node call site instead of an ``ImportError`` deep
    inside the platform's ``_service.py``.
    """
    return _REGISTRY.get(platform)


def registered_platforms() -> frozenset[str]:
    """Return an immutable snapshot of registered platform identifiers."""
    return frozenset(_REGISTRY.keys())


__all__ = [
    "register_social_send_handler",
    "get_social_send_handler",
    "registered_platforms",
    "SocialSendHandler",
]
