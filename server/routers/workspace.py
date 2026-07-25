"""Per-workflow workspace file access.

Two routes: serve a file out of a workflow's workspace, and accept an
upload into it. Both exist because audio cannot travel through the workflow
engine as bytes (see ``services/media/limits.py``) — the UI needs a URL to
play generated audio from, and the file parameter needs somewhere to put an
uploaded clip other than base64 inside the node's parameters.

**The id/slug asymmetry is the thing to understand here.** The URL carries
``workflow_id`` because an ``AudioRef`` deliberately stores the immutable id,
so a reference keeps working after the workflow is renamed. But the
directory on disk is named by ``Workflow.slug``, which the rename path
moves. This router owns the id → slug lookup, because it is the layer that
has a database; ``services.media`` stays synchronous and takes the resolved
directory.

Containment is delegated to ``resolve_within``, which rejects ``..`` / ``~``
/ drive-prefixed input before touching the filesystem and re-checks
containment after resolution so a symlink or Windows junction cannot
redirect the result outside the root.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, HTTPException, Path as PathParam, UploadFile
from fastapi.responses import FileResponse

from core.container import container
from core.database import Database
from core.logging import get_logger
from services.media import MEDIA_MAX_UPLOAD_BYTES, AudioRef, write_audio
from services.media.preview import serves_inline
from services.media.workspace import UPLOAD_SUBDIR

logger = get_logger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

# Bounded so a hostile Content-Length cannot make us allocate; the running
# total is what enforces the cap, not the declared length.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _db() -> Database:
    return container.database()


async def _workspace_root(workflow_id: str, database: Database) -> Path:
    """Resolve a workflow id to its on-disk workspace directory.

    Delegates to :mod:`services.workspace_locator`, which owns the id->slug
    translation for every consumer. Both routes here are reads, so the
    ``"default"`` fallback (the anonymous workspace a one-off run without a
    saved row writes into) stays enabled.
    """
    from services.workspace_locator import resolve_workspace_root

    return await resolve_workspace_root(workflow_id, database)


def _resolve(root: Path, rel_path: str) -> Path:
    """Contain ``rel_path`` under ``root`` or 404.

    404 rather than 403 throughout: a different status for "exists but
    forbidden" would confirm the existence of files outside the workspace.
    """
    from nodes.filesystem._backend import resolve_within

    try:
        target = resolve_within(root, rel_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return target


@router.get("/{workflow_id}/files/{file_path:path}")
async def serve_workspace_file(
    workflow_id: str = PathParam(..., min_length=1, max_length=128),
    file_path: str = PathParam(..., min_length=1),
    database: Database = Depends(_db),
) -> FileResponse:
    """Serve one file from a workflow's workspace.

    Range requests work without any code here: Starlette's ``FileResponse``
    already implements ``Accept-Ranges``, ``206``, ``Content-Range``,
    ``If-Range`` and ``416``. Wrapping this in a ``StreamingResponse`` would
    lose all of that and break seeking in an ``<audio>`` element.
    """
    root = await _workspace_root(workflow_id, database)
    target = _resolve(root, file_path)

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    disposition = "inline" if serves_inline(media_type) else "attachment"

    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{target.name}"',
            # Workspace files are mutable, so no immutable caching. The
            # filename already carries a random suffix, which makes a short
            # revalidating cache safe.
            "Cache-Control": "private, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{workflow_id}/uploads")
async def upload_workspace_file(
    workflow_id: str = PathParam(..., min_length=1, max_length=128),
    file: UploadFile = File(...),
    database: Database = Depends(_db),
) -> AudioRef:
    """Accept a file into the workflow's workspace and return a reference.

    Read in bounded chunks with a running total rather than
    ``await file.read()``: the declared ``Content-Length`` is attacker
    controlled, so the only trustworthy limit is what has actually been
    read. Exceeding the cap aborts immediately, before the whole body has
    been received.

    Returns an ``AudioRef`` — the shape ``coerce_file_param`` already
    documents as "what the upload route now returns", so the file parameter
    on a node accepts it with no further plumbing.
    """
    root = await _workspace_root(workflow_id, database)

    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MEDIA_MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload exceeds the {MEDIA_MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                    "limit."
                ),
            )
        digest.update(chunk)
        chunks.append(chunk)

    if not total:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    payload = b"".join(chunks)
    name = Path(file.filename or "upload.bin").name
    stem, _, ext = name.rpartition(".")

    # write_audio owns filename sanitization (Windows reserved device names,
    # slugging, a random suffix so nothing collides or overwrites) and the
    # atomic write. Reimplementing any of that here would be a second, worse
    # copy.
    ref = write_audio(
        payload,
        ctx=SimpleNamespace(
            workspace_dir=str(root), node_id="upload", workflow_id=workflow_id
        ),
        stem=stem or name,
        ext=ext or "bin",
        mime_type=file.content_type or None,
        subdir=UPLOAD_SUBDIR,
    )

    logger.info(
        "workspace upload stored",
        workflow_id=workflow_id,
        path=ref.path,
        size_bytes=ref.size_bytes,
    )
    return ref
