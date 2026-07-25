"""Provider-neutral speech types — the contract every provider implements.

Mirrors :mod:`services.llm.protocol`: stdlib dataclasses only, no Pydantic,
no SDK objects. These values cross the Temporal boundary and land in node
results, so everything here must stay JSON-safe.

The one place this deliberately diverges from the LLM layer is the error
taxonomy. ``LLMError`` carries ``CONTEXT_LENGTH``, which is meaningless for
speech, and speech has failure modes the LLM layer has never seen
(``AUDIO_TOO_LONG``, ``UNSUPPORTED_FORMAT``). Sharing one enum would mean
half the members raising "can't happen here" questions on both sides, so
each layer owns its own. The HTTP status ladder underneath is similar by
coincidence of HTTP, not by shared domain.

Audio bytes appear in exactly two places here — :class:`AudioPayload` on the
way out of a TTS provider, and :attr:`SttRequest.audio` on the way in. Both
are consumed inside the node before it returns; what leaves the node is an
``AudioRef`` (see :mod:`services.media.refs`). Nothing in this module is
allowed into a node ``Output`` model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass
class TtsRequest:
    """One synthesis call, in provider-neutral terms.

    ``provider_options`` is the escape hatch for vendor-specific keys that
    do not generalise (ElevenLabs' ``stability``, Sarvam's ``pitch``). It is
    passed through untouched, which keeps the common surface small without
    making any provider's full capability unreachable.
    """

    text: str
    model: str = ""
    voice: str = ""
    language: str = ""
    speed: Optional[float] = None
    output_format: str = ""
    sample_rate: Optional[int] = None
    provider_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SttRequest:
    """One transcription call.

    ``audio`` is raw bytes rather than a path or a file handle: providers
    disagree on transport (multipart vs. raw body), and a handle cannot be
    replayed by ``Connection``'s auth-retry. The caller has already applied
    containment via ``services.media.coerce_file_param``.
    """

    audio: bytes
    filename: str = "audio.wav"
    mime_type: str = "application/octet-stream"
    model: str = ""
    language: str = ""
    prompt: str = ""
    translate: bool = False
    diarize: bool = False
    timestamps: bool = False
    provider_options: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class AudioPayload:
    """One synthesized audio blob.

    Providers return a *list* of these. Most emit exactly one; Sarvam splits
    long input into several standalone clips, and those are not
    byte-concatenatable, so the plural is the honest shape rather than a
    convenience.
    """

    data: bytes
    format: str = ""
    mime_type: str = ""
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


@dataclass
class TtsResult:
    audio: List[AudioPayload] = field(default_factory=list)
    model: str = ""
    voice: str = ""
    request_id: Optional[str] = None
    # Billing units differ per provider (characters / seconds / minutes), so
    # the provider that made the call reports both the count and its unit.
    # Central code never guesses.
    billed_units: Optional[float] = None
    billed_unit: str = ""


@dataclass
class TranscriptWord:
    word: str
    start: Optional[float] = None
    end: Optional[float] = None
    speaker: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class SttResult:
    text: str = ""
    language: Optional[str] = None
    language_confidence: Optional[float] = None
    duration_seconds: Optional[float] = None
    words: List[TranscriptWord] = field(default_factory=list)
    segments: List[Dict[str, Any]] = field(default_factory=list)
    request_id: Optional[str] = None
    billed_units: Optional[float] = None
    billed_unit: str = ""


@dataclass
class Voice:
    """One selectable voice, for the node's dropdown loader."""

    id: str
    name: str = ""
    description: str = ""
    language: str = ""
    preview_url: str = ""

    def as_option(self) -> Dict[str, str]:
        """Shape the ``loadOptionsMethod`` contract expects."""
        option = {"value": self.id, "label": self.name or self.id}
        if self.description:
            option["description"] = self.description
        return option


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SpeechErrorCategory(str, Enum):
    """Stable categories used by retry and user-error policies."""

    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    AUDIO_TOO_LONG = "audio_too_long"
    UNSUPPORTED_FORMAT = "unsupported_format"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    UNKNOWN = "unknown"


_RETRYABLE = {
    SpeechErrorCategory.RATE_LIMIT,
    SpeechErrorCategory.TIMEOUT,
    SpeechErrorCategory.CONNECTION,
    SpeechErrorCategory.SERVER,
}

_PROVIDER_NAMES = {
    "openai": "OpenAI",
    "groq": "Groq",
    "elevenlabs": "ElevenLabs",
    "deepgram": "Deepgram",
    "sarvam": "Sarvam AI",
}


@dataclass
class SpeechError(Exception):
    """Provider-independent structured speech failure."""

    message: str
    provider: str
    category: SpeechErrorCategory = SpeechErrorCategory.UNKNOWN
    retryable: bool = False
    status_code: Optional[int] = None
    provider_code: Optional[str] = None
    request_id: Optional[str] = None
    retry_after: Optional[float] = None
    retry_after_raw: Optional[str] = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @property
    def user_message(self) -> str:
        """A category-based message that is safe for public surfaces.

        ``message`` retains the original provider text for operator
        diagnostics and exception chaining. Provider bodies routinely echo
        request fragments and internal endpoint URLs, so execution
        boundaries must expose this property instead.
        """
        known = str(self.provider or "").strip().lower() in _PROVIDER_NAMES
        provider = _PROVIDER_NAMES.get(
            str(self.provider or "").strip().lower(), "The speech provider"
        )
        provider_object = provider if known else "the speech provider"
        category = (
            self.category.value
            if isinstance(self.category, SpeechErrorCategory)
            else str(self.category or SpeechErrorCategory.UNKNOWN.value)
        )
        messages = {
            SpeechErrorCategory.AUTHENTICATION.value: (
                f"{provider} authentication failed. Check the configured API key."
            ),
            SpeechErrorCategory.PERMISSION.value: (
                f"{provider} denied this request. Check account, plan tier and "
                "model access."
            ),
            SpeechErrorCategory.RATE_LIMIT.value: (
                f"{provider} is rate-limiting requests. Retry after a short delay."
            ),
            SpeechErrorCategory.INVALID_REQUEST.value: (
                f"{provider} rejected the request configuration. Check the "
                "selected model, voice and output format."
            ),
            SpeechErrorCategory.NOT_FOUND.value: (
                f"The configured {provider_object} model, voice or endpoint was "
                "not found."
            ),
            SpeechErrorCategory.AUDIO_TOO_LONG.value: (
                f"The audio is longer or larger than {provider_object} accepts "
                "on this endpoint. Trim or split the clip."
            ),
            SpeechErrorCategory.UNSUPPORTED_FORMAT.value: (
                f"{provider} does not accept this audio format. Convert the "
                "file and try again."
            ),
            SpeechErrorCategory.TIMEOUT.value: (
                f"The request to {provider_object} timed out."
            ),
            SpeechErrorCategory.CONNECTION.value: (
                f"Could not connect to {provider_object}."
            ),
            SpeechErrorCategory.SERVER.value: (
                f"{provider} is temporarily unavailable."
            ),
            SpeechErrorCategory.UNKNOWN.value: f"{provider} request failed.",
        }
        return messages.get(category, messages[SpeechErrorCategory.UNKNOWN.value])

    @classmethod
    def from_exception(cls, provider: str, exc: BaseException) -> "SpeechError":
        """Normalize an httpx / SDK exception into a structured error.

        Duck-typed rather than isinstance-based so a provider may raise its
        own SDK error without this module importing that SDK.
        """
        response = getattr(exc, "response", None)
        status = _optional_int(
            getattr(exc, "status_code", None)
            or getattr(response, "status_code", None)
        )
        body_text = _response_text(response)
        code = _provider_code(response, body_text)
        request_id = (
            getattr(exc, "request_id", None)
            or _header(response, "x-request-id")
            or _header(response, "request-id")
            or _header(response, "dg-request-id")
        )
        retry_after_raw = _header(response, "retry-after")
        category = classify(exc, status, body_text)
        return cls(
            message=_detail(exc, body_text),
            provider=provider,
            category=category,
            retryable=category in _RETRYABLE,
            status_code=status,
            provider_code=str(code) if code is not None else None,
            request_id=str(request_id) if request_id is not None else None,
            retry_after=_optional_float(retry_after_raw),
            retry_after_raw=(
                str(retry_after_raw) if retry_after_raw is not None else None
            ),
        )


def classify(
    exc: BaseException, status: Optional[int], body_text: str = ""
) -> SpeechErrorCategory:
    """Map an exception + HTTP status onto a category.

    Status wins where it is unambiguous; name and body substrings resolve
    the rest. 413 and 415 are the two speech-specific rungs — every provider
    in the v1 set uses them for oversize and unsupported-codec respectively.
    """
    name = type(exc).__name__.lower()
    haystack = f"{exc} {body_text}".lower()

    if status == 401 or "authentication" in name or "api key" in haystack:
        return SpeechErrorCategory.AUTHENTICATION
    if status == 403 or "permission" in name:
        return SpeechErrorCategory.PERMISSION
    if status == 429 or "ratelimit" in name or "rate limit" in haystack:
        return SpeechErrorCategory.RATE_LIMIT
    if status == 404 or "notfound" in name:
        return SpeechErrorCategory.NOT_FOUND
    if status == 413 or "too large" in haystack or "too long" in haystack:
        return SpeechErrorCategory.AUDIO_TOO_LONG
    if status == 415 or "unsupported" in haystack or "invalid audio" in haystack:
        return SpeechErrorCategory.UNSUPPORTED_FORMAT
    if status in {400, 409, 422} or "badrequest" in name:
        return SpeechErrorCategory.INVALID_REQUEST
    if status == 408 or "timeout" in name:
        return SpeechErrorCategory.TIMEOUT
    if "connect" in name:
        return SpeechErrorCategory.CONNECTION
    if status is not None and status >= 500:
        return SpeechErrorCategory.SERVER
    return SpeechErrorCategory.UNKNOWN


# ---------------------------------------------------------------------------
# Small helpers (duck-typed; never import a provider SDK)
# ---------------------------------------------------------------------------


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _header(response: Any, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except (AttributeError, TypeError):
        return None


def _response_text(response: Any) -> str:
    """Best-effort body text, capped. Never raises."""
    if response is None:
        return ""
    try:
        return str(getattr(response, "text", "") or "")[:600]
    except Exception:
        return ""


def _provider_code(response: Any, body_text: str) -> Optional[str]:
    """Pull a vendor error code out of a JSON body if one is there."""
    if response is None:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        return error.get("code") or error.get("type")
    return payload.get("code") or payload.get("err_code")


def _detail(exc: BaseException, body_text: str) -> str:
    """Operator-facing message: exception text plus body when it adds signal."""
    base = str(exc)
    if body_text and body_text not in base:
        return f"{base} :: {body_text}"
    return base


# ---------------------------------------------------------------------------
# Provider protocols (structural typing)
# ---------------------------------------------------------------------------
#
# Two protocols, not one. AssemblyAI is transcription-only and Cartesia is
# synthesis-only; a single protocol would force dead methods that raise at
# runtime *after* the provider has already appeared in a dropdown. With one
# registry per direction, capability is registry membership and there is no
# ``supports_tts`` flag to keep in sync with reality.
#
# ``aclose`` is deliberately absent from both. The unifier probes for it with
# ``getattr`` — every provider here happens to hold an httpx client and
# implement it, but keeping it out of the protocol means a stateless provider
# never has to declare an empty one.


@runtime_checkable
class TtsProvider(Protocol):
    provider_name: str

    async def synthesize(self, req: TtsRequest) -> TtsResult: ...

    async def list_voices(self) -> List[Voice]: ...


@runtime_checkable
class SttProvider(Protocol):
    provider_name: str

    async def transcribe(self, req: SttRequest) -> SttResult: ...


__all__ = [
    "AudioPayload",
    "SpeechError",
    "SpeechErrorCategory",
    "SttProvider",
    "SttRequest",
    "SttResult",
    "TranscriptWord",
    "TtsProvider",
    "TtsRequest",
    "TtsResult",
    "Voice",
    "classify",
]
