"""``AudioRef`` — a reference to audio, never the audio itself.

The one rule this module exists to enforce: **audio bytes do not travel
through the workflow engine.** See :mod:`services.media.limits` for the
measured reason. This model has no bytes field, no base64 field, and
``extra="forbid"`` so that adding one is a validation error rather than a
silent regression.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AudioRef(BaseModel):
    """A pointer to an audio file inside a workflow workspace.

    Serializes to roughly 400 bytes, i.e. about 5,200 refs before
    approaching Temporal's 2 MiB error limit -- the envelope is
    structurally incapable of getting near it.

    ``path`` is workspace-**relative** POSIX, never an absolute host path.
    Absolute paths embed the mutable workflow slug, leak the operator's
    home directory into the database / WebSocket broadcasts / LLM context,
    and cannot be safely turned into an HTTP URL.
    """

    kind: Literal["audio"] = "audio"

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
    format: str = Field(default="", description="Container/codec: wav, mp3, opus, ...")
    size_bytes: int = 0

    # None whenever the container could not be inspected. Never guessed --
    # a fabricated duration would silently mis-bill per-second providers.
    duration_seconds: Optional[float] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None

    sha256: Optional[str] = None

    # Path-only, no scheme or host, so the frontend can prepend its own
    # base via buildApiUrl() when it points at a remote backend.
    # Advisory: `path` + `workflow_id` remain canonical.
    url: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


__all__ = ["AudioRef"]
