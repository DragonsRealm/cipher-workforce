"""Dispatch and error translation for the two speech nodes.

Reads the registries to route a call to the right provider, and normalizes
every provider failure into ``NodeUserError`` at one catch site. The nodes
never touch a provider class.

**No client cache here, deliberately.** ``ChatUnifier`` caches SDK clients
because an agent loop makes many model calls inside one node execution, so
connection reuse is worth a lease-counted LRU. Speech is the opposite shape:
a node execution issues exactly one HTTP request, and the client setup cost
is invisible next to a multi-second synthesis. Caching would buy nothing and
cost a process-wide singleton plus a shutdown hook wired into ``main.py`` --
core coupling this plugin has no reason to need. A client is built per call
and closed in ``finally``.

That also settles the question left open when the generic registry was
extracted: there is no second consumer for a shared client cache, so none
was built.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional, TypeVar

from core.logging import get_logger
from services.plugin import NodeUserError
from services.provider_registry import ProviderSpec

from ._protocol import (
    SpeechError,
    SpeechErrorCategory,
    SttRequest,
    SttResult,
    TtsRequest,
    TtsResult,
    Voice,
)
from ._registry import get_stt_provider, get_tts_provider

logger = get_logger(__name__)

T = TypeVar("T")


async def synthesize(
    *,
    provider: str,
    api_key: str,
    request: TtsRequest,
    translate_errors: bool = True,
) -> TtsResult:
    """Synthesize speech via the named provider."""
    spec = get_tts_provider(provider)
    return await _dispatch(
        spec,
        provider,
        api_key,
        lambda client: client.synthesize(request),
        operation="synthesis",
        translate_errors=translate_errors,
    )


async def transcribe(
    *,
    provider: str,
    api_key: str,
    request: SttRequest,
    translate_errors: bool = True,
) -> SttResult:
    """Transcribe audio via the named provider."""
    spec = get_stt_provider(provider)
    return await _dispatch(
        spec,
        provider,
        api_key,
        lambda client: client.transcribe(request),
        operation="transcription",
        translate_errors=translate_errors,
    )


async def list_voices(*, provider: str, api_key: str) -> List[Voice]:
    """List selectable voices for a synthesis provider.

    Providers without a live catalogue return their configured static list,
    so the dropdown loader never branches on whether an endpoint exists.
    """
    spec = get_tts_provider(provider)
    return await _dispatch(
        spec,
        provider,
        api_key,
        lambda client: client.list_voices(),
        operation="voice listing",
        translate_errors=True,
    )


async def _dispatch(
    spec: ProviderSpec,
    provider: str,
    api_key: str,
    call: Callable[[object], Awaitable[T]],
    *,
    operation: str,
    translate_errors: bool,
) -> T:
    """Build a client, run ``call``, normalize failures, always close.

    One catch site for all three entry points. ``translate_errors=False``
    re-raises the structured :class:`SpeechError` instead of a flattened
    user message, which is what a Temporal retry policy needs to read
    ``retryable``.
    """
    client: Optional[object] = None
    try:
        client = spec.factory(api_key=api_key, **spec.client_kwargs)
        return await call(client)
    except spec.sdk_exception_types as exc:
        error = SpeechError.from_exception(provider, exc)
        if not translate_errors:
            raise error from exc
        logger.warning(
            "speech provider request failed",
            provider=error.provider,
            operation=operation,
            category=error.category.value,
            retryable=error.retryable,
            status_code=error.status_code,
            provider_code=error.provider_code,
            request_id=error.request_id,
        )
        raise NodeUserError(error.user_message) from error
    except SpeechError as exc:
        # A provider classified a failure itself -- typically a documented
        # vendor error that arrived with HTTP 200, which both Deepgram and
        # Sarvam can do. Already structured, so just surface it.
        if not translate_errors:
            raise
        logger.warning(
            "speech provider returned a failure payload",
            provider=exc.provider,
            operation=operation,
            category=exc.category.value,
        )
        raise NodeUserError(exc.user_message) from exc
    except (ValueError, TypeError, OSError) as exc:
        # Only normalize failures raised while *constructing* the client.
        # Once one exists, an unexpected exception from provider logic is a
        # programming error and keeps its traceback.
        if client is not None:
            raise
        category = (
            SpeechErrorCategory.CONNECTION
            if isinstance(exc, OSError)
            else SpeechErrorCategory.INVALID_REQUEST
        )
        error = SpeechError(
            message=str(exc),
            provider=provider,
            category=category,
            retryable=category == SpeechErrorCategory.CONNECTION,
        )
        if not translate_errors:
            raise error from exc
        logger.warning(
            "speech client construction failed",
            provider=error.provider,
            operation=operation,
            category=error.category.value,
        )
        raise NodeUserError(error.user_message) from error
    finally:
        if client is not None:
            await _close(client)


async def _close(client: object) -> None:
    """Close a provider if it holds anything. Probed, not required."""
    close = getattr(client, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:
        logger.warning("failed to close speech client", error=str(exc))


__all__ = ["list_voices", "synthesize", "transcribe"]
