"""Reading and writing media inside a workflow workspace.

Everything here funnels through :func:`nodes.filesystem._backend.resolve_within`,
which rejects ``..`` / ``~`` / drive-prefixed inputs before touching the
filesystem and then re-checks containment after resolution so a symlink or
Windows junction cannot redirect the result outside the workspace.

That containment is not decorative. Before this module existed, the Sarvam
speech-to-text node joined a user-supplied path onto the workspace root with
no check at all, so ``audio_file="../../credentials.db"`` read the encrypted
credential store and uploaded it to the provider. :func:`coerce_file_param`
closes that by construction, for every node that adopts it.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Optional, Tuple, get_args
from uuid import uuid4

from core.logging import get_logger
from services.media.inspect import inspect_audio
from services.media.limits import MEDIA_MAX_READ_BYTES
from services.media.refs import AudioRef, FileKind, FileRef

logger = get_logger(__name__)

AUDIO_SUBDIR = "audio"
UPLOAD_SUBDIR = "uploads"
MEDIA_SUBDIR = "media"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Windows treats these as devices no matter the extension, so a write to
# "CON.wav" is a write to the console. Every generated name carries a uuid
# suffix, which makes a bare reserved name unreachable, but the check stays
# as defence for callers that pass an unusual stem.
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _slugify(value: str, *, limit: int = 32) -> str:
    slug = _SLUG_RE.sub("-", (value or "").lower()).strip("-")[:limit].strip("-")
    if not slug or slug in _WINDOWS_RESERVED:
        return "audio"
    return slug


def workspace_root(
    ctx: Any = None,
    *,
    workspace_dir: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> Path:
    """Resolve the workspace directory for a context.

    Order: an explicit ``workspace_dir``, then the executor-injected
    ``ctx.workspace_dir``. Callers with neither get an error rather than a
    guess.

    ``workflow_id`` is accepted for call-site readability but is **not**
    sufficient on its own, and that is deliberate. Workspace directories are
    named by ``Workflow.slug`` (see ``WorkflowService._get_workspace_dir``),
    while an ``AudioRef`` deliberately carries the immutable ``workflow_id``
    so a reference survives a rename. Turning one into the other needs a
    database read, which cannot happen in a sync helper -- so the caller
    that has a database (the workspace HTTP route) does the lookup and
    passes the resolved directory in via ``workspace_dir``.

    An earlier version composed ``workspaces/<workflow_id>/`` here. That
    silently produced a path that never exists, because the directory on
    disk is ``workspaces/<slug>/``. It never fired in practice only because
    every caller happened to supply a ctx.
    """
    from services.plugin import NodeUserError

    if workspace_dir:
        return Path(workspace_dir)

    direct = getattr(ctx, "workspace_dir", None) if ctx is not None else None
    if not direct and ctx is not None:
        raw = getattr(ctx, "raw", None)
        if isinstance(raw, dict):
            direct = raw.get("workspace_dir")
    if direct:
        return Path(direct)

    if workflow_id is None and ctx is not None:
        workflow_id = getattr(ctx, "workflow_id", None)
    raise NodeUserError(
        "No workspace is available for this execution, so media cannot be "
        "read or written. Save the workflow and run it again."
        + (f" (workflow {workflow_id})" if workflow_id else "")
    )


def resolve_media(
    ref: FileRef | str,
    *,
    ctx: Any = None,
    workspace_dir: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> Path:
    """Resolve a ref or a path string to a contained absolute path."""
    from nodes.filesystem._backend import resolve_within
    from services.plugin import NodeUserError

    key = ref.path if isinstance(ref, FileRef) else str(ref or "")
    if not key.strip():
        raise NodeUserError("No media path was provided.")

    if isinstance(ref, FileRef) and ref.workflow_id and workflow_id is None:
        workflow_id = ref.workflow_id

    root = workspace_root(ctx, workspace_dir=workspace_dir, workflow_id=workflow_id)

    candidate = Path(key)
    if candidate.is_absolute():
        # Tolerated for back-compat with nodes that stored absolute paths
        # before AudioRef existed, but still contained: an absolute path
        # outside the workspace is refused rather than read.
        #
        # Both sides are resolved before comparing, for the reason spelled
        # out in ``resolve_within``: a resolved candidate measured against an
        # unresolved root refuses valid files whenever the root sits under a
        # symlink.
        root_resolved = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        if not resolved_candidate.is_relative_to(root_resolved):
            raise NodeUserError(
                f"'{candidate}' is outside this workflow's workspace."
            )
        key = resolved_candidate.relative_to(root_resolved).as_posix()

    try:
        return resolve_within(root, key)
    except ValueError as exc:
        raise NodeUserError(f"'{key}' is outside this workflow's workspace.") from exc


def read_media_bytes(
    ref: FileRef | str,
    *,
    ctx: Any = None,
    workspace_dir: Optional[str] = None,
    workflow_id: Optional[str] = None,
    max_bytes: int = MEDIA_MAX_READ_BYTES,
) -> Tuple[str, bytes]:
    """Return ``(filename, bytes)`` for a contained media reference."""
    from services.plugin import NodeUserError

    target = resolve_media(
        ref, ctx=ctx, workspace_dir=workspace_dir, workflow_id=workflow_id
    )
    if not target.is_file():
        raise NodeUserError(f"Media file not found: {target.name}")

    size = target.stat().st_size
    if size == 0:
        raise NodeUserError(f"Media file is empty: {target.name}")
    if size > max_bytes:
        raise NodeUserError(
            f"{target.name} is {size // (1024 * 1024)} MB; the limit is "
            f"{max_bytes // (1024 * 1024)} MB."
        )
    return target.name, target.read_bytes()


def _persist(
    payload: bytes,
    *,
    ctx: Any,
    stem: str,
    ext: str,
    subdir: str,
    empty_message: str,
) -> Tuple[str, str, Path, Optional[str]]:
    """Atomically place bytes in the workspace.

    Returns ``(rel_path, filename, absolute_target, workflow_id)``. Shared by
    :func:`write_audio` and :func:`write_media` so both produce byte-identical
    naming, containment and atomicity -- the filename shape
    ``<stem>-<node8>-<rand6>.<ext>`` carries a random suffix precisely so
    retries and repeated runs never collide or silently overwrite.
    """
    from nodes.filesystem._backend import atomic_write_bytes
    from services.plugin import NodeUserError

    if not payload:
        raise NodeUserError(empty_message)

    root = workspace_root(ctx)
    node_id = str(getattr(ctx, "node_id", "") or "node")
    name = f"{_slugify(stem)}-{node_id[:8]}-{uuid4().hex[:6]}.{ext.lstrip('.')}"
    rel = f"{subdir}/{name}"

    target = resolve_media(rel, ctx=ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, payload, root_dir=root)

    return rel, name, target, getattr(ctx, "workflow_id", None)


def write_audio(
    payload: bytes,
    *,
    ctx: Any,
    stem: str,
    ext: str,
    mime_type: Optional[str] = None,
    subdir: str = AUDIO_SUBDIR,
    inspect: bool = True,
    sample_rate: Optional[int] = None,
    channels: int = 1,
) -> AudioRef:
    """Atomically write audio into the workspace and return a reference.

    The filename shape (``<stem>-<node8>-<rand6>.<ext>``) matches what the
    Sarvam TTS node produced before this helper existed, so porting it is
    behaviour-preserving. The random suffix also means retries and repeated
    runs never collide or silently overwrite earlier audio.

    Returns an ``AudioRef``, i.e. it *claims* the container was probed. Use
    :func:`write_media` for anything whose duration you have not measured --
    see the note there.
    """
    rel, name, target, workflow_id = _persist(
        payload,
        ctx=ctx,
        stem=stem,
        ext=ext,
        subdir=subdir,
        empty_message="Refusing to write an empty audio file.",
    )

    probe = (
        inspect_audio(
            target,
            declared_format=ext,
            pcm_sample_rate=sample_rate,
            pcm_channels=channels,
        )
        if inspect
        else None
    )

    return AudioRef(
        path=rel,
        workflow_id=workflow_id,
        filename=name,
        mime_type=mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
        format=ext.lstrip(".").lower(),
        size_bytes=len(payload),
        duration_seconds=probe.duration_seconds if probe else None,
        sample_rate=(probe.sample_rate if probe else None) or sample_rate,
        channels=(probe.channels if probe else None),
        sha256=hashlib.sha256(payload).hexdigest(),
        url=workspace_file_url(workflow_id, rel),
    )


def write_media(
    payload: bytes,
    *,
    ctx: Any,
    stem: str,
    ext: str,
    kind: FileKind = "file",
    mime_type: Optional[str] = None,
    subdir: str = MEDIA_SUBDIR,
) -> FileRef:
    """Atomically write any file into the workspace and return a reference.

    The kind-agnostic sibling of :func:`write_audio`: same naming, same
    containment, same atomic write, but it makes no claim about the
    container beyond what the caller declares.

    ``kind`` narrows what the reference asserts, and the honest default is
    ``"file"``. Do **not** pass ``"audio"`` here -- that value asserts the
    duration/rate fields came from :func:`inspect_audio`, and a fabricated
    duration silently mis-bills per-second providers downstream. Reach for
    :func:`write_audio` when you want that claim, which measures rather than
    guesses. ``image`` / ``video`` / ``document`` assert nothing beyond a
    rendering hint, so they are safe to declare from a MIME type.
    """
    from services.plugin import NodeUserError

    if kind == "audio":
        raise NodeUserError(
            "write_media cannot produce kind='audio': that claims the container "
            "was probed. Use write_audio, which measures the duration."
        )

    rel, name, _target, workflow_id = _persist(
        payload,
        ctx=ctx,
        stem=stem,
        ext=ext,
        subdir=subdir,
        empty_message="Refusing to write an empty file.",
    )

    return FileRef(
        kind=kind,
        path=rel,
        workflow_id=workflow_id,
        filename=name,
        mime_type=mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        url=workspace_file_url(workflow_id, rel),
    )


def workspace_file_url(workflow_id: Optional[str], rel_path: str) -> Optional[str]:
    """Path-only URL for the workspace file route. No scheme, no host."""
    if not workflow_id:
        return None
    from urllib.parse import quote

    return f"/api/workspace/{quote(str(workflow_id))}/files/{quote(rel_path)}"


def coerce_file_param(
    value: Any,
    *,
    ctx: Any = None,
    max_bytes: int = MEDIA_MAX_READ_BYTES,
) -> Tuple[str, bytes]:
    """Read a file parameter in any of the three shapes the UI can produce.

    1. A serialized :class:`AudioRef` -- what the upload route now returns.
    2. The legacy ``{"type": "upload", "data": "<base64>"}`` envelope the
       file widget used to emit. Still accepted indefinitely: saved
       workflow rows carry it and are not migrated. Logs one warning
       naming the node so operators can find and re-save them.
    3. A bare path string, typed by the user or dragged from an upstream
       node -- now resolved with containment.
    """
    import base64
    import binascii

    from pydantic import ValidationError

    from services.plugin import NodeUserError

    if isinstance(value, dict) and value.get("kind") in get_args(FileKind):
        # The model is picked explicitly rather than always validating as the
        # base: ``extra="forbid"`` means an audio payload does NOT validate as
        # a plain FileRef (it carries duration_seconds/sample_rate/channels).
        model = AudioRef if value.get("kind") == "audio" else FileRef
        try:
            ref = model.model_validate(value)
        except ValidationError as exc:
            # A well-known ``kind`` with a malformed body is still a bad file
            # parameter, not a server fault: keep it on the NodeUserError path
            # so it logs one WARN line instead of a pydantic traceback.
            raise NodeUserError(
                "File parameter is not a usable file reference. "
                "Re-select the file in the parameter panel."
            ) from exc
        return read_media_bytes(ref, ctx=ctx, max_bytes=max_bytes)

    if isinstance(value, dict) and value.get("type") == "upload":
        data = value.get("data") or ""
        if not data:
            raise NodeUserError(
                "The uploaded file carried no data. Re-select it in the "
                "parameter panel."
            )
        logger.warning(
            "legacy base64 upload parameter; re-select the file to migrate it",
            node_id=str(getattr(ctx, "node_id", "") or "unknown"),
            characters=len(data),
        )
        try:
            blob = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NodeUserError(f"Uploaded file is not valid base64: {exc}") from exc
        if len(blob) > max_bytes:
            raise NodeUserError(
                f"Uploaded file is {len(blob) // (1024 * 1024)} MB; the limit "
                f"is {max_bytes // (1024 * 1024)} MB."
            )
        if not blob:
            raise NodeUserError("Uploaded file is empty.")
        return str(value.get("filename") or "upload.bin"), blob

    if isinstance(value, dict):
        raise NodeUserError(
            "File parameter is an object but not a recognised file reference. "
            "Re-select the file in the parameter panel."
        )

    text = str(value or "").strip()
    if not text:
        raise NodeUserError("No file was provided.")
    return read_media_bytes(text, ctx=ctx, max_bytes=max_bytes)


__all__ = [
    "AUDIO_SUBDIR",
    "MEDIA_SUBDIR",
    "UPLOAD_SUBDIR",
    "coerce_file_param",
    "read_media_bytes",
    "resolve_media",
    "workspace_file_url",
    "workspace_root",
    "write_audio",
    "write_media",
]
