"""Inbound triggers for the WhatsApp Cloud API.

Two nodes rather than one with a mode switch, for two reasons that
outlived the original one.

The original reason was that per-node filters were not applied on the
deployed push path, so a ``trigger_on: messages | statuses`` parameter
would work on Run and silently fire on everything once deployed. That is
no longer true -- the node's filter now gates the spawn (see
``machina-trigger-listener-node-filter``) -- but the split still stands:

  * The CloudEvents type is the routing key. A canary trigger registers
    exactly one type (``canary_registry`` is a str-to-str map and the
    listener carries a single ``EventType`` keyword Search Attribute), so
    one merged node could only subscribe to one of the two.
  * The payloads are genuinely different shapes. An inbound message and a
    delivery receipt share almost no fields, so merging would mean a union
    output schema and weaker ``{{trigger.field}}`` resolution downstream.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.events import BaseTriggerParams, WebhookTriggerNode, WorkflowEvent

from ._credentials import WhatsAppBusinessCredential
from ._source import WhatsAppBusinessWebhookSource

_OUTPUT_HANDLE = (
    {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
)


class WhatsAppBusinessMediaRef(BaseModel):
    """The media *id* Meta sends, never the bytes.

    Downloading during the trigger would put the payload into the node
    result, which is persisted, broadcast and replayed into LLM context.
    whatsappBusinessMedia resolves this on demand instead.
    """

    kind: Optional[str] = None
    id: Optional[str] = None
    mime_type: Optional[str] = None
    sha256: Optional[str] = None
    filename: Optional[str] = None
    caption: Optional[str] = None
    voice: Optional[bool] = None
    animated: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class WhatsAppBusinessInteractiveReply(BaseModel):
    kind: Optional[str] = None
    id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class WhatsAppBusinessReceiveOutput(BaseModel):
    message_id: Optional[str] = None
    # ``from`` is a Python keyword, so the field is declared by alias. Meta's
    # payload uses the bare name and the trigger output is consumed as
    # {{node.from}}, so renaming it would break the obvious template.
    from_: Optional[str] = Field(default=None, alias="from")
    wa_id: Optional[str] = None
    profile_name: Optional[str] = None
    timestamp: Optional[str] = None
    type: Optional[str] = None
    text: Optional[str] = None
    phone_number_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    media: Optional[WhatsAppBusinessMediaRef] = None
    interactive_reply: Optional[WhatsAppBusinessInteractiveReply] = None
    reply_to_message_id: Optional[str] = None
    location: Optional[dict] = None
    contacts: Optional[List[dict]] = None
    reaction: Optional[dict] = None
    errors: Optional[List[dict]] = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class WhatsAppBusinessStatusOutput(BaseModel):
    message_id: Optional[str] = None
    status: Optional[str] = None
    recipient_id: Optional[str] = None
    timestamp: Optional[str] = None
    phone_number_id: Optional[str] = None
    conversation_id: Optional[str] = None
    conversation_expires_at: Optional[str] = None
    conversation_category: Optional[str] = None
    billable: Optional[bool] = None
    pricing_model: Optional[str] = None
    pricing_category: Optional[str] = None
    error_code: Optional[int] = None
    error_title: Optional[str] = None
    error_detail: Optional[str] = None
    biz_opaque_callback_data: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class _WhatsAppBusinessTrigger(WebhookTriggerNode, abstract=True):
    """Everything both inbound triggers share.

    ``abstract=True`` keeps ``__init_subclass__`` from registering this as
    a node; only the two concrete subclasses below appear on the canvas.
    """

    group = ("whatsapp_business", "trigger")
    component_kind = "trigger"
    handles = _OUTPUT_HANDLE
    credentials = (WhatsAppBusinessCredential,)
    webhook_source = WhatsAppBusinessWebhookSource
    Params = BaseTriggerParams

    async def _check_precondition(self) -> Optional[str]:
        """Fail fast on the canvas instead of waiting out the 24h timeout."""
        try:
            secrets = await WhatsAppBusinessCredential.resolve()
        except PermissionError:
            return (
                "WhatsApp Business is not connected. Add the credential in "
                "Credentials first."
            )
        if not secrets.get("whatsapp_business_app_secret"):
            return (
                "Add the Meta App Secret to the WhatsApp Business credential -- "
                "inbound webhooks cannot be verified without it."
            )
        return None

    def shape_output(self, event: WorkflowEvent) -> Dict:
        """Emit the flat payload, which is what the deployed path emits.

        ``TriggerListenerWorkflow`` hands downstream nodes ``event.data``,
        while the base default dumps the whole CloudEvents envelope. Without
        this override ``{{trigger.text}}`` resolves once deployed and breaks
        when you press Run, and the Run output does not match the declared
        ``Output`` model.

        The shaping itself already happened in the webhook source, so this
        only unwraps -- deliberately, so the two paths cannot drift into
        producing different fields.
        """
        return event.data if isinstance(event.data, dict) else {}


class WhatsAppBusinessReceiveNode(_WhatsAppBusinessTrigger):
    type = "whatsappBusinessReceive"
    display_name = "WhatsApp Business Receive"
    subtitle = "Inbound message"
    description = "Trigger when a WhatsApp user messages your business number"
    event_type_prefix = "com.opencompany.whatsapp_business.message."
    Output = WhatsAppBusinessReceiveOutput


class WhatsAppBusinessStatusNode(_WhatsAppBusinessTrigger):
    type = "whatsappBusinessStatus"
    display_name = "WhatsApp Business Status"
    subtitle = "Delivery status"
    description = "Trigger on sent / delivered / read / failed callbacks for messages you sent"
    event_type_prefix = "com.opencompany.whatsapp_business.status."
    Output = WhatsAppBusinessStatusOutput


__all__ = [
    "WhatsAppBusinessReceiveNode",
    "WhatsAppBusinessReceiveOutput",
    "WhatsAppBusinessStatusNode",
    "WhatsAppBusinessStatusOutput",
]
