"""Shared httpx plumbing for speech providers.

None of the v1 speech providers ship a Python SDK worth taking a dependency
on, so every one of them is a plain HTTP client. This base holds the parts
that are genuinely identical -- lazy client construction, timeouts, status
handling -- and leaves the parts that are not to subclasses.

Authentication is the interesting one. All four providers use a different
scheme, and none of them is plain Bearer across the board:

===========  ==========================================
openai/groq  ``Authorization: Bearer <key>``
elevenlabs   ``xi-api-key: <key>``          (no scheme)
deepgram     ``Authorization: Token <key>`` (not Bearer)
sarvam       ``api-subscription-key: <key>``
===========  ==========================================

So :meth:`HttpSpeechProvider.auth_headers` is the single override point, and
nothing above this layer ever has to know.

Timeouts are generous by speech standards: synthesis of a few thousand
characters routinely takes tens of seconds, and transcription of a long clip
longer still. The default here is well under the Temporal activity
``start_to_close`` so a hung provider surfaces as a provider error rather
than an activity timeout.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from core.logging import get_logger
from .. import _config as speech_config

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 180.0


class HttpSpeechProvider:
    """Base for HTTP-backed speech providers.

    Subclasses set :attr:`provider_name` and implement whichever of
    ``synthesize`` / ``transcribe`` / ``list_voices`` their direction
    requires. Structural typing means there is no ABC to satisfy -- the
    Protocols in :mod:`nodes.speech._protocol` check shape, not ancestry.
    """

    provider_name: str = ""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        provider_name: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        # provider_name may arrive via client_kwargs so one class can serve
        # several registry entries (openai and groq share a transcription
        # implementation and differ only in base URL and model list).
        self.provider_name = provider_name or type(self).provider_name
        self.api_key = api_key
        self._base_url = (
            base_url or speech_config.base_url(self.provider_name)
        ).rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def auth_headers(self) -> Dict[str, str]:
        """Headers that authenticate a request. Override per provider."""
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazily-built client, reused for this provider's lifetime.

        Built on first use rather than in ``__init__`` so constructing a
        provider (which the unifier does inside a lock) never touches the
        network stack.
        """
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
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Any] = None,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Issue one request and raise ``httpx.HTTPStatusError`` on 4xx/5xx.

        ``params`` is not an afterthought: ElevenLabs takes ``output_format``
        and Deepgram takes its entire option set as query string. Passing
        those in a JSON body is accepted and silently ignored, which is far
        worse than an error, so the distinction is explicit at every call
        site.
        """
        response = await self.client.request(
            method,
            path,
            json=json_body,
            params=params,
            data=data,
            files=files,
            content=content,
            headers=headers,
        )
        response.raise_for_status()
        return response

    async def aclose(self) -> None:
        """Close the underlying client. Probed via ``getattr`` by the unifier."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def drop_none(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip ``None`` values from a body or query dict.

    Several of these APIs reject an explicit null where they would happily
    accept an absent key -- Sarvam 422s, Deepgram treats the literal string
    "None" as an option value. Building the full dict and filtering once is
    less error-prone than conditional insertion at a dozen call sites.
    """
    return {k: v for k, v in payload.items() if v is not None}


def query_flags(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Render a query dict for APIs that want lowercase boolean literals.

    httpx encodes ``True`` as ``"true"`` already, but only for actual bools;
    this makes the conversion explicit and survives values arriving as
    strings from JSON parameters.
    """
    rendered: Dict[str, Any] = {}
    for key, value in drop_none(payload).items():
        if isinstance(value, bool):
            rendered[key] = "true" if value else "false"
        else:
            rendered[key] = value
    return rendered


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpSpeechProvider",
    "drop_none",
    "query_flags",
]
