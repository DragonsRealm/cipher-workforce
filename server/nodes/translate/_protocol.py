"""Provider-neutral text-language types.

Same shape as :mod:`nodes.speech._protocol`, minus everything that existed
there to keep audio bytes out of the engine. Text results are small, so there
is no ``FileRef`` equivalent and none of ``services.media`` is involved.

**Three capabilities, three registries.** Translation, transliteration and
language identification are related but not interchangeable: DeepL translates
and does nothing else, ICU transliterates without translating, and a
general-purpose LLM does all three. One registry with capability flags would
put a provider in a dropdown it cannot serve and fail at runtime; membership
per capability makes that unrepresentable. This is the same argument that gave
speech two registries rather than one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass
class TranslateRequest:
    """One translation call.

    ``text`` is a list because the batch shape is the honest one: DeepL takes
    an array natively, and a provider that only handles one string at a time
    can loop. Modelling it as a single string would make batching impossible
    to add later without breaking every caller.
    """

    text: List[str]
    target_language: str
    source_language: str = ""
    model: str = ""
    formality: str = ""
    context: str = ""
    preserve_formatting: bool = False
    provider_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TransliterateRequest:
    """Script conversion — same language, different writing system."""

    text: List[str]
    target_script: str = ""
    source_language: str = ""
    target_language: str = ""
    model: str = ""
    # Numerals and spoken-form handling differ enough between vendors that
    # they stay in provider_options rather than being half-normalised.
    provider_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectRequest:
    """Language identification."""

    text: List[str]
    model: str = ""
    provider_options: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class TranslatedText:
    text: str = ""
    detected_source_language: Optional[str] = None


@dataclass
class TranslateResult:
    translations: List[TranslatedText] = field(default_factory=list)
    model: str = ""
    request_id: Optional[str] = None
    # Reported by the provider when it tells us (DeepL returns
    # `billed_characters` per translation, which beats counting input
    # ourselves); otherwise the node falls back to input length.
    billed_units: Optional[float] = None
    billed_unit: str = "characters"

    @property
    def text(self) -> str:
        """First translation, for the single-input common case."""
        return self.translations[0].text if self.translations else ""


@dataclass
class TransliterateResult:
    results: List[str] = field(default_factory=list)
    model: str = ""
    request_id: Optional[str] = None
    billed_units: Optional[float] = None
    billed_unit: str = "characters"

    @property
    def text(self) -> str:
        return self.results[0] if self.results else ""


@dataclass
class DetectedLanguage:
    language: str = ""
    script: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class DetectResult:
    detections: List[DetectedLanguage] = field(default_factory=list)
    model: str = ""
    request_id: Optional[str] = None
    billed_units: Optional[float] = None
    billed_unit: str = "characters"

    @property
    def language(self) -> str:
        return self.detections[0].language if self.detections else ""


@dataclass
class LanguageOption:
    """One selectable language, for the dropdown loaders."""

    code: str
    name: str = ""

    def as_option(self) -> Dict[str, str]:
        return {"value": self.code, "label": self.name or self.code}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TranslateErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    NOT_FOUND = "not_found"
    TEXT_TOO_LONG = "text_too_long"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    UNKNOWN = "unknown"


_RETRYABLE = {
    TranslateErrorCategory.RATE_LIMIT,
    TranslateErrorCategory.TIMEOUT,
    TranslateErrorCategory.CONNECTION,
    TranslateErrorCategory.SERVER,
}

_PROVIDER_NAMES = {
    "deepl": "DeepL",
    "sarvam": "Sarvam AI",
    "openai": "OpenAI",
}


@dataclass
class TranslateError(Exception):
    """Provider-independent structured failure."""

    message: str
    provider: str
    category: TranslateErrorCategory = TranslateErrorCategory.UNKNOWN
    retryable: bool = False
    status_code: Optional[int] = None
    provider_code: Optional[str] = None
    request_id: Optional[str] = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @property
    def user_message(self) -> str:
        """Sanitized message — the only field allowed across a boundary.

        ``message`` keeps the raw provider text for operator diagnostics, and
        provider bodies routinely echo request fragments.
        """
        key = str(self.provider or "").strip().lower()
        known = key in _PROVIDER_NAMES
        provider = _PROVIDER_NAMES.get(key, "The translation provider")
        obj = provider if known else "the translation provider"
        category = (
            self.category.value
            if isinstance(self.category, TranslateErrorCategory)
            else str(self.category or TranslateErrorCategory.UNKNOWN.value)
        )
        messages = {
            TranslateErrorCategory.AUTHENTICATION.value: (
                f"{provider} authentication failed. Check the configured API key."
            ),
            TranslateErrorCategory.PERMISSION.value: (
                f"{provider} denied this request. Check the account and plan."
            ),
            TranslateErrorCategory.QUOTA.value: (
                f"The {obj} character quota is exhausted for this billing period."
            ),
            TranslateErrorCategory.RATE_LIMIT.value: (
                f"{provider} is rate-limiting requests. Retry after a short delay."
            ),
            TranslateErrorCategory.INVALID_REQUEST.value: (
                f"{provider} rejected the request configuration."
            ),
            TranslateErrorCategory.UNSUPPORTED_LANGUAGE.value: (
                f"{provider} does not support that language pair."
            ),
            TranslateErrorCategory.NOT_FOUND.value: (
                f"The configured {obj} model or endpoint was not found."
            ),
            TranslateErrorCategory.TEXT_TOO_LONG.value: (
                f"The text exceeds what {obj} accepts in one request. Split it."
            ),
            TranslateErrorCategory.TIMEOUT.value: f"The request to {obj} timed out.",
            TranslateErrorCategory.CONNECTION.value: f"Could not connect to {obj}.",
            TranslateErrorCategory.SERVER.value: (
                f"{provider} is temporarily unavailable."
            ),
            TranslateErrorCategory.UNKNOWN.value: f"{provider} request failed.",
        }
        return messages.get(category, messages[TranslateErrorCategory.UNKNOWN.value])

    @classmethod
    def from_exception(cls, provider: str, exc: BaseException) -> "TranslateError":
        response = getattr(exc, "response", None)
        status = _optional_int(
            getattr(exc, "status_code", None)
            or getattr(response, "status_code", None)
        )
        body = _response_text(response)
        category = classify(exc, status, body)
        return cls(
            message=_detail(exc, body),
            provider=provider,
            category=category,
            retryable=category in _RETRYABLE,
            status_code=status,
            provider_code=_provider_code(response),
            request_id=_header(response, "x-request-id"),
        )


def classify(
    exc: BaseException, status: Optional[int], body_text: str = ""
) -> TranslateErrorCategory:
    """Map an exception plus HTTP status onto a category.

    456 is DeepL-specific and worth handling by number: it means the character
    quota is exhausted, which is a billing problem the operator must act on,
    not a transient failure to retry.
    """
    name = type(exc).__name__.lower()
    haystack = f"{exc} {body_text}".lower()

    if status in (401, 403) or "authentication" in name or "api key" in haystack:
        return TranslateErrorCategory.AUTHENTICATION
    if status == 456 or "quota" in haystack:
        return TranslateErrorCategory.QUOTA
    if status == 429 or "ratelimit" in name or "rate limit" in haystack:
        return TranslateErrorCategory.RATE_LIMIT
    if status == 404 or "notfound" in name:
        return TranslateErrorCategory.NOT_FOUND
    if status == 413 or "too long" in haystack or "too large" in haystack:
        return TranslateErrorCategory.TEXT_TOO_LONG
    if "language" in haystack and ("support" in haystack or "invalid" in haystack):
        return TranslateErrorCategory.UNSUPPORTED_LANGUAGE
    if status in {400, 409, 422} or "badrequest" in name:
        return TranslateErrorCategory.INVALID_REQUEST
    if status == 408 or "timeout" in name:
        return TranslateErrorCategory.TIMEOUT
    if "connect" in name:
        return TranslateErrorCategory.CONNECTION
    if status is not None and status >= 500:
        return TranslateErrorCategory.SERVER
    return TranslateErrorCategory.UNKNOWN


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
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
    if response is None:
        return ""
    try:
        return str(getattr(response, "text", "") or "")[:600]
    except Exception:
        return ""


def _provider_code(response: Any) -> Optional[str]:
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
        code = error.get("code") or error.get("type")
        return str(code) if code is not None else None
    code = payload.get("code")
    return str(code) if code is not None else None


def _detail(exc: BaseException, body_text: str) -> str:
    base = str(exc)
    return f"{base} :: {body_text}" if body_text and body_text not in base else base


# ---------------------------------------------------------------------------
# Provider protocols
# ---------------------------------------------------------------------------
#
# One per capability. A provider implements only what it serves and registers
# only into those registries, so an unsupported capability is unreachable
# rather than a method that raises.


@runtime_checkable
class TranslateProvider(Protocol):
    provider_name: str

    async def translate(self, req: TranslateRequest) -> TranslateResult: ...

    async def languages(self, *, target: bool = True) -> List[LanguageOption]: ...


@runtime_checkable
class TransliterateProvider(Protocol):
    provider_name: str

    async def transliterate(
        self, req: TransliterateRequest
    ) -> TransliterateResult: ...


@runtime_checkable
class DetectProvider(Protocol):
    provider_name: str

    async def detect(self, req: DetectRequest) -> DetectResult: ...


__all__ = [
    "DetectProvider",
    "DetectRequest",
    "DetectResult",
    "DetectedLanguage",
    "LanguageOption",
    "TranslateError",
    "TranslateErrorCategory",
    "TranslateProvider",
    "TranslateRequest",
    "TranslateResult",
    "TranslatedText",
    "TransliterateProvider",
    "TransliterateRequest",
    "TransliterateResult",
    "classify",
]
