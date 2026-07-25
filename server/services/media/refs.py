"""``FileRef`` / ``AudioRef`` — a reference to a file, never the file itself.

The one rule this module exists to enforce: **file bytes do not travel
through the workflow engine.** See :mod:`services.media.limits` for the
measured reason. These models have no bytes field, no base64 field, and
``extra="forbid"`` so that adding one is a validation error rather than a
silent regression.

``FileRef`` is the base every kind shares; ``AudioRef`` narrows it with the
container metadata a probe produced. Kind-specific siblings (image, video,
document) subclass the same base as they are needed — see
``docs-internal/media_transport.md``, which rejects one fat generic model
because the metadata that matters differs per kind.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# The narrowing a producer may claim. ``file`` is the honest default: it
# asserts nothing beyond "this exists in the workspace". A node that has not
# probed the container must not claim a richer kind — a fabricated duration
# silently mis-bills per-second providers downstream.
FileKind = Literal["file", "audio", "image", "video", "document"]


class FileRef(BaseModel):
    """A pointer to a file inside a workflow workspace.

    Serializes to roughly 400 bytes, i.e. about 5,200 refs before
    approaching Temporal's 2 MiB error limit -- the envelope is
    structurally incapable of getting near it.

    ``path`` is workspace-**relative** POSIX, never an absolute host path.
    Absolute paths embed the mutable workflow slug, leak the operator's
    home directory into the database / WebSocket broadcasts / LLM context,
    and cannot be safely turned into an HTTP URL. A leading slash is also
    not merely cosmetic: ``resolve_media`` treats it as absolute on POSIX
    (and not on Windows), so the wrong shape fails only in production.
    """

    kind: FileKind = "file"

    path: str = Field(
        description="Workspace-relative POSIX path, no leading slash "
        "(e.g. 'audio/greeting-1a2b3c.wav').",
    )
    # Stable across renames -- the workspace directory is keyed on the
    # slug, which changes, while the id does not. Lets the resolver and
    # the file-serving route find the workspace without a NodeContext.
    workflow_id: Optional[str] = None

    filename: str = Field(description="Display name. Never used for resolution.")
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0

    # ISO 8601. Advisory only -- a listing snapshot, not a cache key.
    modified_at: Optional[str] = None

    sha256: Optional[str] = None

    # Path-only, no scheme or host, so the frontend can prepend its own
    # base via buildApiUrl() when it points at a remote backend.
    # Advisory: `path` + `workflow_id` remain canonical.
    url: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class AudioRef(FileRef):
    """A :class:`FileRef` whose container has actually been probed.

    Only set ``kind="audio"`` when the duration/rate fields come from
    :func:`services.media.inspect.inspect_audio`. Guessing the kind from a
    file extension is what ``FileRef`` is for.
    """

    kind: Literal["audio"] = "audio"

    format: str = Field(default="", description="Container/codec: wav, mp3, opus, ...")

    # None whenever the container could not be inspected. Never guessed --
    # a fabricated duration would silently mis-bill per-second providers.
    duration_seconds: Optional[float] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


__all__ = ["AudioRef", "FileKind", "FileRef"]
