"""Media transport — references, not bytes.

Vendor-neutral and deliberately not part of ``services/speech``: the same
envelope is consumed by the file widget, the workspace HTTP routes, and
any node that produces or accepts a file. Owning it from a provider
package would invert the dependency.

The contract in one line: **a node returns an**
:class:`~services.media.refs.AudioRef`, **never audio bytes.** See
:mod:`services.media.limits` for the measured reason.
"""

from services.media.fetch import fetch_to_workspace
from services.media.inspect import AudioProbe, inspect_audio
from services.media.limits import (
    MEDIA_MAX_AUDIO_SECONDS,
    MEDIA_MAX_READ_BYTES,
    MEDIA_MAX_UPLOAD_BYTES,
    TEMPORAL_PAYLOAD_ERROR_BYTES,
    TEMPORAL_PAYLOAD_WARN_BYTES,
)
from services.media.refs import AudioRef, FileKind, FileRef
from services.media.workspace import (
    AUDIO_SUBDIR,
    MEDIA_SUBDIR,
    UPLOAD_SUBDIR,
    coerce_file_param,
    read_media_bytes,
    resolve_media,
    workspace_file_url,
    workspace_root,
    write_audio,
    write_media,
)

__all__ = [
    "AUDIO_SUBDIR",
    "MEDIA_SUBDIR",
    "UPLOAD_SUBDIR",
    "AudioProbe",
    "AudioRef",
    "FileKind",
    "FileRef",
    "MEDIA_MAX_AUDIO_SECONDS",
    "MEDIA_MAX_READ_BYTES",
    "MEDIA_MAX_UPLOAD_BYTES",
    "TEMPORAL_PAYLOAD_ERROR_BYTES",
    "TEMPORAL_PAYLOAD_WARN_BYTES",
    "coerce_file_param",
    "fetch_to_workspace",
    "inspect_audio",
    "read_media_bytes",
    "resolve_media",
    "workspace_file_url",
    "workspace_root",
    "write_audio",
    "write_media",
]
