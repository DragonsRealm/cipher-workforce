"""Path resolution for the Data node's two-source namespace.

One ``path`` argument addresses two kinds of roots:

    reports/q3.csv                -> the per-workflow workspace (as every
                                     existing filesystem tool spells paths)
    mnt/<mount_name>/<rel_path>   -> an operator-approved external mount

The first segment ``mnt`` is *reserved*: workspace operations refuse it, so
the two namespaces can never collide and the model gets a hint instead of a
silent miss. Containment for both roots runs through the same two-layer
helpers in :mod:`nodes.filesystem._backend` (``resolve_within`` for reads,
``resolve_entry_within`` for mutations) — they are root-agnostic and
symlink-safe.

A mount is usable only when it is BOTH in the node's persisted subset
(``Params.mounts``) AND still present in the machine-wide allowlist at call
time, so a globally revoked mount dies immediately even if a stale node
still lists it.

External files never leak host locations: results carry the round-trippable
virtual path (``mnt/<name>/<rel>``), never ``root_path`` or any absolute
path.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from services.plugin import NodeContext, NodeUserError

MOUNT_PREFIX = "mnt"


@dataclass(frozen=True)
class ResolvedTarget:
    """A validated, contained filesystem target."""

    source: Literal["workspace", "mount"]
    mount_name: Optional[str]
    abs_path: Path
    root: Path
    writable: bool
    virtual: str  # canonical round-trippable path (feed back as `path`)


def owner_id(ctx: NodeContext) -> str:
    """Trusted owner identity — mirrors SimpleMemoryNode._scope."""
    return str(ctx.user_id or ctx.raw.get("user_id") or "owner")


def _clean(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").strip("/")


def split_mount_path(path: str) -> Optional[tuple[str, str]]:
    """``mnt/<name>/<rest>`` -> (name, rest); None when not mount-shaped."""
    cleaned = _clean(path)
    if not cleaned:
        return None
    first, _, rest = cleaned.partition("/")
    if first != MOUNT_PREFIX:
        return None
    name, _, remainder = rest.partition("/")
    return (name, remainder)


async def _mount_row(
    ctx: NodeContext, enabled: list[str], name: str
) -> dict[str, Any]:
    from services.data.mount_store import DataMountStore
    from services.plugin.deps import get_database

    if not name:
        raise NodeUserError(
            "A mount path needs a name: mnt/<mount_name>/<file>. "
            f"Mounts enabled on this node: {', '.join(enabled) or 'none'}"
        )
    if name not in enabled:
        raise NodeUserError(
            f"Mount '{name}' is not enabled on this Data node. "
            f"Enabled mounts: {', '.join(enabled) or 'none'}"
        )
    row = await DataMountStore(get_database()).get_mount(owner_id(ctx), name)
    if row is None:
        raise NodeUserError(
            f"Mount '{name}' is no longer defined on this machine; ask the "
            "operator to re-add it in the Data panel"
        )
    return row


async def resolve_data_path(
    ctx: NodeContext,
    mounts: list[str],
    path: str,
    *,
    for_write: bool = False,
) -> ResolvedTarget:
    """Resolve ``path`` to a contained absolute target or raise.

    ``for_write`` switches to :func:`resolve_entry_within` (containment
    proved on the parent, basename appended unresolved — mutation-correct)
    and enforces the mount's writable flag. The workspace is always
    writable.
    """
    from nodes.filesystem._backend import (
        resolve_entry_within as _resolve_entry_within,
        resolve_within as _resolve_within,
    )
    from services.media.workspace import workspace_root

    def resolve_within(root: Path, key: str) -> Path:
        # The containment helpers raise ValueError; translate to the
        # user-correctable contract so the operator log gets one WARN line
        # and the model gets an explanation it can act on.
        try:
            return _resolve_within(root, key)
        except ValueError as exc:
            raise NodeUserError(str(exc)) from exc

    def resolve_entry_within(root: Path, key: str) -> Path:
        try:
            return _resolve_entry_within(root, key)
        except ValueError as exc:
            raise NodeUserError(str(exc)) from exc

    split = split_mount_path(path)
    if split is not None:
        name, rest = split
        row = await _mount_row(ctx, list(mounts or []), name)
        root = Path(row["root_path"])
        writable = bool(row["writable"])
        if for_write:
            if not writable:
                raise NodeUserError(
                    f"Mount '{name}' is read-only. Ask the operator to "
                    "flip its writable flag, or write into the workspace"
                )
            if not rest:
                raise NodeUserError(
                    "Writes need a file path inside the mount, not the "
                    "mount root itself"
                )
            abs_path = resolve_entry_within(root, rest)
        else:
            abs_path = resolve_within(root, rest) if rest else root.resolve()
        virtual = f"{MOUNT_PREFIX}/{name}/{rest}" if rest else f"{MOUNT_PREFIX}/{name}"
        return ResolvedTarget(
            source="mount",
            mount_name=name,
            abs_path=abs_path,
            root=root.resolve(),
            writable=writable,
            virtual=virtual,
        )

    rel = _clean(path)
    if rel.split("/", 1)[0] == MOUNT_PREFIX:
        # Unreachable in practice (split_mount_path matched), kept as a
        # guard should the split rules ever diverge.
        raise NodeUserError(
            "'mnt/' is reserved for external mounts: mnt/<mount_name>/<file>"
        )
    root = workspace_root(ctx).resolve()
    if for_write:
        if not rel:
            raise NodeUserError("Writes need a file path, not the workspace root")
        abs_path = resolve_entry_within(root, rel)
    else:
        abs_path = resolve_within(root, rel) if rel else root
    return ResolvedTarget(
        source="workspace",
        mount_name=None,
        abs_path=abs_path,
        root=root,
        writable=True,
        virtual=rel,
    )


def mount_entry(
    *,
    mount_name: str,
    rel_path: str,
    abs_path: Path,
    writable: bool,
) -> dict[str, Any]:
    """Listing/metadata row for an external-mount entry.

    Deliberately NOT a FileRef: that type is workspace-contract-bound (its
    ``path`` is served by the workspace HTTP route). ``location`` is the
    only address given out, so no host path ever reaches outputs, the DB,
    or LLM context.
    """
    try:
        stat = abs_path.stat()
        size = int(stat.st_size)
        modified = stat.st_mtime
    except OSError:
        size = 0
        modified = None
    is_dir = abs_path.is_dir()
    location = (
        f"{MOUNT_PREFIX}/{mount_name}/{rel_path}"
        if rel_path
        else f"{MOUNT_PREFIX}/{mount_name}"
    )
    return {
        "location": location,
        "mount": mount_name,
        "name": abs_path.name or mount_name,
        "is_dir": is_dir,
        "mime_type": None
        if is_dir
        else (mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"),
        "size_bytes": 0 if is_dir else size,
        "modified_at": modified,
        "writable": writable,
    }


__all__ = [
    "MOUNT_PREFIX",
    "ResolvedTarget",
    "mount_entry",
    "owner_id",
    "resolve_data_path",
    "split_mount_path",
]
