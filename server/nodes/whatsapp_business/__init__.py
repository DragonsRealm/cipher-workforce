"""Official Meta WhatsApp Business Platform (Cloud API) plugin.

Distinct from ``nodes/whatsapp/``, which drives a *personal* account through
an unofficial Go bridge and QR pairing. Both ship; they share no node types,
no credential id, no WebSocket keys and no palette group, because the auth
models, capabilities and failure modes have nothing in common.

Importing the node modules is what registers them --
``BaseNode.__init_subclass__`` does the work, so this file stays pure wiring.
"""

from __future__ import annotations

from services.deployment.canary_registry import register_canary_trigger_type
from services.events import register_webhook_source
from services.node_output_schemas import register_output_schema
from services.plugin.social_provider_registry import register_social_send_handler

from services.ws_handler_registry import register_option_loader

from ._credentials import WhatsAppBusinessCredential
from ._events import MESSAGE_RECEIVED_TYPE, STATUS_UPDATED_TYPE
from ._option_loaders import load_templates
from ._social import social_send_adapter
from ._source import get_webhook_source
from .whatsapp_business_media import WhatsAppBusinessMediaNode, WhatsAppBusinessMediaOutput
from .whatsapp_business_receive import (
    WhatsAppBusinessReceiveNode,
    WhatsAppBusinessReceiveOutput,
    WhatsAppBusinessStatusNode,
    WhatsAppBusinessStatusOutput,
)
from .whatsapp_business_send import WhatsAppBusinessSendNode, WhatsAppBusinessSendOutput

# Claims POST/GET /webhook/whatsapp-business. Takes an instance, not the class.
# Without this the path falls through to the generic legacy handler, which
# would fire every deployed webhookTrigger instead of these nodes.
register_webhook_source(get_webhook_source())

# Opt both triggers into the Temporal listener path. The second argument must
# equal the ``type`` on the envelope the source emits -- it becomes the
# EventType Search Attribute the Visibility query matches on, and a mismatch
# is silent: the listener runs forever and never receives a signal.
register_canary_trigger_type(WhatsAppBusinessReceiveNode.type, MESSAGE_RECEIVED_TYPE)
register_canary_trigger_type(WhatsAppBusinessStatusNode.type, STATUS_UPDATED_TYPE)

register_option_loader("whatsappBusinessTemplates", load_templates)

# socialSend routes by platform id. Registered under "whatsapp_business", not
# "whatsapp": nodes/whatsapp/ owns that key, and the two share no credential
# and no API.
register_social_send_handler("whatsapp_business", social_send_adapter)

register_output_schema(WhatsAppBusinessSendNode.type, WhatsAppBusinessSendOutput)
register_output_schema(WhatsAppBusinessReceiveNode.type, WhatsAppBusinessReceiveOutput)
register_output_schema(WhatsAppBusinessStatusNode.type, WhatsAppBusinessStatusOutput)
register_output_schema(WhatsAppBusinessMediaNode.type, WhatsAppBusinessMediaOutput)

__all__ = [
    "WhatsAppBusinessCredential",
    "WhatsAppBusinessMediaNode",
    "WhatsAppBusinessReceiveNode",
    "WhatsAppBusinessSendNode",
    "WhatsAppBusinessStatusNode",
]
