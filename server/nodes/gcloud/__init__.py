"""Plugin for the 'deployment' palette group — Google Cloud via the
gcloud CLI.

Self-contained CLI-managed-auth plugin (gh/Stripe pattern): the gcloud
CLI owns its auth end-to-end — ``gcloud auth login`` driven from the
credentials modal opens the browser itself, credentials land in
gcloud's own store under the pinned ``CLOUDSDK_CONFIG`` dir
(``<DATA_DIR>/gcloud/``), and a synthetic ``cli-managed`` marker OAuth
row drives the catalogue badge. OpenCompany never stores or injects a
token. The SDK itself is project-local and version-pinned under
``<DATA_DIR>/packages/gcloud/`` — the system gcloud is never consulted.
"""

from __future__ import annotations

from services.node_output_schemas import register_output_schema
from services.ws_handler_registry import register_ws_handlers

from ._credentials import GCloudCredential
from ._handlers import WS_HANDLERS
from .gcloud_action import GCloudActionNode, GCloudActionOutput

register_ws_handlers(WS_HANDLERS)
register_output_schema("gcloudAction", GCloudActionOutput)

__all__ = [
    "GCloudCredential",
    "GCloudActionNode",
    "WS_HANDLERS",
]
