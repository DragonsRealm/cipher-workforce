"""Dispatch and error translation for the three text-language nodes.

Same shape as ``nodes/speech/_unifier.py``: look the spec up in the matching
registry, build a client per call, run it, normalize every failure into
``NodeUserError`` at one catch site, close in ``finally``. No client cache,
for the same reason — one call per node execution.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional, TypeVar

from core.logging import get_logger
from services.plugin import NodeUserError
from services.provider_registry import ProviderSpec

from ._protocol import (
    DetectRequest,
    DetectResult,
    LanguageOption,
    TranslateError,
    TranslateErrorCategory,
    TranslateRequest,
    TranslateResult,
    TransliterateRequest,
    TransliterateResult,
)
from ._registry import (
    get_detect_provider,
    get_translate_provider,
    get_transliterate_provider,
)

logger = get_logger(__name__)

T = TypeVar("T")


async def translate(
    *, provider: str, api_key: str, request: TranslateRequest, translate_errors: bool = True
) -> TranslateResult:
    return await _dispatch(
        get_translate_provider(provider),
        provider,
        api_key,
        lambda client: client.translate(request),
        operation="translation",
        translate_errors=translate_errors,
    )


async def transliterate(
    *,
    provider: str,
    api_key: str,
    request: TransliterateRequest,
    translate_errors: bool = True,
) -> TransliterateResult:
    return await _dispatch(
        get_transliterate_provider(provider),
        provider,
        api_key,
        lambda client: client.transliterate(request),
        operation="transliteration",
        translate_errors=translate_errors,
    )


async def detect(
    *, provider: str, api_key: str, request: DetectRequest, translate_errors: bool = True
) -> DetectResult:
    return await _dispatch(
        get_detect_provider(provider),
        provider,
        api_key,
        lambda client: client.detect(request),
        operation="language detection",
        translate_errors=translate_errors,
    )


async def languages(
    *, provider: str, api_key: str, target: bool = True
) -> List[LanguageOption]:
    """Selectable languages for a translation provider.

    Providers without a live endpoint return their configured list, so the
    dropdown loader never branches on whether one exists.
    """
    return await _dispatch(
        get_translate_provider(provider),
        provider,
        api_key,
        lambda client: client.languages(target=target),
        operation="language listing",
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
    client: Optional[object] = None
    try:
        client = spec.factory(api_key=api_key, **spec.client_kwargs)
        return await call(client)
    except spec.sdk_exception_types as exc:
        error = TranslateError.from_exception(provider, exc)
        if not translate_errors:
            raise error from exc
        logger.warning(
            "translate provider request failed",
            provider=error.provider,
            operation=operation,
            category=error.category.value,
            retryable=error.retryable,
            status_code=error.status_code,
            provider_code=error.provider_code,
        )
        raise NodeUserError(error.user_message) from error
    except TranslateError as exc:
        # A provider classified a failure itself — typically a documented
        # vendor error that arrived with HTTP 200.
        if not translate_errors:
            raise
        logger.warning(
            "translate provider returned a failure payload",
            provider=exc.provider,
            operation=operation,
            category=exc.category.value,
        )
        raise NodeUserError(exc.user_message) from exc
    except (ValueError, TypeError, OSError) as exc:
        # Only normalize failures raised while CONSTRUCTING the client; once
        # one exists an unexpected exception is a programming error and keeps
        # its traceback.
        if client is not None:
            raise
        category = (
            TranslateErrorCategory.CONNECTION
            if isinstance(exc, OSError)
            else TranslateErrorCategory.INVALID_REQUEST
        )
        error = TranslateError(
            message=str(exc),
            provider=provider,
            category=category,
            retryable=category == TranslateErrorCategory.CONNECTION,
        )
        if not translate_errors:
            raise error from exc
        raise NodeUserError(error.user_message) from error
    finally:
        if client is not None:
            await _close(client)


async def _close(client: object) -> None:
    close = getattr(client, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:
        logger.warning("failed to close translate client", error=str(exc))


__all__ = ["detect", "languages", "translate", "transliterate"]
