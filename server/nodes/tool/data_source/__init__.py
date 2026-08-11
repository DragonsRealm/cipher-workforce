"""Data node — an agent-facing raw-data reader/writer.

One locked multi-operation tool (``data``) over two path namespaces: the
per-workflow workspace and operator-approved external mounts
(``mnt/<name>/...``). Reads dispatch to typed, bounded tiers
(text/csv/json/pdf/html/xlsx/image-metadata/binary); binary content travels
as references, never bytes. Writes are allowed into the workspace and into
mounts whose operator flipped the writable flag. There is deliberately no
delete operation — deletion stays a human action in the gallery panel.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.plugin import (
    NodeContext,
    NodeUserError,
    Operation,
    TaskQueue,
    ToolNode,
)

from ._paths import (
    MOUNT_PREFIX,
    ResolvedTarget,
    mount_entry,
    owner_id,
    resolve_data_path,
    split_mount_path,
)
from ._readers import (
    bound_result,
    detect_tier,
    file_sha256,
    read_csv,
    read_html,
    read_image_meta,
    read_json,
    read_pdf,
    read_text,
    read_xlsx,
    walk_mount,
)

DataOperation = Literal[
    "list",
    "read",
    "search",
    "metadata",
    "write",
    "append",
    "copy_to_workspace",
]

ReadType = Literal[
    "auto", "text", "csv", "json", "pdf", "html", "xlsx", "image", "binary"
]

_LIST_LIMIT_MAX = 500
_COPY_SUFFIX_ATTEMPTS = 100


class DataToolParams(BaseModel):
    """Persisted operator configuration; never exposed as model arguments."""

    mounts: list[str] = Field(
        default_factory=list,
        title="Enabled Mounts",
        description=(
            "Names of machine-wide mounts this node exposes to its agent. "
            "Mounts are defined in the Data panel."
        ),
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce_mounts(cls, data: Any) -> Any:
        # The parameter panel stores "" for cleared fields regardless of
        # declared type, and list values may arrive as JSON strings.
        if isinstance(data, dict):
            raw = data.get("mounts")
            if raw == "" or raw is None:
                data = {**data, "mounts": []}
            elif isinstance(raw, str):
                import json

                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = [raw]
                data = {
                    **data,
                    "mounts": parsed if isinstance(parsed, list) else [str(parsed)],
                }
        return data


class DataToolInput(BaseModel):
    """One locked, multi-operation schema visible to the LLM."""

    operation: DataOperation = Field(
        description=(
            "Data operation: list, read, search, metadata, write, append, "
            "or copy_to_workspace."
        )
    )
    path: str = Field(
        default="",
        max_length=4_096,
        description=(
            "Workspace-relative path (e.g. reports/q3.csv), or an external "
            "mount path mnt/<mount_name>/<file>. Empty lists the workspace "
            "root plus the enabled mounts."
        ),
    )
    pattern: Optional[str] = Field(
        default=None,
        max_length=256,
        description="search: filename glob or bare term (term matches *term*)",
    )
    as_type: ReadType = Field(
        default="auto",
        description="read: force a tier instead of extension detection",
    )
    offset: int = Field(default=0, ge=0, description="read: rows/lines/pages to skip")
    limit: int = Field(default=100, ge=1, le=_LIST_LIMIT_MAX)
    sheet: Optional[str] = Field(default=None, max_length=128)
    encoding: Optional[str] = Field(default=None, max_length=32)
    content: Optional[str] = Field(
        default=None,
        max_length=200_000,
        description="write/append: UTF-8 text content",
    )
    dest: Optional[str] = Field(
        default=None,
        max_length=4_096,
        description=(
            "copy_to_workspace: workspace-relative destination "
            "(default imports/<filename>)"
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_operation_fields(self) -> "DataToolInput":
        required: dict[str, tuple[str, ...]] = {
            "read": ("path",),
            "metadata": ("path",),
            "write": ("path", "content"),
            "append": ("path", "content"),
            "search": ("pattern",),
            "copy_to_workspace": ("path",),
        }
        missing = [
            field_name
            for field_name in required.get(self.operation, ())
            if not getattr(self, field_name)
        ]
        if missing:
            raise ValueError(f"{self.operation} requires {', '.join(missing)}")
        return self


class DataToolOutput(BaseModel):
    operation: str
    path: Optional[str] = None
    source: Optional[str] = None
    mount: Optional[str] = None
    type: Optional[str] = None
    truncated: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


def _utc_iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


class DataSourceNode(ToolNode):
    type = "dataSource"
    display_name = "Data"
    subtitle = "Files & Raw Data"
    group = ("tool",)
    description = (
        "Read, search, and write raw local data — workspace files and "
        "operator-mounted folders — with typed bounded readers for text, "
        "CSV, JSON, PDF, HTML, XLSX, and image metadata"
    )
    component_kind = "tool"
    tool_name = "data"
    tool_description = (
        "Read and write raw local data. Paths are workspace-relative "
        "(reports/q3.csv) or external-mount paths (mnt/<mount_name>/<file>). "
        "Start with list (empty path) to discover files and enabled mounts. "
        "read auto-detects text/csv/json/pdf/html/xlsx/image and pages with "
        "offset/limit; metadata stats a file without reading it; write/append "
        "store UTF-8 text (mounts only when writable); copy_to_workspace "
        "imports a mount file into the workspace. There is no delete."
    )
    handles = (
        {
            "name": "output-tool",
            "kind": "output",
            "position": "top",
            "label": "Data",
            "role": "tools",
        },
    )
    ui_hints = {
        "isToolPanel": True,
        "isDataPanel": True,
        "hideInputSection": True,
        "hideOutputSection": True,
        "hideRunButton": True,
    }
    annotations = {
        "destructive": False,
        "readonly": False,
        "open_world": False,
    }
    task_queue = TaskQueue.DEFAULT

    Params = DataToolParams
    ToolInput = DataToolInput
    Output = DataToolOutput
    tool_schema_locked = True
    server_controlled_fields = frozenset({"mounts"})

    @staticmethod
    def _enabled_mounts(ctx: NodeContext, params: Any) -> list[str]:
        config = ctx.raw.get("_tool_config")
        if isinstance(config, DataToolParams):
            return list(config.mounts)
        if isinstance(params, DataToolParams):
            return list(params.mounts)
        return []

    @Operation("data")
    async def data(
        self,
        ctx: NodeContext,
        params: DataToolInput | DataToolParams,
    ) -> DataToolOutput:
        # The node's Run button is hidden. Treat a framework-side execution
        # as a harmless root listing for diagnostics.
        args = (
            DataToolInput(operation="list")
            if isinstance(params, DataToolParams)
            else params
        )
        mounts = self._enabled_mounts(ctx, params)
        handlers = {
            "list": self._op_list,
            "read": self._op_read,
            "search": self._op_search,
            "metadata": self._op_metadata,
            "write": self._op_write,
            "append": self._op_append,
            "copy_to_workspace": self._op_copy,
        }
        result = await handlers[args.operation](ctx, mounts, args)
        result.setdefault("operation", args.operation)
        return DataToolOutput.model_validate(bound_result(result))

    # ---------------------------------------------------------------- ops

    async def _op_list(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        if split_mount_path(args.path) is not None:
            return await self._list_mount_dir(ctx, mounts, args)

        from nodes.filesystem.gallery._service import list_directory
        from services.media.workspace import workspace_root

        root = workspace_root(ctx)
        listing = await list_directory(
            str(root),
            path=args.path,
            workflow_id=ctx.workflow_id,
            limit=args.limit,
        )
        result: dict[str, Any] = {
            "source": "workspace",
            "path": listing["path"],
            "entries": listing["entries"],
            "count": listing["count"],
            "truncated": listing["truncated"],
        }
        if not str(args.path or "").strip("/"):
            result["mounts"] = await self._mount_summaries(ctx, mounts)
        return result

    async def _mount_summaries(
        self, ctx: NodeContext, mounts: list[str]
    ) -> list[dict[str, Any]]:
        """The enabled mounts as the model discovers them at runtime."""
        if not mounts:
            return []
        from services.data.mount_store import DataMountStore
        from services.plugin.deps import get_database

        rows = await DataMountStore(get_database()).list_mounts(owner_id(ctx))
        by_name = {row["name"]: row for row in rows}
        summaries = []
        for name in mounts:
            row = by_name.get(name)
            summaries.append(
                {
                    "name": name,
                    "path": f"{MOUNT_PREFIX}/{name}",
                    "writable": bool(row["writable"]) if row else False,
                    "available": row is not None,
                }
            )
        return summaries

    async def _list_mount_dir(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        target = await resolve_data_path(ctx, mounts, args.path)
        if not target.abs_path.is_dir():
            raise NodeUserError(
                f"{target.virtual} is not a directory; use read for files"
            )

        def scan() -> list[dict[str, Any]]:
            entries = []
            with os.scandir(target.abs_path) as iterator:
                for entry in iterator:
                    rest = (
                        f"{target.virtual.split('/', 2)[2]}/{entry.name}"
                        if target.virtual.count("/") >= 2
                        else entry.name
                    )
                    entries.append(
                        mount_entry(
                            mount_name=target.mount_name or "",
                            rel_path=rest,
                            abs_path=Path(entry.path),
                            writable=target.writable,
                        )
                    )
            entries.sort(key=lambda row: (not row["is_dir"], row["name"].lower()))
            return entries

        rows = await asyncio.to_thread(scan)
        return {
            "source": "mount",
            "mount": target.mount_name,
            "path": target.virtual,
            "entries": rows[: args.limit],
            "count": min(len(rows), args.limit),
            "truncated": len(rows) > args.limit,
        }

    async def _op_read(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        target = await self._resolve_file(ctx, mounts, args.path)
        tier = detect_tier(target.abs_path, args.as_type)
        common = self._common(target)

        if tier == "image":
            meta = await asyncio.to_thread(read_image_meta, target.abs_path)
            result = {**common, **meta, **self._reference(ctx, target)}
            # Vision opt-in (services/llm/media.py contract): workspace refs
            # to allowlisted image types become viewable by vision-capable
            # host models; mounts go through copy_to_workspace first.
            ref = result.get("ref")
            if isinstance(ref, dict):
                from services.llm.media import IMAGE_MIME_ALLOWLIST

                if ref.get("mime_type") in IMAGE_MIME_ALLOWLIST:
                    result["llm_media"] = [{"ref": ref, "detail": "auto"}]
            return result
        if tier == "binary":
            sha = await asyncio.to_thread(file_sha256, target.abs_path)
            stat = target.abs_path.stat()
            return {
                **common,
                "type": "binary",
                "mime_type": mimetypes.guess_type(target.abs_path.name)[0]
                or "application/octet-stream",
                "size_bytes": int(stat.st_size),
                "sha256": sha,
                **self._reference(ctx, target),
            }

        readers = {
            "text": lambda: read_text(
                target.abs_path,
                offset=args.offset,
                limit=args.limit,
                encoding=args.encoding,
            ),
            "csv": lambda: read_csv(
                target.abs_path,
                offset=args.offset,
                limit=args.limit,
                encoding=args.encoding,
            ),
            "json": lambda: read_json(
                target.abs_path, encoding=args.encoding
            ),
            "pdf": lambda: read_pdf(
                target.abs_path, offset=args.offset, limit=args.limit
            ),
            "html": lambda: read_html(
                target.abs_path,
                offset=args.offset,
                limit=args.limit,
                encoding=args.encoding,
            ),
            "xlsx": lambda: read_xlsx(
                target.abs_path,
                sheet=args.sheet,
                offset=args.offset,
                limit=args.limit,
            ),
        }
        payload = await asyncio.to_thread(readers[tier])
        return {**common, **payload}

    async def _op_search(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        from nodes.filesystem.gallery._service import (
            list_matching,
            search_to_pattern,
        )

        pattern = search_to_pattern(args.pattern or "")
        if not pattern:
            raise NodeUserError("search requires a non-empty pattern")

        if split_mount_path(args.path) is not None:
            target = await resolve_data_path(ctx, mounts, args.path)
            start_rel = (
                target.virtual.split("/", 2)[2]
                if target.virtual.count("/") >= 2
                else ""
            )
            rel_paths, truncated = await asyncio.to_thread(
                walk_mount,
                target.root,
                start_rel=start_rel,
                pattern=pattern,
            )
            rows = [
                mount_entry(
                    mount_name=target.mount_name or "",
                    rel_path=rel,
                    abs_path=target.root / rel,
                    writable=target.writable,
                )
                for rel in rel_paths[: args.limit]
            ]
            return {
                "source": "mount",
                "mount": target.mount_name,
                "path": target.virtual,
                "pattern": pattern,
                "entries": rows,
                "count": len(rows),
                "truncated": truncated or len(rel_paths) > args.limit,
            }

        from services.media.workspace import workspace_root

        root = workspace_root(ctx)
        listing = await list_matching(
            str(root),
            pattern=pattern,
            path=args.path,
            workflow_id=ctx.workflow_id,
            limit=args.limit,
        )
        return {
            "source": "workspace",
            "path": listing["path"],
            "pattern": listing["pattern"],
            "entries": listing["entries"],
            "count": listing["count"],
            "truncated": listing["truncated"],
        }

    async def _op_metadata(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        target = await self._resolve_file(ctx, mounts, args.path)
        stat = target.abs_path.stat()
        tier = detect_tier(target.abs_path)
        result: dict[str, Any] = {
            **self._common(target),
            "type": tier,
            "name": target.abs_path.name,
            "mime_type": mimetypes.guess_type(target.abs_path.name)[0]
            or "application/octet-stream",
            "size_bytes": int(stat.st_size),
            "modified_at": _utc_iso(stat.st_mtime),
            "sha256": await asyncio.to_thread(file_sha256, target.abs_path),
            **self._reference(ctx, target),
        }
        if tier == "image":
            try:
                meta = await asyncio.to_thread(read_image_meta, target.abs_path)
                result["image"] = meta["image"]
            except NodeUserError:
                pass
        return result

    async def _op_write(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        return await self._write(ctx, mounts, args, append=False)

    async def _op_append(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        return await self._write(ctx, mounts, args, append=True)

    async def _write(
        self,
        ctx: NodeContext,
        mounts: list[str],
        args: DataToolInput,
        *,
        append: bool,
    ) -> dict[str, Any]:
        from nodes.filesystem._backend import atomic_write_bytes, get_path_lock

        target = await resolve_data_path(
            ctx, mounts, args.path, for_write=True
        )
        payload = (args.content or "").encode("utf-8")
        existed = target.abs_path.exists()
        if target.abs_path.is_dir():
            raise NodeUserError(f"{target.virtual} is a directory")

        def write_sync() -> None:
            target.abs_path.parent.mkdir(parents=True, exist_ok=True)
            if append:
                with open(target.abs_path, "ab") as handle:
                    handle.write(payload)
            else:
                atomic_write_bytes(
                    target.abs_path, payload, root_dir=target.root
                )

        async with get_path_lock(target.abs_path):
            await asyncio.to_thread(write_sync)
        return {
            **self._common(target),
            "bytes_written": len(payload),
            "created": not existed,
            "appended": append,
        }

    async def _op_copy(
        self, ctx: NodeContext, mounts: list[str], args: DataToolInput
    ) -> dict[str, Any]:
        from nodes.filesystem._backend import (
            get_path_lock,
            resolve_entry_within,
        )
        from services.media.limits import MEDIA_MAX_READ_BYTES
        from services.media.workspace import workspace_root

        if split_mount_path(args.path) is None:
            raise NodeUserError(
                "copy_to_workspace imports FROM a mount: path must be "
                "mnt/<mount_name>/<file>"
            )
        source = await self._resolve_file(ctx, mounts, args.path)
        dest_rel = (
            str(args.dest or "").strip().replace("\\", "/").strip("/")
            or f"imports/{source.abs_path.name}"
        )
        if dest_rel.split("/", 1)[0] == MOUNT_PREFIX:
            raise NodeUserError(
                "copy_to_workspace destination is workspace-relative; it "
                "cannot target a mount"
            )
        root = workspace_root(ctx).resolve()
        dest = resolve_entry_within(root, dest_rel)
        # Never overwrite: an import must not be able to destroy data.
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            for attempt in range(1, _COPY_SUFFIX_ATTEMPTS + 1):
                candidate = dest.with_name(f"{stem}-{attempt}{suffix}")
                if not candidate.exists():
                    dest = candidate
                    break
            else:
                raise NodeUserError(
                    f"Too many existing copies of {dest_rel} in the workspace"
                )
        size = source.abs_path.stat().st_size
        if size > MEDIA_MAX_READ_BYTES:
            raise NodeUserError(
                f"File is {size:,} bytes; copy_to_workspace caps at "
                f"{MEDIA_MAX_READ_BYTES:,}"
            )

        def copy_sync() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.abs_path, dest)

        async with get_path_lock(dest):
            await asyncio.to_thread(copy_sync)
        final_rel = dest.relative_to(root).as_posix()
        return {
            "source": "workspace",
            "path": final_rel,
            "copied_from": source.virtual,
            "ref": self._workspace_ref(ctx, root, dest),
        }

    # ------------------------------------------------------------ helpers

    @staticmethod
    async def _resolve_file(
        ctx: NodeContext, mounts: list[str], path: str
    ) -> ResolvedTarget:
        from services.media.limits import MEDIA_MAX_READ_BYTES

        target = await resolve_data_path(ctx, mounts, path)
        if target.abs_path.is_dir():
            raise NodeUserError(
                f"{target.virtual} is a directory; use list for directories"
            )
        if not target.abs_path.is_file():
            raise NodeUserError(f"File not found: {target.virtual}")
        size = target.abs_path.stat().st_size
        if size > MEDIA_MAX_READ_BYTES:
            raise NodeUserError(
                f"File is {size:,} bytes; reads cap at "
                f"{MEDIA_MAX_READ_BYTES:,}"
            )
        return target

    @staticmethod
    def _common(target: ResolvedTarget) -> dict[str, Any]:
        common: dict[str, Any] = {
            "source": target.source,
            "path": target.virtual,
        }
        if target.mount_name:
            common["mount"] = target.mount_name
        return common

    def _reference(
        self, ctx: NodeContext, target: ResolvedTarget
    ) -> dict[str, Any]:
        """The handle other nodes consume: FileRef in the workspace,
        location-only for mounts (host paths never leave the server)."""
        if target.source == "workspace":
            return {
                "ref": self._workspace_ref(ctx, target.root, target.abs_path)
            }
        return {"location": target.virtual}

    @staticmethod
    def _workspace_ref(
        ctx: NodeContext, root: Path, abs_path: Path
    ) -> dict[str, Any]:
        from nodes.filesystem.gallery._service import to_file_ref
        from services.media.workspace import workspace_file_url

        rel = abs_path.relative_to(root).as_posix()
        stat = abs_path.stat()
        workflow_id = ctx.workflow_id
        row = {
            "path": rel,
            "name": abs_path.name,
            "mime_type": mimetypes.guess_type(abs_path.name)[0]
            or "application/octet-stream",
            "size_bytes": int(stat.st_size),
            "modified_at": _utc_iso(stat.st_mtime),
            "url": workspace_file_url(workflow_id, rel) if workflow_id else None,
        }
        return to_file_ref(row, workflow_id)


# Plugin-owned side-channel API for the Data panel.
from services.ws_handler_registry import register_ws_handlers  # noqa: E402

from ._handlers import WS_HANDLERS  # noqa: E402

register_ws_handlers(WS_HANDLERS)


__all__ = [
    "WS_HANDLERS",
    "DataOperation",
    "DataSourceNode",
    "DataToolInput",
    "DataToolOutput",
    "DataToolParams",
]
