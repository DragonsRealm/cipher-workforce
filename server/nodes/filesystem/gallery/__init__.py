"""Gallery — browse the workflow's workspace from the editor.

The panel is the point: it lists what a workflow has actually produced and
lets a file be dragged straight onto another node's parameter. Before this,
using a produced file meant already knowing its path and typing it by hand.

Deliberately **not** ``usable_as_tool``. ``fsSearch`` (ls/glob/grep) and
``fileModify`` (write/edit) already cover what an agent needs, and this node
carries destructive operations — shipping an editor panel should not hand
every agent a delete tool as a side effect. If agent-facing delete is ever
wanted it belongs on ``fileModify``, where the tool description can be
written carefully.

Running the node is not a no-op: it emits the listing as ``FileRef`` rows so
downstream nodes consume workspace files inside a workflow, not just in the
editor.
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._service import WORKSPACE_LIST_DEFAULT, list_directory, list_matching, to_file_ref


class GalleryParams(BaseModel):
    path: str = Field(
        default="",
        description="Workspace-relative directory to list. Empty = workspace root.",
    )
    pattern: str = Field(
        default="",
        description="Optional glob (e.g. '*.wav'). Set = recursive search under path.",
    )
    selection: List[str] = Field(
        default_factory=list,
        description="Specific workspace-relative files to emit, pinned in the panel.",
    )
    include_dirs: bool = Field(
        default=False,
        description="Include directories in the output as well as files.",
    )
    limit: int = Field(default=200, ge=1, le=1000)

    model_config = ConfigDict(extra="ignore")


class GalleryOutput(BaseModel):
    path: Optional[str] = None
    files: Optional[list] = None  # serialized FileRef rows
    directories: Optional[list] = None
    count: Optional[int] = None
    truncated: Optional[bool] = None
    missing: Optional[list] = None

    model_config = ConfigDict(extra="allow")


class GalleryNode(ActionNode):
    type = "gallery"
    display_name = "Gallery"
    subtitle = "Workspace Files"
    group = ("filesystem",)
    description = "Browse, manage and pass along this workflow's workspace files"
    component_kind = "square"
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    annotations = {"destructive": True, "readonly": False, "open_world": False}
    task_queue = TaskQueue.DEFAULT
    usable_as_tool = False
    # hideInputSection only: unlike processManager this node produces output
    # worth seeing and dragging, so the Output section stays.
    ui_hints = {"isGalleryPanel": True, "hideInputSection": True}

    Params = GalleryParams
    Output = GalleryOutput

    @Operation("list")
    async def list_files(self, ctx: NodeContext, params: GalleryParams) -> Any:
        workspace_dir = ctx.workspace_dir or (ctx.raw or {}).get("workspace_dir")
        if not workspace_dir:
            raise NodeUserError(
                "This workflow has no workspace yet. Save and run it once first."
            )

        if params.selection:
            return await self._emit_selection(ctx, params, str(workspace_dir))

        if params.pattern:
            result = await list_matching(
                str(workspace_dir),
                pattern=params.pattern,
                path=params.path,
                workflow_id=ctx.workflow_id,
                limit=params.limit,
            )
        else:
            result = await list_directory(
                str(workspace_dir),
                path=params.path,
                workflow_id=ctx.workflow_id,
                limit=params.limit,
            )

        rows = result["entries"]
        # The listing already carries a finished ref per row — building a
        # second one here would be two places to keep in step.
        files = [r["ref"] for r in rows if not r["is_dir"]]
        directories = [r["path"] for r in rows if r["is_dir"]]

        return GalleryOutput(
            path=result["path"],
            files=files,
            directories=directories if params.include_dirs else [],
            count=len(files),
            truncated=result["truncated"],
        )

    async def _emit_selection(
        self, ctx: NodeContext, params: GalleryParams, workspace_dir: str
    ) -> Any:
        """Emit exactly the pinned files, reporting any that have gone.

        A missing selection is not automatically an error: the node that
        writes the file may simply not have run yet in this execution. Only
        an entirely missing selection fails.
        """
        import mimetypes
        from datetime import datetime

        from services.media.workspace import resolve_media

        files: list = []
        missing: list = []
        for rel in params.selection:
            try:
                target = resolve_media(rel, workspace_dir=workspace_dir)
            except NodeUserError:
                missing.append(rel)
                continue
            if not target.is_file():
                missing.append(rel)
                continue
            stat = target.stat()
            files.append(
                to_file_ref(
                    {
                        "name": target.name,
                        "path": str(rel).strip("/"),
                        "is_dir": False,
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "mime_type": mimetypes.guess_type(target.name)[0],
                        "url": None,
                    },
                    ctx.workflow_id,
                )
            )

        if missing and not files:
            raise NodeUserError(
                "None of the selected files exist in this workspace: "
                + ", ".join(missing[:5])
            )

        return GalleryOutput(
            path=params.path or "",
            files=files,
            directories=[],
            count=len(files),
            truncated=False,
            missing=missing,
        )


# --- self-registration on import -------------------------------------------
from services.ws_handler_registry import register_ws_handlers  # noqa: E402

from ._handlers import WS_HANDLERS  # noqa: E402

register_ws_handlers(WS_HANDLERS)


__all__ = [
    "WS_HANDLERS",
    "GalleryNode",
    "GalleryOutput",
    "GalleryParams",
    "WORKSPACE_LIST_DEFAULT",
]
