"""Google Cloud credential — thin marker (gh/Stripe idiom).

The gcloud CLI manages its own auth state (credential store under the
pinned ``CLOUDSDK_CONFIG`` dir, populated by ``gcloud auth login`` and
cleared by ``gcloud auth revoke``). OpenCompany stores no token — the
credentials modal's connected badge is driven by the synthetic
``cli-managed`` marker OAuth row written by ``_handlers.py`` after a
successful login.

Deliberately a SEPARATE provider from the Google Workspace ``google``
OAuth2 credential: that one holds OpenCompany-managed tokens minted
with our own OAuth client for the Workspace APIs, while this row is
only a badge for the gcloud CLI's self-managed session. Different id,
different auth model, no storage-key overlap.
"""

from __future__ import annotations

from typing import Any, Dict

from services.plugin.credential import Credential


class GCloudCredential(Credential):
    id = "gcloud"
    display_name = "Google Cloud"
    category = "Deployment"
    auth = "custom"
    docs_url = "https://cloud.google.com/sdk/gcloud"

    @classmethod
    async def resolve(cls, *, user_id: str = "owner") -> Dict[str, Any]:
        """Nothing to resolve — auth lives in the gcloud CLI's own
        credential store under the pinned config dir."""
        return {}
