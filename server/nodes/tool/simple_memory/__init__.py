"""Simple Memory V2 — an explicit, durable AI tool.

Conversation replay belongs to the companion Context node. This plugin stores
only memories the model explicitly asks to remember and never injects,
retrieves, or compacts agent transcripts automatically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from services.memory.tool_store import (
    MemoryScope,
    MemoryStoreError,
    MemoryToolStore,
)
from services.plugin import (
    NodeContext,
    NodeUserError,
    Operation,
    TaskQueue,
    ToolNode,
)
from services.plugin.deps import get_database

MemoryOperation = Literal[
    "remember",
    "recall",
    "list",
    "get",
    "update",
    "forget",
]


class SimpleMemoryParams(BaseModel):
    """Persisted operator configuration; never exposed as model arguments."""

    reset_policy: Literal["preserve", "clear"] = Field(
        default="preserve",
        title="Workflow Reset",
        description=(
            "Preserve durable memory items across Workflow Reset, or clear "
            "only this Memory node's isolated namespace."
        ),
    )

    # Legacy graph imports may still carry markdown/session/vector fields.
    # Ignoring them keeps graph loading compatible without reintroducing them
    # into runtime behavior or the NodeSpec.
    model_config = ConfigDict(extra="ignore")


class MemoryUpdatePatch(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=50_000)
    title: Optional[str] = Field(default=None, max_length=500)
    category: Optional[str] = Field(default=None, max_length=100)
    tags: Optional[list[str]] = Field(default=None, max_length=20)
    expires_at: Optional[datetime] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: Optional[list[str]]) -> Optional[list[str]]:
        if tags is not None and any(len(str(tag).strip()) > 64 for tag in tags):
            raise ValueError("tags must be at most 64 characters each")
        return tags


class SimpleMemoryToolInput(BaseModel):
    """One locked, multi-operation schema visible to the LLM."""

    operation: MemoryOperation = Field(
        description=(
            "Memory operation: remember, recall, list, get, update, or forget."
        )
    )
    content: Optional[str] = Field(default=None, min_length=1, max_length=50_000)
    title: Optional[str] = Field(default=None, max_length=500)
    category: Optional[str] = Field(default=None, max_length=100)
    tags: Optional[list[str]] = Field(default=None, max_length=20)
    expires_at: Optional[datetime] = None
    query: Optional[str] = Field(default=None, min_length=1, max_length=2_000)
    categories: Optional[list[str]] = Field(default=None, max_length=20)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: Optional[str] = Field(default=None, max_length=4_096)
    memory_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    expected_version: Optional[int] = Field(default=None, ge=1)
    patch: Optional[MemoryUpdatePatch] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags", "categories")
    @classmethod
    def _validate_filters(
        cls, values: Optional[list[str]]
    ) -> Optional[list[str]]:
        if values is not None and any(
            not str(value).strip() or len(str(value).strip()) > 100
            for value in values
        ):
            raise ValueError(
                "categories/tags must be non-empty and at most 100 characters"
            )
        return values

    @model_validator(mode="after")
    def _validate_operation_fields(self) -> "SimpleMemoryToolInput":
        required: dict[str, tuple[str, ...]] = {
            "remember": ("content",),
            "recall": ("query",),
            "get": ("memory_id",),
            "update": ("memory_id", "expected_version", "patch"),
            "forget": ("memory_id", "expected_version"),
        }
        missing = [
            field_name
            for field_name in required.get(self.operation, ())
            if getattr(self, field_name) is None
        ]
        if missing:
            raise ValueError(
                f"{self.operation} requires {', '.join(missing)}"
            )
        if (
            self.operation == "update"
            and self.patch is not None
            and not self.patch.model_fields_set
        ):
            raise ValueError("update patch must contain at least one field")
        return self


class SimpleMemoryOutput(BaseModel):
    operation: str
    memory: Optional[dict[str, Any]] = None
    items: Optional[list[dict[str, Any]]] = None
    count: Optional[int] = None
    next_cursor: Optional[str] = None
    retrieval: Optional[str] = None
    receipt: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


class SimpleMemoryNode(ToolNode):
    type = "simpleMemory"
    display_name = "Simple Memory"
    subtitle = "Explicit Durable Memory"
    group = ("tool", "memory")
    description = (
        "Explicitly remember, recall, list, update, and forget durable facts"
    )
    component_kind = "tool"
    tool_name = "memory"
    tool_description = (
        "Store and retrieve durable memories only when useful. Use remember "
        "for stable facts or decisions; use recall/list/get to retrieve them; "
        "use update/forget with the item's expected_version."
    )
    handles = (
        {
            "name": "output-tool",
            "kind": "output",
            "position": "top",
            "label": "Memory",
            "role": "tools",
        },
    )
    ui_hints = {
        "isToolPanel": True,
        "isMemoryPanel": True,
        "isMemoryToolPanel": True,
        # Kept as a wire-compatibility hint for older clients. V2 clients
        # select isMemoryToolPanel first; there is no markdown field to edit.
        "hasCodeEditor": True,
        "hideInputSection": True,
        "hideOutputSection": True,
        "hideRunButton": True,
    }
    annotations = {
        "destructive": True,
        "readonly": False,
        "open_world": False,
    }
    task_queue = TaskQueue.DEFAULT

    Params = SimpleMemoryParams
    ToolInput = SimpleMemoryToolInput
    Output = SimpleMemoryOutput
    tool_schema_locked = True
    server_controlled_fields = frozenset({"reset_policy"})

    @staticmethod
    def _scope(ctx: NodeContext) -> MemoryScope:
        return MemoryScope(
            owner_id=str(ctx.user_id or ctx.raw.get("user_id") or "owner"),
            workflow_id=str(
                ctx.workflow_id or ctx.raw.get("workflow_id") or "local"
            ),
            memory_node_id=str(ctx.node_id),
        )

    @staticmethod
    def _operation_id(ctx: NodeContext) -> Optional[str]:
        value = (
            ctx.raw.get("operation_id")
            or ctx.raw.get("tool_call_id")
            or ctx.raw.get("mutation_id")
        )
        return str(value)[:512] if value else None

    @Operation("memory")
    async def memory(
        self,
        ctx: NodeContext,
        params: SimpleMemoryToolInput | SimpleMemoryParams,
    ) -> SimpleMemoryOutput:
        # The node's Run button is hidden. Treat a framework-side execution as
        # a harmless list for diagnostics instead of making it a second API.
        args = (
            SimpleMemoryToolInput(operation="list")
            if isinstance(params, SimpleMemoryParams)
            else params
        )
        store = MemoryToolStore(get_database())
        scope = self._scope(ctx)
        operation_id = self._operation_id(ctx)
        try:
            if args.operation == "remember":
                result = await store.remember(
                    scope,
                    content=args.content or "",
                    title=args.title,
                    category=args.category,
                    tags=args.tags,
                    expires_at=args.expires_at,
                    operation_id=operation_id,
                )
            elif args.operation == "recall":
                result = await store.recall(
                    scope,
                    query=args.query or "",
                    categories=args.categories,
                    tags=args.tags,
                    limit=args.limit,
                    cursor=args.cursor,
                )
            elif args.operation == "list":
                result = await store.list(
                    scope,
                    categories=args.categories,
                    tags=args.tags,
                    limit=args.limit,
                    cursor=args.cursor,
                )
            elif args.operation == "get":
                result = await store.get(scope, args.memory_id or "")
            elif args.operation == "update":
                assert args.patch is not None
                result = await store.update(
                    scope,
                    memory_id=args.memory_id or "",
                    expected_version=args.expected_version or 0,
                    patch=args.patch.model_dump(exclude_unset=True),
                    operation_id=operation_id,
                )
            else:
                result = await store.forget(
                    scope,
                    memory_id=args.memory_id or "",
                    expected_version=args.expected_version or 0,
                    operation_id=operation_id,
                )
        except MemoryStoreError as exc:
            raise NodeUserError(str(exc)) from exc
        return SimpleMemoryOutput.model_validate(result)

    @classmethod
    async def reset_execution_state(
        cls,
        *,
        node_id: str,
        workflow_id: str,
        execution_id: str,
        generation: int,
        graph: dict[str, Any],
        database: Any,
    ) -> dict[str, Any]:
        del generation
        persisted = await database.get_node_parameters(node_id) or {}
        config = SimpleMemoryParams.model_validate(persisted)
        if config.reset_policy == "preserve":
            return {
                "reset": False,
                "reset_policy": "preserve",
                "preserved": True,
            }
        owner_id = str(graph.get("owner_id") or graph.get("user_id") or "owner")
        scope = MemoryScope(
            owner_id=owner_id,
            workflow_id=str(workflow_id),
            memory_node_id=str(node_id),
        )
        result = await MemoryToolStore(database).clear_namespace(
            scope,
            operation_id=f"workflow-reset:{execution_id}:{node_id}",
        )
        return {
            "reset": True,
            "reset_policy": "clear",
            **result,
        }


# Plugin-owned side-channel API for the Memory panel.
from services.ws_handler_registry import register_ws_handlers  # noqa: E402

from ._handlers import WS_HANDLERS  # noqa: E402

register_ws_handlers(WS_HANDLERS)


__all__ = [
    "WS_HANDLERS",
    "MemoryOperation",
    "MemoryUpdatePatch",
    "SimpleMemoryNode",
    "SimpleMemoryOutput",
    "SimpleMemoryParams",
    "SimpleMemoryToolInput",
]
