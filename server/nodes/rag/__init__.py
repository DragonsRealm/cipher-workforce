"""RAG (Retrieval-Augmented Generation) capability node.

Provides a SQLite-backed, per-soul-namespaced vector knowledge base.

Design (Orion D5):
- Shared SQLite instance, per-soul namespace. Soul A cannot query Soul B.
- Embeddings via OpenAI text-embedding-3-small (zero new deps).
- Store is a read cache of the vault: vault → embed → store (one-way).
  Souls retrieve; vault writes are the authoritative path.
- All data at ``~/.cipheros/workforce/rag/rag.db``.

Nodes exposed:
- ``ragStore``  — embed text chunks and store them for a soul
- ``ragQuery``  — query the knowledge base for a soul
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue


class RagStoreParams(BaseModel):
    soul_id: str = Field(..., description="DCS soul identifier (e.g. 'maren', 'orion')")
    chunks: list[str] = Field(..., description="Text chunks to embed and store")
    metadata: Optional[dict] = Field(default=None, description="Optional metadata attached to every chunk")

    model_config = ConfigDict(extra="ignore")


class RagStoreOutput(BaseModel):
    stored: int
    soul_id: str

    model_config = ConfigDict(extra="allow")


class RagQueryParams(BaseModel):
    soul_id: str = Field(..., description="DCS soul identifier — only this soul's chunks are searched")
    text: str = Field(..., description="Query text")
    k: int = Field(default=5, ge=1, le=50, description="Number of results to return")

    model_config = ConfigDict(extra="ignore")


class RagQueryOutput(BaseModel):
    results: list[dict]
    soul_id: str
    count: int

    model_config = ConfigDict(extra="allow")


class RagStoreNode(ActionNode):
    type = "ragStore"
    display_name = "RAG Store"
    subtitle = "Knowledge Base"
    group = ("rag", "tool")
    description = "Embed and store text chunks in the soul-scoped knowledge base"
    component_kind = "square"
    tool_name = "rag_store"
    tool_description = (
        "Embed text chunks and store them in the knowledge base under a soul's namespace. "
        "Chunks are retrievable via ragQuery. soul_id must match the querying soul."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    annotations = {"destructive": False, "readonly": False, "open_world": False}
    task_queue = TaskQueue.DEFAULT
    usable_as_tool = True

    Params = RagStoreParams
    Output = RagStoreOutput

    @Operation("store")
    async def store_op(self, ctx: NodeContext, params: RagStoreParams) -> Any:
        from ._store import embed_and_store

        if not params.chunks:
            raise NodeUserError("chunks must not be empty")

        n = await embed_and_store(
            chunks=params.chunks,
            soul_id=params.soul_id,
            metadata=params.metadata,
        )
        return {"stored": n, "soul_id": params.soul_id}


class RagQueryNode(ActionNode):
    type = "ragQuery"
    display_name = "RAG Query"
    subtitle = "Knowledge Base"
    group = ("rag", "tool")
    description = "Query the soul-scoped knowledge base for relevant chunks"
    component_kind = "square"
    tool_name = "rag_query"
    tool_description = (
        "Query the knowledge base for chunks relevant to a text query. "
        "Only chunks stored under soul_id are searched — isolation is enforced. "
        "Returns up to k results ranked by cosine similarity."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    annotations = {"destructive": False, "readonly": True, "open_world": False}
    task_queue = TaskQueue.DEFAULT
    usable_as_tool = True

    Params = RagQueryParams
    Output = RagQueryOutput

    @Operation("query")
    async def query_op(self, ctx: NodeContext, params: RagQueryParams) -> Any:
        from ._store import query

        if not params.text.strip():
            raise NodeUserError("text must not be empty")

        results = await query(
            soul_id=params.soul_id,
            text=params.text,
            k=params.k,
        )
        return {"results": results, "soul_id": params.soul_id, "count": len(results)}
