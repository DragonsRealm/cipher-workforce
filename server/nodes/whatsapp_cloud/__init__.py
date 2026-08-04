"""Official Meta WhatsApp Business Platform (Cloud API) plugin.

Distinct from ``nodes/whatsapp/``, which drives a *personal* account through
an unofficial Go bridge and QR pairing. Both ship; they share no node types,
no credential id, no WebSocket keys and no palette group, because the auth
models, capabilities and failure modes have nothing in common.

Importing the node modules is what registers them --
``BaseNode.__init_subclass__`` does the work, so this file stays pure wiring.
"""

from __future__ import annotations

from services.node_output_schemas import register_output_schema

from ._credentials import WhatsAppCloudCredential
from .whatsapp_cloud_send import WhatsAppCloudSendNode, WhatsAppCloudSendOutput

register_output_schema(WhatsAppCloudSendNode.type, WhatsAppCloudSendOutput)

__all__ = [
    "WhatsAppCloudCredential",
    "WhatsAppCloudSendNode",
]
