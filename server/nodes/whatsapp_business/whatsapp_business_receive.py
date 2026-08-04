"""Inbound triggers for the WhatsApp Cloud API.

Two nodes rather than one with a mode switch. Per-node filters are not
applied on the deployed push path -- only the CloudEvents type is -- so a
``trigger_on: messages | statuses`` parameter would work when you press Run
and silently fire on everything once deployed. Separate node types get
separate type strings, which is the discriminator that actually works.
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


class WhatsAppBusinessReceiveNode(WebhookTriggerNode):
    type = "whatsappBusinessReceive"
    display_name = "WhatsApp Business Receive"
    subtitle = "Inbound message"
    group = ("whatsapp_business", "trigger")
    description = "Trigger when a WhatsApp user messages your business number"
    component_kind = "trigger"
    handles = _OUTPUT_HANDLE
    credentials = (WhatsAppBusinessCredential,)
    webhook_source = WhatsAppBusinessWebhookSource
    event_type_prefix = "com.opencompany.whatsapp_business.message."
    Params = BaseTriggerParams
    Output = WhatsAppBusinessReceiveOutput

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


class WhatsAppBusinessStatusNode(WebhookTriggerNode):
    type = "whatsappBusinessStatus"
    display_name = "WhatsApp Business Status"
    subtitle = "Delivery status"
    group = ("whatsapp_business", "trigger")
    description = "Trigger on sent / delivered / read / failed callbacks for messages you sent"
    component_kind = "trigger"
    handles = _OUTPUT_HANDLE
    credentials = (WhatsAppBusinessCredential,)
    webhook_source = WhatsAppBusinessWebhookSource
    event_type_prefix = "com.opencompany.whatsapp_business.status."
    Params = BaseTriggerParams
    Output = WhatsAppBusinessStatusOutput

    async def _check_precondition(self) -> Optional[str]:
        return await WhatsAppBusinessReceiveNode._check_precondition(self)  # type: ignore[arg-type]


__all__ = [
    "WhatsAppBusinessReceiveNode",
    "WhatsAppBusinessReceiveOutput",
    "WhatsAppBusinessStatusNode",
    "WhatsAppBusinessStatusOutput",
]
