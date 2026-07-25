"""Workspace listing — the shared body behind the node op and the WS handlers.

Enumeration itself is delegated to :class:`WorkspaceBackend`, which already
filters symlink escapes through ``_safe_child``. This module owns the two
things the backend does not: the **wire shape** and the **caps**.

The wire shape is the load-bearing part. ``WorkspaceBackend._file_info``
emits ``"/audio/x.wav"`` with a leading slash, but every consumer of a file
reference wants it relative:

    POSIX  : Path('/audio/x.wav').is_absolute() -> True
    Windows: Path('/audio/x.wav').is_absolute() -> False

so ``resolve_media`` takes its absolute-path branch on Linux only, fails
``relative_to(root)``, and raises "outside this workflow's workspace" — while
the same path works on a Windows dev box. A leading slash is therefore a
production-only bug, and it is converted here rather than in ``_file_info``,
whose current shape ``fsSearch`` output and its LLM-facing prompt depend on.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bounds the response, not the walk: ls_info materialises the directory
# before we slice it. The only in-repo precedent is the MCP tool's 1000.
WORKSPACE_LIST_LIMIT = 1000
WORKSPACE_LIST_DEFAULT = 500


def _relative(virtual_path: str) -> str:
    """``/audio/x.wav`` or ``/audio/`` -> ``audio/x.wav`` / ``audio``."""
    return virtual_path.strip("/")


def _entry_url(workflow_id: Optional[str], rel_path: str, is_dir: bool) -> Optional[str]:
    if is_dir or not workflow_id:
        return None
    from services.media.workspace import workspace_file_url

    return workspace_file_url(workflow_id, rel_path)


def _to_row(info: Dict[str, Any], workflow_id: Optional[str]) -> Dict[str, Any]:
    """One ``_file_info`` dict -> the wire row.

    The row is self-sufficient on purpose. It carries the finished
    ``FileRef`` and the preview verdict rather than the raw material for
    them, so a client renders and drags without re-deriving anything:

    - ``ref`` is built by :func:`to_file_ref` here, where ``FileRef`` and
      its ``extra="forbid"`` config live. A client assembling that object
      itself would be a second, untyped copy of a server model.
    - ``preview`` comes from :func:`services.media.preview.preview_kind`,
      the same function that decides the route's ``Content-Disposition``,
      so what the panel offers to display and what the server will serve
      inline cannot disagree.
    """
    from services.media.preview import preview_kind

    is_dir = bool(info.get("is_dir"))
    rel = _relative(str(info.get("path", "")))
    name = rel.rsplit("/", 1)[-1]
    mime = None if is_dir else (mimetypes.guess_type(name)[0] or "application/octet-stream")

    row: Dict[str, Any] = {
        "name": name,
        "path": rel,
        "is_dir": is_dir,
        "size_bytes": int(info.get("size") or 0),
        "modified_at": info.get("modified_at"),
        "mime_type": mime,
        "url": _entry_url(workflow_id, rel, is_dir),
        "preview": "none" if is_dir else preview_kind(mime),
    }
    # A directory has no reference: there is no such thing as a FileRef to
    # one, and a null is what stops a client offering to drag it anywhere.
    row["ref"] = None if is_dir else to_file_ref(row, workflow_id)
    return row


def _crumbs(rel: str) -> List[Dict[str, str]]:
    """Breadcrumb trail for a workspace-relative path.

    The root is omitted — it has no name of its own, and how to label it is
    the panel's business. Every other segment gets the path that navigating
    to it requires, so a client never has to reassemble one by splitting.
    """
    parts = [part for part in rel.split("/") if part]
    return [
        {"name": part, "path": "/".join(parts[: index + 1])}
        for index, part in enumerate(parts)
    ]


_GLOB_METACHARACTERS = frozenset("*?[]")


def search_to_pattern(term: str) -> str:
    """A human search term -> a glob the matcher will actually match.

    A bare word is not a glob, so ``greeting`` alone finds nothing and a
    search box would appear broken. Wrapping it makes the box behave the
    way people expect; a term already carrying glob metacharacters passes
    through untouched, so exact control stays available to anyone who
    wants it. Deciding this server-side keeps one definition of what
    "search" means for both the panel and the node.
    """
    term = term.strip()
    if not term:
        return ""
    return term if any(char in _GLOB_METACHARACTERS for char in term) else f"*{term}*"


def _sort_key(row: Dict[str, Any]):
    # Directories first, then case-insensitive name. Not cosmetic: it means
    # truncating at the cap can never strip navigation out of the response.
    return (not row["is_dir"], row["name"].lower())


async def list_directory(
    workspace_dir: str,
    *,
    path: str = "",
    workflow_id: Optional[str] = None,
    limit: int = WORKSPACE_LIST_DEFAULT,
) -> Dict[str, Any]:
    """List one directory. Never recursive.

    ``glob_info`` uses ``rglob``, so a ``*`` pattern would walk the entire
    tree unbounded — a multi-second handler and a multi-megabyte payload.
    Directory-at-a-time is what a tree view needs anyway.
    """
    from .._backend import get_backend, normalize_virtual_path

    capped = max(1, min(int(limit or WORKSPACE_LIST_DEFAULT), WORKSPACE_LIST_LIMIT))
    virtual = normalize_virtual_path(path or "/")
    rel = _relative(virtual)

    root = Path(workspace_dir)

    # Probe BEFORE building the backend: get_backend mkdirs the root, so
    # asking afterwards always answers True and the panel could never tell
    # "no workspace yet" from "this folder is empty" — which is the whole
    # reason these two flags exist, since ls_info returns [] for both.
    target = root / rel if rel else root
    workspace_exists = root.is_dir()
    path_exists = target.is_dir()

    backend = get_backend({}, {"workspace_dir": str(root)})

    # iterdir() on a large directory blocks; this runs on the shared WS loop.
    infos: List[Dict[str, Any]] = (
        await asyncio.to_thread(backend.ls_info, virtual) if path_exists else []
    )

    rows = sorted((_to_row(info, workflow_id) for info in infos), key=_sort_key)
    truncated = len(rows) > capped

    return {
        "path": rel,
        "parent": None if not rel else rel.rsplit("/", 1)[0] if "/" in rel else "",
        "crumbs": _crumbs(rel),
        "entries": rows[:capped],
        "count": min(len(rows), capped),
        "truncated": truncated,
        "workspace_exists": workspace_exists,
        "path_exists": path_exists,
    }


async def list_matching(
    workspace_dir: str,
    *,
    pattern: str,
    path: str = "",
    workflow_id: Optional[str] = None,
    limit: int = WORKSPACE_LIST_DEFAULT,
) -> Dict[str, Any]:
    """Recursive glob under ``path``. Files only — ``glob_info`` filters dirs.

    Used by the node's execution path and by panel search, both of which
    want to escape the current directory. Same cap applies.
    """
    from .._backend import get_backend, normalize_virtual_path

    capped = max(1, min(int(limit or WORKSPACE_LIST_DEFAULT), WORKSPACE_LIST_LIMIT))
    virtual = normalize_virtual_path(path or "/")

    backend = get_backend({}, {"workspace_dir": str(workspace_dir)})
    infos: List[Dict[str, Any]] = await asyncio.to_thread(
        backend.glob_info, pattern or "*", virtual
    )

    rows = sorted((_to_row(info, workflow_id) for info in infos), key=_sort_key)
    return {
        "path": _relative(virtual),
        "pattern": pattern or "*",
        "entries": rows[:capped],
        "count": min(len(rows), capped),
        "truncated": len(rows) > capped,
    }


def to_file_ref(row: Dict[str, Any], workflow_id: Optional[str]) -> Dict[str, Any]:
    """Wire row -> serialized ``FileRef``.

    Always ``kind="file"``, even for a ``.wav``: ``kind="audio"`` asserts the
    container was probed by ``inspect_audio``, and this only guessed a mime
    type from the extension. Claiming the richer kind with a null duration
    would mis-bill a per-second provider downstream.
    """
    from services.media.refs import FileRef

    return FileRef(
        path=row["path"],
        workflow_id=workflow_id,
        filename=row["name"],
        mime_type=row.get("mime_type") or "application/octet-stream",
        size_bytes=row.get("size_bytes") or 0,
        modified_at=row.get("modified_at"),
        url=row.get("url"),
    ).model_dump(mode="json")


__all__ = [
    "WORKSPACE_LIST_DEFAULT",
    "WORKSPACE_LIST_LIMIT",
    "list_directory",
    "list_matching",
    "search_to_pattern",
    "to_file_ref",
]
