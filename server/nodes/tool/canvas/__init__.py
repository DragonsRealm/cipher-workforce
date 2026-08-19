"""Canvas node — a display board workflows and agents push content onto.

The board holds three item kinds: workspace file *references* (never bytes —
the media-transport contract), external URLs, and small markdown notes. The
parameter panel and the docked canvas sidebar render it; agents push via the
``canvas`` tool, workflows via the ``input-main`` edge (explicit params or a
structural FileRef scan of the connected outputs).

Shape follows ``nodes/tool/write_todos`` (ToolNode + input-main handle +
per-scope store + metadata-only broadcast + panel WS handlers); the WS
handlers copy ``nodes/tool/simple_memory``'s security preamble.
"""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, get_args
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from services.plugin import (
    NodeContext,
    NodeUserError,
    Operation,
    TaskQueue,
    ToolNode,
)
from services.plugin.params import coerce_blank_params

from ._store import (
    CANVAS_MAX_PATHS_PER_CALL,
    CanvasScope,
    CanvasStore,
    CanvasStoreError,
    UNSAVED_WORKFLOW_ID,
    truncate_note,
)

# Depth cap for the structural FileRef scan of connected outputs. Node
# results are already payload-disciplined; anything nested deeper than this
# is not a ref a user meant to display.
_SCAN_MAX_DEPTH = 6


# Flat on purpose — this IS the LLM tool schema (nested models would emit
# $defs/$ref, which the tool-schema invariant rejects). The docstring is
# deliberately absent: a Pydantic model docstring becomes the schema
# description the model reads.
class CanvasParams(BaseModel):
    title: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Optional caption for the first item added by this call.",
    )
    paths: Optional[List[str]] = Field(
        default=None,
        description=(
            "Workspace-relative file paths to display "
            "(e.g. ['media/chart.png', 'reports/summary.md'])."
        ),
    )
    url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="An external http(s) URL to display in an embedded frame.",
    )
    content: Optional[str] = Field(
        default=None,
        description="Markdown text to display directly (a note/report).",
    )
    language: Optional[str] = Field(
        default=None,
        max_length=40,
        description="Optional syntax-highlight language when content is code.",
    )
    mode: Literal["append", "replace"] = Field(
        default="append",
        description="append adds to the board; replace clears it first.",
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce_blanks(cls, values: Any) -> Any:
        return coerce_blank_params(cls, values)

    @field_validator("paths", mode="before")
    @classmethod
    def _coerce_paths(cls, value: Any) -> Any:
        """Accept the shapes that actually reach this field.

        - a JSON-encoded array string (Gemini stringifies array tool args —
          same boundary coercion as ``WriteTodosParams._coerce_todos``);
        - a bare path string (LLM passed one path instead of a list);
        - serialized FileRef dicts (a ``{{node.files}}`` template resolves to
          a list of dicts) — reduced to their ``path`` key.
        Malformed JSON passes through so Pydantic raises a correctable error.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                return [stripped]
            value = parsed if isinstance(parsed, list) else [stripped]
        if isinstance(value, (list, tuple)):
            coerced: List[Any] = []
            for entry in value:
                if isinstance(entry, dict) and entry.get("path"):
                    coerced.append(str(entry["path"]))
                else:
                    coerced.append(entry)
            return coerced
        return value


class CanvasOutput(BaseModel):
    """Small by contract: ids and counts only, never bodies or full refs —
    a node result is persisted, broadcast, and replayed into LLM context."""

    message: Optional[str] = None
    count: Optional[int] = None
    revision: Optional[int] = None
    added: Optional[list] = None

    model_config = ConfigDict(extra="allow")


class CanvasNode(ToolNode):
    type = "canvas"
    display_name = "Canvas"
    subtitle = "Content Board"
    group = ("tool", "ai")
    description = (
        "Display board for workflow output: images, video, audio, documents, "
        "reports, and websites render in the Canvas panel and the docked "
        "canvas sidebar."
    )
    component_kind = "tool"
    tool_name = "canvas"
    # Locks tool_name + schema against stale persisted ToolSchema rows
    # (simpleMemory precedent).
    tool_schema_locked = True
    tool_description = (
        "Show content to the user on the Canvas board. Pass workspace file "
        "paths (images, video, audio, PDF, markdown, code, HTML), an external "
        "http(s) URL, or markdown text via content. Use this after producing "
        "a file or result the user should see — e.g. a screenshot, a chart, "
        "or a report. mode=replace clears the board first; the default "
        "append adds to it."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-tool", "kind": "output", "position": "top", "label": "Tool", "role": "tools"},
    )
    # isConfigNode is auto-derived True for the "tool" group; the Canvas
    # node's input-main is a real runtime dataflow edge (upstream output
    # displays on execution), so it must present as a normal node with
    # inheritable inputs disabled. Explicit declaration wins over
    # auto-derivation.
    ui_hints = {
        "isCanvasPanel": True,
        "isToolPanel": True,
        "isConfigNode": False,
    }
    # Overrides ToolNode's readonly default — canvas writes the durable board.
    annotations = {"destructive": False, "readonly": False, "open_world": False}
    task_queue = TaskQueue.DEFAULT

    Params = CanvasParams
    Output = CanvasOutput

    @Operation("display")
    async def display(self, ctx: NodeContext, params: CanvasParams) -> Any:
        """Push content onto the board. Single op — serves the agent tool
        call, the Run button, and the workflow edge identically."""
        from services.plugin.deps import get_database

        from ._events import dispatch_canvas_updated

        scope = CanvasScope(
            owner_id=ctx.user_id or "owner",
            workflow_id=ctx.workflow_id or UNSAVED_WORKFLOW_ID,
            node_id=ctx.node_id,
        )
        # execute_as_tool sets _tool_config around the op body; the Run
        # button and the workflow edge never do. Cosmetic labeling only.
        source = "agent" if "_tool_config" in ctx.raw else "workflow"

        items: List[Dict[str, Any]] = []
        notices: List[str] = []

        raw_paths = [p for p in (params.paths or []) if str(p or "").strip()]
        if len(raw_paths) > CANVAS_MAX_PATHS_PER_CALL:
            raise NodeUserError(
                f"Too many paths in one canvas call "
                f"({len(raw_paths)} > {CANVAS_MAX_PATHS_PER_CALL}). "
                "Split into multiple calls."
            )
        for raw in raw_paths:
            items.append(
                {
                    "kind": "file",
                    "ref": _build_ref(ctx, str(raw)),
                    "source": source,
                }
            )

        if params.url:
            scheme = urlsplit(params.url).scheme.lower()
            if scheme not in ("http", "https"):
                raise NodeUserError(
                    "url must be an http(s) address, "
                    f"got scheme '{scheme or 'none'}'"
                )
            items.append({"kind": "url", "url": params.url, "source": source})

        if params.content:
            note, truncated = truncate_note(params.content)
            if truncated:
                notices.append("note content was truncated at 64KB")
            items.append(
                {
                    "kind": "note",
                    "content": note,
                    "language": params.language,
                    "source": source,
                }
            )

        # Workflow-edge convenience: with no explicit items, display whatever
        # FileRefs the connected upstream outputs carry (tts -> canvas works
        # with a zero-config edge).
        if not items:
            connected = ctx.raw.get("connected_outputs") or {}
            for ref in _collect_connected_refs(
                connected, limit=CANVAS_MAX_PATHS_PER_CALL
            ):
                items.append({"kind": "file", "ref": ref, "source": source})

        if not items:
            raise NodeUserError(
                "Nothing to display — pass paths, url, or content "
                "(or connect a node that outputs files)."
            )

        if params.title:
            items[0]["title"] = params.title

        store = CanvasStore(get_database())
        try:
            added, revision, total = await store.append(
                scope, items, mode=params.mode
            )
        except CanvasStoreError as exc:
            raise NodeUserError(str(exc)) from exc

        await dispatch_canvas_updated(
            workflow_id=ctx.workflow_id,
            node_id=ctx.node_id,
            revision=revision,
        )

        message = f"Displayed {len(added)} item(s); board holds {total}."
        if params.mode == "replace":
            message = f"Board replaced with {len(added)} item(s)."
        if notices:
            message += " " + "; ".join(notices) + "."
        return {
            "message": message,
            "count": total,
            "revision": revision,
            "added": [
                {"id": row["id"], "kind": row["kind"], "title": row["title"]}
                for row in added
            ],
        }


def _build_ref(ctx: NodeContext, raw_path: str) -> Dict[str, Any]:
    """A workspace path -> a serialized ``FileRef`` (gallery-style).

    Containment via ``resolve_media`` + a stat — never reads bytes: the board
    stores a pointer, and reading a 500 MB video to register it would be
    pure waste. ``kind`` stays ``"file"`` even for media — ``kind="audio"``
    asserts a container probe this code never ran.
    """
    from services.media.refs import FileRef
    from services.media.workspace import (
        resolve_media,
        workspace_file_url,
        workspace_root,
    )

    resolved = resolve_media(raw_path, ctx=ctx)
    if not resolved.is_file():
        raise NodeUserError(f"File not found in workspace: {raw_path}")

    root = workspace_root(ctx).resolve(strict=False)
    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError as exc:  # pragma: no cover - resolve_media contains
        raise NodeUserError(
            f"Path is outside this workflow's workspace: {raw_path}"
        ) from exc

    stat = resolved.stat()
    name = resolved.name
    return FileRef(
        path=rel,
        workflow_id=ctx.workflow_id,
        filename=name,
        mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        url=workspace_file_url(ctx.workflow_id, rel),
    ).model_dump(mode="json")


def _collect_connected_refs(
    payload: Any, *, limit: int
) -> List[Dict[str, Any]]:
    """Structural scan for serialized FileRefs in connected outputs.

    Mirrors the frontend's structural AudioRef detection: a dict is a ref iff
    it validates as one (``extra="forbid"`` makes near-misses fail), so a
    gallery listing row is skipped while the ``ref`` nested inside it is
    found. Deduped by path, order-preserving, depth- and count-capped.
    """
    from services.media.refs import AudioRef, FileKind, FileRef

    kinds = set(get_args(FileKind))
    found: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()

    def try_ref(candidate: Dict[str, Any]) -> bool:
        if candidate.get("kind") not in kinds:
            return False
        if not candidate.get("path") or not candidate.get("filename"):
            return False
        model = AudioRef if candidate.get("kind") == "audio" else FileRef
        try:
            ref = model.model_validate(candidate)
        except ValidationError:
            return False
        if ref.path in seen_paths:
            return True
        seen_paths.add(ref.path)
        found.append(ref.model_dump(mode="json"))
        return True

    def walk(value: Any, depth: int) -> None:
        if len(found) >= limit or depth > _SCAN_MAX_DEPTH:
            return
        if isinstance(value, dict):
            if try_ref(value):
                return
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)

    walk(payload, 0)
    return found[:limit]


# --- self-registration on import -------------------------------------------
# The Canvas panel and docked sidebar read/mutate the board through these WS
# handlers (self-contained plugin-folder pattern — core router needs no
# edit). See nodes/tool/write_todos/__init__.py for the template.
from services.ws_handler_registry import register_ws_handlers  # noqa: E402

from ._handlers import WS_HANDLERS  # noqa: E402

register_ws_handlers(WS_HANDLERS)
