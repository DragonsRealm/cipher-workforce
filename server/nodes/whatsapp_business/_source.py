"""Signed-webhook receiver for Meta's WhatsApp Cloud API.

Meta's delivery shape drives most of the design here:

* One POST can carry several ``entry[]``, each with several ``changes[]``,
  each ``value`` holding several ``messages[]`` and/or ``statuses[]``. Reading
  only ``entry[0].changes[0]`` is the documented common integration bug, so
  this fans out across all three levels.
* Ownership is verified with a ``GET`` that must echo ``hub.challenge`` as
  bare ``text/plain`` -- not the router's JSON body, hence ``handle_get``.
* Signatures are HMAC-SHA256 over the raw body under the Meta **App Secret**,
  in the same header and prefix GitHub uses.
"""

from __future__ import annotations

import hmac
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse

from core.logging import get_logger
from services.events import WebhookSource, WorkflowEvent
from services.events.verifiers import HmacVerifier

from ._credentials import WhatsAppBusinessCredential
from ._events import (
    MESSAGE_RECEIVED_TYPE,
    emit_message_received,
    emit_status_updated,
    message_received,
    status_updated,
)

logger = get_logger(__name__)


class MetaHubVerifier(HmacVerifier):
    """Meta signs exactly as GitHub does: ``X-Hub-Signature-256: sha256=<hex>``.

    Subclassed rather than reusing GitHubVerifier so the secret's identity is
    named at the call site: this is the **App Secret** from the Meta app
    dashboard, not a per-webhook secret. Meta issues no per-endpoint secret,
    which is a real difference from Stripe and easy to assume otherwise.

    Known risk: Meta's *Messenger* docs state the signature is computed over
    an escaped-unicode rendering of the payload rather than the raw bytes.
    That claim appears nowhere in the WhatsApp docs, and raw bytes verify
    correctly in practice, so raw bytes it is. If verification ever fails
    intermittently and the failures correlate with emoji or non-Latin names,
    this is the first thing to check -- hence the diagnostic in ``handle``.
    """

    header_name = "X-Hub-Signature-256"
    signature_prefix = "sha256="


def _media_block(message: Dict[str, Any], msg_type: str) -> Optional[Dict[str, Any]]:
    """Normalise whichever media sub-object this message carries.

    Deliberately carries the ``id`` and no bytes. Downloading here would put
    the payload into the node result, which is persisted, broadcast and
    replayed into LLM context -- the whatsappBusinessMedia node downloads on
    demand instead.
    """
    payload = message.get(msg_type)
    if not isinstance(payload, dict):
        return None
    if msg_type not in {"image", "video", "audio", "document", "sticker"}:
        return None
    return {
        "kind": msg_type,
        "id": payload.get("id"),
        "mime_type": payload.get("mime_type"),
        "sha256": payload.get("sha256"),
        "filename": payload.get("filename"),
        "caption": payload.get("caption"),
        "voice": payload.get("voice"),
        "animated": payload.get("animated"),
    }


def _text_of(message: Dict[str, Any], msg_type: str) -> str:
    """The human-readable body, whatever shape it arrived in.

    Buttons and interactive replies are the user speaking as much as a text
    message is, so a downstream agent should not have to branch on type just
    to read what someone said.
    """
    if msg_type == "text":
        return str((message.get("text") or {}).get("body") or "")
    if msg_type == "button":
        return str((message.get("button") or {}).get("text") or "")
    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return str(reply.get("title") or "")
    if msg_type in {"image", "video", "document"}:
        return str((message.get(msg_type) or {}).get("caption") or "")
    return ""


def _interactive_reply(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    interactive = message.get("interactive")
    if not isinstance(interactive, dict):
        button = message.get("button")
        if isinstance(button, dict):
            # A template quick-reply tap. `payload` is the developer-defined
            # id; `text` is the visible label.
            return {"kind": "button", "id": button.get("payload"), "title": button.get("text")}
        return None
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    return {
        "kind": interactive.get("type"),
        "id": reply.get("id"),
        "title": reply.get("title"),
        "description": reply.get("description"),
    }


def shape_message(
    message: Dict[str, Any], metadata: Dict[str, Any], contacts: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Flatten one inbound message into the trigger's output.

    Kept flat because the deployed path uses ``event["data"]`` verbatim as the
    trigger output, and ``{{trigger.field}}`` resolves against its top level.
    A non-dict here is coerced to ``{}`` upstream, which would silently empty
    the trigger.
    """
    msg_type = str(message.get("type") or "")
    profile = (contacts[0].get("profile") if contacts else None) or {}
    return {
        "message_id": message.get("id"),
        "from": message.get("from"),
        "wa_id": (contacts[0].get("wa_id") if contacts else None),
        "profile_name": profile.get("name"),
        "timestamp": message.get("timestamp"),
        "type": msg_type,
        "text": _text_of(message, msg_type),
        "phone_number_id": metadata.get("phone_number_id"),
        "display_phone_number": metadata.get("display_phone_number"),
        "media": _media_block(message, msg_type),
        "interactive_reply": _interactive_reply(message),
        "reply_to_message_id": (message.get("context") or {}).get("id"),
        "location": message.get("location"),
        "contacts": message.get("contacts"),
        "reaction": message.get("reaction"),
        "errors": message.get("errors"),
    }


def shape_status(status: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    conversation = status.get("conversation") or {}
    pricing = status.get("pricing") or {}
    errors = status.get("errors") or []
    first_error = errors[0] if errors else {}
    return {
        "message_id": status.get("id"),
        "status": status.get("status"),
        "recipient_id": status.get("recipient_id"),
        "timestamp": status.get("timestamp"),
        "phone_number_id": metadata.get("phone_number_id"),
        "conversation_id": conversation.get("id"),
        # The programmatic read on when the 24-hour customer service window
        # closes, so a workflow can act before a send starts failing with 131047.
        "conversation_expires_at": conversation.get("expiration_timestamp"),
        "conversation_category": (conversation.get("origin") or {}).get("type"),
        "billable": pricing.get("billable"),
        # Open string on purpose: both CBP (deprecated) and PMP appear live,
        # and pricing.type exists only in the newer shape.
        "pricing_model": pricing.get("pricing_model"),
        "pricing_category": pricing.get("category"),
        "error_code": first_error.get("code"),
        "error_title": first_error.get("title"),
        "error_detail": (first_error.get("error_data") or {}).get("details"),
        # Our own correlation value, echoed back verbatim by Meta. More
        # reliable than matching on the wamid alone.
        "biz_opaque_callback_data": status.get("biz_opaque_callback_data"),
    }


def iter_events(payload: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any], str]]:
    """Flatten a webhook body into ``(kind, data, event_id)`` triples.

    Walks all three nesting levels. Status ids are composite
    (``<wamid>:<status>``) because one message reports sent, delivered and
    read under the same wamid and the listener dedups by id.
    """
    out: List[Tuple[str, Dict[str, Any], str]] = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            metadata = value.get("metadata") or {}
            contacts = value.get("contacts") or []

            for message in value.get("messages") or []:
                message_id = message.get("id")
                if not message_id:
                    # Without a stable id the listener drops it anyway, and a
                    # minted one would defeat replay dedup.
                    logger.warning("whatsapp cloud: inbound message without an id, skipping")
                    continue
                out.append(("message", shape_message(message, metadata, contacts), str(message_id)))

            for status in value.get("statuses") or []:
                status_id = status.get("id")
                state = status.get("status")
                if not status_id or not state:
                    logger.warning("whatsapp cloud: status without id/status, skipping")
                    continue
                out.append(("status", shape_status(status, metadata), f"{status_id}:{state}"))

            for error in value.get("errors") or []:
                # Account/app-level errors are not a trigger firing -- there is
                # no message and no workflow to run -- but they are the only
                # signal for things like a token going bad, so they are logged.
                logger.warning(
                    "whatsapp cloud account-level error",
                    code=error.get("code"),
                    title=error.get("title"),
                    detail=(error.get("error_data") or {}).get("details"),
                )
    return out


class WhatsAppBusinessWebhookSource(WebhookSource):
    type = "whatsapp_business.webhook"
    path = "whatsapp-business"
    verifier = MetaHubVerifier
    secret_field = "whatsapp_business_app_secret"
    credential = WhatsAppBusinessCredential

    async def handle_get(self, request: Request) -> Optional[Response]:
        """Answer Meta's subscription handshake.

        Meta sends ``hub.mode=subscribe``, ``hub.verify_token`` and
        ``hub.challenge``, and requires the bare challenge echoed back. Its
        docs describe the challenge as a string on the WhatsApp page and an
        int on the Graph page, so it is echoed verbatim -- which satisfies
        both readings.
        """
        params = request.query_params
        challenge = params.get("hub.challenge")
        supplied = params.get("hub.verify_token")
        if challenge is None or supplied is None:
            # Not a handshake; let the normal POST path deal with it.
            return None

        expected = None
        try:
            secrets = await self.credential.resolve()
            expected = secrets.get("whatsapp_business_verify_token")
        except PermissionError:
            expected = None

        if not expected:
            logger.warning("whatsapp cloud: handshake attempted with no verify token stored")
            return PlainTextResponse("verify token not configured", status_code=403)

        if not hmac.compare_digest(str(expected), str(supplied)):
            logger.warning("whatsapp cloud: handshake rejected, verify token mismatch")
            return PlainTextResponse("verification failed", status_code=403)

        logger.info("whatsapp cloud: webhook subscription verified")
        return PlainTextResponse(str(challenge))

    async def shape(self, request: Request, body: bytes, payload: dict) -> WorkflowEvent:
        """Shaping happens here, not in the node's ``shape_output``.

        ``shape_output`` runs only on the canvas Run path. When deployed the
        trigger output is ``event["data"]`` verbatim, so shaping there would
        make Run and deploy disagree about the output shape.

        A single return value cannot express a batch, so every event is
        emitted directly and the last is returned for the caller's logging.
        """
        events = iter_events(payload)
        if not events:
            return WorkflowEvent(source="opencompany://nodes/whatsapp_business", type=MESSAGE_RECEIVED_TYPE, data={})

        last: Optional[WorkflowEvent] = None
        for kind, data, event_id in events:
            if kind == "message":
                await emit_message_received(data, event_id=event_id)
                last = message_received(data, event_id=event_id)
            else:
                await emit_status_updated(data, event_id=event_id)
                last = status_updated(data, event_id=event_id)
        return last  # type: ignore[return-value]

    async def handle(self, request: Request) -> WorkflowEvent:
        try:
            return await super().handle(request)
        except Exception:
            # Non-ASCII in the body is the fingerprint of the escaped-unicode
            # signature question documented on MetaHubVerifier. Recording it
            # here turns "signatures fail sometimes" into a one-line answer.
            try:
                raw = await request.body()
                if any(byte > 127 for byte in raw):
                    logger.warning(
                        "whatsapp cloud: webhook failed on a payload containing "
                        "non-ASCII bytes -- if this correlates with emoji or "
                        "non-Latin names, check the escaped-unicode signature note"
                    )
            except Exception:  # pragma: no cover - diagnostics must never mask
                pass
            raise


_source: Optional[WhatsAppBusinessWebhookSource] = None


def get_webhook_source() -> WhatsAppBusinessWebhookSource:
    global _source
    if _source is None:
        _source = WhatsAppBusinessWebhookSource()
    return _source


__all__ = [
    "MetaHubVerifier",
    "WhatsAppBusinessWebhookSource",
    "get_webhook_source",
    "iter_events",
    "shape_message",
    "shape_status",
]
