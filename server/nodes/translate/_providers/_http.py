"""Shared httpx plumbing for translate providers.

Mirrors ``nodes/speech/_providers/_http.py``. Auth diverges again — DeepL uses
a scheme keyword nobody else does (``DeepL-Auth-Key``), Sarvam uses a bare
custom header — so :meth:`auth_headers` is the single override point.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from core.logging import get_logger

from .. import _config as translate_config

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0


class HttpTranslateProvider:
    """Base for HTTP-backed text-language providers."""

    provider_name: str = ""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        provider_name: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.provider_name = provider_name or type(self).provider_name
        self.api_key = api_key
        # Resolved from the key where a provider needs it to (DeepL's
        # free-vs-pro host split keys off the ':fx' suffix).
        self._base_url = (
            base_url or translate_config.base_url(self.provider_name, api_key=api_key)
        ).rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self.auth_headers(),
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        response = await self.client.request(
            method, path, json=json_body, params=params, headers=headers
        )
        response.raise_for_status()
        return response

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def drop_none(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip ``None`` values. Several of these APIs 422 on an explicit null."""
    return {k: v for k, v in payload.items() if v is not None}


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "HttpTranslateProvider", "drop_none"]
