"""Screenshot persistence shared by the two browser plugins.

Both browser nodes historically leaked screenshots in shapes the platform
cannot use: ``browser`` returned whatever agent-browser printed (a base64
blob — a media-contract violation that also hits the 100 KB CLI-output
truncation), and ``browserHarness`` returned an absolute path under its
daemon tmp dir, which no HTTP route can serve. These helpers land the bytes
in the workflow workspace via ``write_media(kind="image")`` so a screenshot
becomes a ~400 B ``FileRef`` the Canvas board / gallery can display.

Every helper is TOLERANT by contract: an unrecognized payload shape, a
missing workspace, or a write failure logs one warning and returns ``None``
so the browser operation itself never breaks over persistence.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.logging import get_logger

logger = get_logger(__name__)

# Keys agent-browser plausibly uses for inline image data (only "base64" is
# evidenced in-repo; the rest are tolerated probes) and for a saved file.
_BASE64_KEYS = ("base64", "data", "screenshot", "image")
_PATH_KEYS = ("path", "file", "filename")

# A real screenshot is never this small; refuse to "persist" garbage.
_MIN_IMAGE_BYTES = 128
_MAX_IMAGE_BYTES = 25 * 1024 * 1024

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _write_image(payload: bytes, ctx: Any, *, ext: str, mime: str) -> Optional[Dict[str, Any]]:
    from services.media.workspace import write_media

    if not getattr(ctx, "workspace_dir", None):
        logger.warning("[Browser] No workspace on this run; screenshot not persisted")
        return None
    if not (_MIN_IMAGE_BYTES <= len(payload) <= _MAX_IMAGE_BYTES):
        logger.warning(
            "[Browser] Screenshot payload size %d outside sane bounds; not persisted",
            len(payload),
        )
        return None
    try:
        ref = write_media(
            payload,
            ctx=ctx,
            stem="screenshot",
            ext=ext,
            kind="image",
            mime_type=mime,
        )
    except Exception as exc:  # persistence must never fail the browser op
        logger.warning("[Browser] Could not persist screenshot: %s", exc)
        return None
    return ref.model_dump(mode="json")


def persist_screenshot_from_payload(
    data: Any, ctx: Any, *, fmt: str = "png"
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """agent-browser screenshot payload -> ``(serialized FileRef, consumed_key)``.

    Probes the known inline-base64 keys first, then saved-file-path keys.
    ``consumed_key`` names the bulky field the caller should drop from the
    payload so no base64 rides the node output. ``(None, None)`` on any
    unrecognized shape — the raw payload then passes through unchanged.
    """
    if not isinstance(data, dict):
        return None, None

    ext = ".jpg" if fmt == "jpeg" else f".{fmt or 'png'}"
    mime = _EXT_MIME.get(ext, "image/png")

    for key in _BASE64_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or len(value) < 200:
            continue
        try:
            payload = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            continue
        ref = _write_image(payload, ctx, ext=ext, mime=mime)
        return (ref, key) if ref else (None, None)

    for key in _PATH_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value.strip())
        if not candidate.is_absolute() or not candidate.is_file():
            continue
        suffix = candidate.suffix.lower()
        if suffix not in _EXT_MIME:
            continue
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            logger.warning("[Browser] Could not read screenshot file %s: %s", candidate, exc)
            return None, None
        ref = _write_image(payload, ctx, ext=suffix, mime=_EXT_MIME[suffix])
        # The path itself is small; nothing bulky to drop.
        return (ref, None) if ref else (None, None)

    return None, None


def persist_screenshot_file(
    path_text: str, ctx: Any, *, contained_under: Path
) -> Optional[Dict[str, Any]]:
    """A harness-printed screenshot path -> serialized FileRef.

    Reads ONLY files contained under the harness runtime dir — the printed
    path is process output, not a trusted input, so this is not a generic
    read-any-path helper.
    """
    candidate = Path(str(path_text or "").strip())
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        root = contained_under.resolve(strict=False)
        if not resolved.is_relative_to(root):
            logger.warning(
                "[BrowserHarness] Screenshot path escapes the harness dir; not persisted"
            )
            return None
    except OSError:
        return None
    if not resolved.is_file():
        return None
    suffix = resolved.suffix.lower()
    mime = _EXT_MIME.get(suffix)
    if mime is None:
        return None
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        logger.warning("[BrowserHarness] Could not read screenshot %s: %s", resolved, exc)
        return None
    return _write_image(payload, ctx, ext=suffix, mime=mime)


__all__ = ["persist_screenshot_file", "persist_screenshot_from_payload"]
