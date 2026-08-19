"""What a browser may render in place, and what must download.

One rule with two consumers: :mod:`routers.workspace` uses it to choose a
``Content-Disposition``, and the gallery listing uses it to tell the panel
which entries are previewable. That is the whole reason it lives here
rather than in either caller — if the two ever disagreed, the panel would
open a player for a file the route forces to download, and the user would
see a dead frame with no explanation.

The client is deliberately not trusted to re-derive this from a mime type.
It would be a second copy of a security decision, and copies drift.
"""

from __future__ import annotations

from typing import Literal, Optional

PreviewKind = Literal["image", "audio", "video", "pdf", "none"]

# Types a browser may render in place. Everything else downloads.
#
# `text/html` and `image/svg+xml` are excluded deliberately and must stay
# excluded: `shell`, `fileDownloader` and `fileModify` can all write
# arbitrary files into a workspace, so serving attacker-authored markup
# inline from the app origin would be stored XSS with access to the session
# cookie. Both types are script-bearing.
#
# `application/pdf` is an exact-match addition (Canvas node): the browser's
# built-in PDF viewer renders it isolated from the page, so the exposure
# class is the same as images — not the markup-from-app-origin class
# NEVER_INLINE guards.
INLINE_PREFIXES = ("audio/", "image/", "video/")
INLINE_EXACT = frozenset({"application/pdf"})
NEVER_INLINE = frozenset(
    {"image/svg+xml", "text/html", "text/xml", "application/xhtml+xml"}
)


def serves_inline(mime_type: Optional[str]) -> bool:
    """Whether the workspace route will serve this with ``inline`` disposition."""
    if not mime_type:
        return False
    return (
        mime_type.startswith(INLINE_PREFIXES) or mime_type in INLINE_EXACT
    ) and mime_type not in NEVER_INLINE


def preview_kind(mime_type: Optional[str]) -> PreviewKind:
    """Which element can display this file, or ``"none"``.

    Gated on :func:`serves_inline` first, so a type the route refuses to
    send inline can never be reported as previewable.
    """
    if not serves_inline(mime_type) or not mime_type:
        return "none"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type == "application/pdf":
        return "pdf"
    return "none"


__all__ = [
    "INLINE_EXACT",
    "INLINE_PREFIXES",
    "NEVER_INLINE",
    "PreviewKind",
    "preview_kind",
    "serves_inline",
]
