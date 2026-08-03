"""Durable, namespaced storage for the explicit Simple Memory tool.

This module deliberately owns its tables instead of adding memory-specific
methods to :mod:`core.database`. Importing the Simple Memory plugin registers
the SQLModel tables before normal ``Database.startup()`` calls
``SQLModel.metadata.create_all``; :meth:`MemoryToolStore.ensure_schema` is a
defensive path for standalone workers/tests that import the plugin later.

Memory retrieval is lexical and durable. SQLite uses an FTS5 projection when
available, with parameterized SQL/LIKE fallback on every platform. Embedding
projections are rebuildable accelerators: generation failure is recorded but
never makes the authoritative item unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, DateTime, JSON, Text, delete, func, or_, text
from sqlmodel import Field, SQLModel, select

from core.logging import get_logger

logger = get_logger(__name__)

EmbeddingFunction = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryToolNamespace(SQLModel, table=True):
    """Server-controlled isolation boundary for one Simple Memory node."""

    __tablename__ = "agent_memory_namespaces"

    id: str = Field(primary_key=True, max_length=80)
    owner_id: str = Field(default="owner", index=True, max_length=255)
    workflow_id: str = Field(index=True, max_length=255)
    memory_node_id: str = Field(index=True, max_length=255)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class MemoryToolItem(SQLModel, table=True):
    """Authoritative durable memory item."""

    __tablename__ = "agent_memory_items"

    id: str = Field(primary_key=True, max_length=64)
    namespace_id: str = Field(index=True, max_length=80)
    content: str = Field(sa_column=Column(Text, nullable=False))
    title: Optional[str] = Field(default=None, max_length=500)
    category: Optional[str] = Field(default=None, index=True, max_length=100)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    version: int = Field(default=1, ge=1)
    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class MemoryToolEmbeddingProjection(SQLModel, table=True):
    """Rebuildable semantic projection; never the source of truth."""

    __tablename__ = "agent_memory_embedding_projections"

    item_id: str = Field(primary_key=True, max_length=64)
    namespace_id: str = Field(index=True, max_length=80)
    # SQLModel 0.0.x cannot map typing.Literal directly on table models.
    # Service code constrains this field to the two values below.
    status: str = Field(max_length=20)
    vector: Optional[list[float]] = Field(default=None, sa_column=Column(JSON))
    error: Optional[str] = Field(default=None, max_length=2000)
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


@dataclass(frozen=True)
class MemoryScope:
    """Trusted scope derived from NodeContext, never from LLM arguments."""

    owner_id: str
    workflow_id: str
    memory_node_id: str

    @property
    def namespace_id(self) -> str:
        material = "\0".join(
            (self.owner_id, self.workflow_id, self.memory_node_id)
        ).encode("utf-8")
        return "mem_" + hashlib.sha256(material).hexdigest()[:48]


class MemoryStoreError(ValueError):
    """Base class for user-correctable memory mutations."""


class MemoryNotFoundError(MemoryStoreError):
    pass


class MemoryVersionConflictError(MemoryStoreError):
    pass


class MemoryItemView(BaseModel):
    id: str
    content: str
    title: Optional[str] = None
    category: Optional[str] = None
    tags: list[str]
    version: int
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    indexing_state: Literal["lexical", "embedding_ready", "embedding_failed"] = (
        "lexical"
    )

    model_config = ConfigDict(from_attributes=True)


def _normalize_tags(tags: Optional[Iterable[str]]) -> list[str]:
    normalized: list[str] = []
    for tag in tags or ():
        value = str(tag).strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_categories(categories: Optional[Iterable[str]]) -> list[str]:
    return [
        value
        for value in dict.fromkeys(
            str(category).strip().lower() for category in categories or ()
        )
        if value
    ]


def _encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        offset = int(decoded["offset"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MemoryStoreError("Invalid memory cursor") from exc
    if offset < 0 or offset > 1_000_000:
        raise MemoryStoreError("Invalid memory cursor")
    return offset


class MemoryToolStore:
    """Transactional CRUD/search service for one database instance."""

    _schema_lock = asyncio.Lock()
    _initialized_engines: set[Any] = set()
    _engine_fts: dict[Any, bool] = {}

    def __init__(
        self,
        database: Any,
        *,
        embedder: Optional[EmbeddingFunction] = None,
    ) -> None:
        self.database = database
        self.embedder = embedder
        self._fts_available = False

    async def ensure_schema(self) -> None:
        engine = getattr(self.database, "engine", None)
        if engine is None:
            raise RuntimeError("Database is not initialized")
        if engine in self._initialized_engines:
            self._fts_available = self._engine_fts.get(engine, False)
            return
        async with self._schema_lock:
            if engine in self._initialized_engines:
                self._fts_available = self._engine_fts.get(engine, False)
                return
            tables = (
                MemoryToolNamespace.__table__,
                MemoryToolItem.__table__,
                MemoryToolEmbeddingProjection.__table__,
            )
            async with engine.begin() as connection:
                for table_obj in tables:
                    await connection.run_sync(
                        lambda sync_connection, table_obj=table_obj: table_obj.create(
                            sync_connection, checkfirst=True
                        )
                    )
                if engine.dialect.name == "sqlite":
                    try:
                        await connection.execute(
                            text(
                                "CREATE VIRTUAL TABLE IF NOT EXISTS "
                                "agent_memory_items_fts USING fts5("
                                "memory_id UNINDEXED, namespace_id UNINDEXED, "
                                "title, content, category, tags)"
                            )
                        )
                        self._fts_available = True
                    except Exception:
                        logger.warning(
                            "SQLite FTS5 unavailable; memory recall will use SQL",
                            exc_info=True,
                        )
                        self._fts_available = False
            self._initialized_engines.add(engine)
            self._engine_fts[engine] = self._fts_available

    async def _ensure_namespace(self, session: Any, scope: MemoryScope) -> None:
        namespace = await session.get(MemoryToolNamespace, scope.namespace_id)
        if namespace is None:
            session.add(
                MemoryToolNamespace(
                    id=scope.namespace_id,
                    owner_id=scope.owner_id,
                    workflow_id=scope.workflow_id,
                    memory_node_id=scope.memory_node_id,
                )
            )
            await session.flush()
            return
        # The identifier is content-addressed, but verify its material anyway
        # so a database corruption cannot collapse tenant/node boundaries.
        if (
            namespace.owner_id != scope.owner_id
            or namespace.workflow_id != scope.workflow_id
            or namespace.memory_node_id != scope.memory_node_id
        ):
            raise RuntimeError("Memory namespace integrity check failed")
        namespace.updated_at = _utcnow()
        session.add(namespace)

    @staticmethod
    def _active_clause() -> Any:
        now = _utcnow()
        return or_(
            MemoryToolItem.expires_at.is_(None),
            MemoryToolItem.expires_at > now,
        )

    async def _projection_states(
        self, session: Any, item_ids: Sequence[str]
    ) -> dict[str, str]:
        if not item_ids:
            return {}
        result = await session.execute(
            select(MemoryToolEmbeddingProjection).where(
                MemoryToolEmbeddingProjection.item_id.in_(item_ids)
            )
        )
        return {row.item_id: row.status for row in result.scalars().all()}

    @staticmethod
    def _serialize_item(
        item: MemoryToolItem, projection_status: Optional[str] = None
    ) -> dict[str, Any]:
        if projection_status == "ready":
            indexing_state = "embedding_ready"
        elif projection_status == "failed":
            indexing_state = "embedding_failed"
        else:
            indexing_state = "lexical"
        return MemoryItemView(
            id=item.id,
            content=item.content,
            title=item.title,
            category=item.category,
            tags=list(item.tags or []),
            version=item.version,
            expires_at=item.expires_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            indexing_state=indexing_state,
        ).model_dump(mode="json")

    async def _fts_upsert(self, session: Any, item: MemoryToolItem) -> None:
        if not self._fts_available:
            return
        try:
            await session.execute(
                text(
                    "DELETE FROM agent_memory_items_fts "
                    "WHERE memory_id = :memory_id"
                ),
                {"memory_id": item.id},
            )
            await session.execute(
                text(
                    "INSERT INTO agent_memory_items_fts "
                    "(memory_id, namespace_id, title, content, category, tags) "
                    "VALUES (:memory_id, :namespace_id, :title, :content, "
                    ":category, :tags)"
                ),
                {
                    "memory_id": item.id,
                    "namespace_id": item.namespace_id,
                    "title": item.title or "",
                    "content": item.content,
                    "category": item.category or "",
                    "tags": " ".join(item.tags or []),
                },
            )
        except Exception:
            # FTS is an acceleration projection. Losing it must not roll back
            # an otherwise valid authoritative item mutation.
            logger.warning("Failed to update memory FTS projection", exc_info=True)
            self._fts_available = False
            engine = getattr(self.database, "engine", None)
            if engine is not None:
                self._engine_fts[engine] = False

    async def _fts_delete(self, session: Any, item_id: str) -> None:
        if not self._fts_available:
            return
        try:
            await session.execute(
                text(
                    "DELETE FROM agent_memory_items_fts "
                    "WHERE memory_id = :memory_id"
                ),
                {"memory_id": item_id},
            )
        except Exception:
            logger.warning("Failed to delete memory FTS projection", exc_info=True)
            self._fts_available = False
            engine = getattr(self.database, "engine", None)
            if engine is not None:
                self._engine_fts[engine] = False

    async def _refresh_embedding(
        self, namespace_id: str, item_id: str, content: str
    ) -> str:
        if self.embedder is None:
            return "lexical"
        status: Literal["ready", "failed"]
        vector: Optional[list[float]]
        error: Optional[str]
        try:
            generated = self.embedder(content)
            if inspect.isawaitable(generated):
                generated = await generated
            vector = [float(value) for value in generated]
            if not vector:
                raise ValueError("embedding is empty")
            status, error = "ready", None
        except Exception as exc:
            status, vector = "failed", None
            error = str(exc)[:2000]
            logger.warning(
                "Memory embedding generation failed; lexical index remains active",
                item_id=item_id,
                exc_info=True,
            )
        async with self.database.get_session() as session:
            row = await session.get(MemoryToolEmbeddingProjection, item_id)
            if row is None:
                row = MemoryToolEmbeddingProjection(
                    item_id=item_id,
                    namespace_id=namespace_id,
                    status=status,
                )
            row.status = status
            row.vector = vector
            row.error = error
            row.updated_at = _utcnow()
            session.add(row)
            await session.commit()
        return "embedding_ready" if status == "ready" else "embedding_failed"

    async def remember(
        self,
        scope: MemoryScope,
        *,
        content: str,
        title: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        expires_at: Optional[datetime] = None,
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        item_id = uuid.uuid4().hex
        normalized_category = str(category).strip().lower() if category else None
        normalized_tags = _normalize_tags(tags)

        async def _mutate(session: Any) -> dict[str, Any]:
            await self._ensure_namespace(session, scope)
            item = MemoryToolItem(
                id=item_id,
                namespace_id=scope.namespace_id,
                content=content,
                title=title,
                category=normalized_category,
                tags=normalized_tags,
                expires_at=expires_at,
            )
            session.add(item)
            await session.flush()
            await self._fts_upsert(session, item)
            return {
                "operation": "remember",
                "memory": self._serialize_item(item),
            }

        result, applied = await self.database.run_runtime_mutation(
            resource_type="agent_memory",
            resource_id=scope.namespace_id,
            operation="remember",
            mutate=_mutate,
            mutation_id=operation_id,
        )
        if applied:
            state = await self._refresh_embedding(
                scope.namespace_id, item_id, content
            )
            if isinstance(result.get("memory"), dict):
                result["memory"]["indexing_state"] = state
        result["receipt"] = {
            "operation_id": operation_id,
            "applied": applied,
        }
        return result

    async def get(
        self, scope: MemoryScope, memory_id: str
    ) -> dict[str, Any]:
        await self.ensure_schema()
        async with self.database.get_session() as session:
            result = await session.execute(
                select(MemoryToolItem).where(
                    MemoryToolItem.id == memory_id,
                    MemoryToolItem.namespace_id == scope.namespace_id,
                    self._active_clause(),
                )
            )
            item = result.scalar_one_or_none()
            if item is None:
                raise MemoryNotFoundError("Memory item not found")
            states = await self._projection_states(session, [item.id])
            return {
                "operation": "get",
                "memory": self._serialize_item(item, states.get(item.id)),
            }

    async def list(
        self,
        scope: MemoryScope,
        *,
        categories: Optional[Iterable[str]] = None,
        tags: Optional[Iterable[str]] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        category_filter = _normalize_categories(categories)
        tag_filter = set(_normalize_tags(tags))
        offset = _decode_cursor(cursor)
        clauses = [
            MemoryToolItem.namespace_id == scope.namespace_id,
            self._active_clause(),
        ]
        if category_filter:
            clauses.append(MemoryToolItem.category.in_(category_filter))
        async with self.database.get_session() as session:
            # Tags live in portable JSON. Scan bounded chunks so exact tag
            # filtering behaves the same on SQLite and Postgres.
            collected: list[tuple[MemoryToolItem, int]] = []
            scanned = offset
            while len(collected) < limit + 1 and scanned < offset + 1000:
                batch_start = scanned
                batch_size = max(50, limit * 2)
                batch_result = await session.execute(
                    select(MemoryToolItem)
                    .where(*clauses)
                    .order_by(
                        MemoryToolItem.created_at.desc(),
                        MemoryToolItem.id.desc(),
                    )
                    .offset(scanned)
                    .limit(batch_size)
                )
                batch = list(batch_result.scalars().all())
                if not batch:
                    break
                for index, item in enumerate(batch):
                    scanned = batch_start + index + 1
                    if tag_filter and not tag_filter.issubset(set(item.tags or [])):
                        continue
                    collected.append((item, scanned))
                    if len(collected) >= limit + 1:
                        break
                if len(batch) < batch_size:
                    break
            page_entries = collected[:limit]
            page = [item for item, _position in page_entries]
            states = await self._projection_states(
                session, [item.id for item in page]
            )
            has_more = len(collected) > limit
            next_offset = (
                page_entries[-1][1] if page_entries else scanned
            )
            return {
                "operation": "list",
                "items": [
                    self._serialize_item(item, states.get(item.id))
                    for item in page
                ],
                "count": len(page),
                "next_cursor": (
                    _encode_cursor(next_offset) if has_more else None
                ),
                "retrieval": "sql",
            }

    async def _fts_ids(
        self, namespace_id: str, query: str
    ) -> Optional[list[str]]:
        if not self._fts_available:
            return None
        tokens = [
            token.lower()
            for token in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
            if token
        ][:20]
        if not tokens:
            return []
        # Tokens are reduced to Unicode word characters before interpolation;
        # all scope/data remains parameterized.
        match_query = " AND ".join(f'"{token}"*' for token in tokens)
        try:
            async with self.database.get_session() as session:
                result = await session.execute(
                    text(
                        "SELECT memory_id FROM agent_memory_items_fts "
                        "WHERE namespace_id = :namespace_id "
                        "AND agent_memory_items_fts MATCH :query "
                        "ORDER BY bm25(agent_memory_items_fts) LIMIT 1000"
                    ),
                    {"namespace_id": namespace_id, "query": match_query},
                )
                return [str(row[0]) for row in result.fetchall()]
        except Exception:
            logger.warning(
                "Memory FTS query failed; using SQL lexical fallback",
                exc_info=True,
            )
            self._fts_available = False
            engine = getattr(self.database, "engine", None)
            if engine is not None:
                self._engine_fts[engine] = False
            return None

    async def recall(
        self,
        scope: MemoryScope,
        *,
        query: str,
        categories: Optional[Iterable[str]] = None,
        tags: Optional[Iterable[str]] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        category_filter = _normalize_categories(categories)
        tag_filter = set(_normalize_tags(tags))
        offset = _decode_cursor(cursor)
        ranked_ids = await self._fts_ids(scope.namespace_id, query)
        retrieval = "fts" if ranked_ids is not None else "sql"
        async with self.database.get_session() as session:
            clauses = [
                MemoryToolItem.namespace_id == scope.namespace_id,
                self._active_clause(),
            ]
            if category_filter:
                clauses.append(MemoryToolItem.category.in_(category_filter))
            if ranked_ids is not None:
                if not ranked_ids:
                    candidates: list[MemoryToolItem] = []
                else:
                    selected = await session.execute(
                        select(MemoryToolItem).where(
                            *clauses, MemoryToolItem.id.in_(ranked_ids)
                        )
                    )
                    by_id = {item.id: item for item in selected.scalars().all()}
                    candidates = [
                        by_id[item_id]
                        for item_id in ranked_ids
                        if item_id in by_id
                    ]
            else:
                terms = [
                    token.lower()
                    for token in re.findall(
                        r"[^\W_]+", query, flags=re.UNICODE
                    )
                    if token
                ][:20]
                for term in terms:
                    pattern = f"%{term}%"
                    clauses.append(
                        or_(
                            func.lower(MemoryToolItem.content).like(pattern),
                            func.lower(
                                func.coalesce(MemoryToolItem.title, "")
                            ).like(pattern),
                            func.lower(
                                func.coalesce(MemoryToolItem.category, "")
                            ).like(pattern),
                        )
                    )
                selected = await session.execute(
                    select(MemoryToolItem)
                    .where(*clauses)
                    .order_by(
                        MemoryToolItem.updated_at.desc(),
                        MemoryToolItem.id.desc(),
                    )
                    .limit(1000)
                )
                candidates = list(selected.scalars().all())
            filtered = [
                item
                for item in candidates
                if not tag_filter
                or tag_filter.issubset(set(item.tags or []))
            ]
            page = filtered[offset : offset + limit]
            states = await self._projection_states(
                session, [item.id for item in page]
            )
            next_offset = offset + len(page)
            return {
                "operation": "recall",
                "items": [
                    self._serialize_item(item, states.get(item.id))
                    for item in page
                ],
                "count": len(page),
                "next_cursor": (
                    _encode_cursor(next_offset)
                    if next_offset < len(filtered)
                    else None
                ),
                "retrieval": retrieval,
            }

    async def update(
        self,
        scope: MemoryScope,
        *,
        memory_id: str,
        expected_version: int,
        patch: dict[str, Any],
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.ensure_schema()

        async def _mutate(session: Any) -> dict[str, Any]:
            result = await session.execute(
                select(MemoryToolItem)
                .where(
                    MemoryToolItem.id == memory_id,
                    MemoryToolItem.namespace_id == scope.namespace_id,
                    self._active_clause(),
                )
                .with_for_update()
            )
            item = result.scalar_one_or_none()
            if item is None:
                raise MemoryNotFoundError("Memory item not found")
            if item.version != expected_version:
                raise MemoryVersionConflictError(
                    f"Memory version conflict: expected {expected_version}, "
                    f"current {item.version}"
                )
            for key, value in patch.items():
                if key == "tags":
                    value = _normalize_tags(value)
                elif key == "category" and value is not None:
                    value = str(value).strip().lower() or None
                setattr(item, key, value)
            item.version += 1
            item.updated_at = _utcnow()
            session.add(item)
            await session.flush()
            await self._fts_upsert(session, item)
            return {
                "operation": "update",
                "memory": self._serialize_item(item),
            }

        result, applied = await self.database.run_runtime_mutation(
            resource_type="agent_memory",
            resource_id=scope.namespace_id,
            operation="update",
            mutate=_mutate,
            mutation_id=operation_id,
        )
        memory = result.get("memory") or {}
        if applied and memory:
            state = await self._refresh_embedding(
                scope.namespace_id,
                memory_id,
                str(memory.get("content") or ""),
            )
            memory["indexing_state"] = state
        result["receipt"] = {
            "operation_id": operation_id,
            "applied": applied,
        }
        return result

    async def forget(
        self,
        scope: MemoryScope,
        *,
        memory_id: str,
        expected_version: int,
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.ensure_schema()

        async def _mutate(session: Any) -> dict[str, Any]:
            result = await session.execute(
                select(MemoryToolItem)
                .where(
                    MemoryToolItem.id == memory_id,
                    MemoryToolItem.namespace_id == scope.namespace_id,
                )
                .with_for_update()
            )
            item = result.scalar_one_or_none()
            if item is None:
                raise MemoryNotFoundError("Memory item not found")
            if item.version != expected_version:
                raise MemoryVersionConflictError(
                    f"Memory version conflict: expected {expected_version}, "
                    f"current {item.version}"
                )
            forgotten = self._serialize_item(item)
            await self._fts_delete(session, item.id)
            await session.execute(
                delete(MemoryToolEmbeddingProjection).where(
                    MemoryToolEmbeddingProjection.item_id == item.id
                )
            )
            await session.delete(item)
            return {"operation": "forget", "memory": forgotten}

        result, applied = await self.database.run_runtime_mutation(
            resource_type="agent_memory",
            resource_id=scope.namespace_id,
            operation="forget",
            mutate=_mutate,
            mutation_id=operation_id,
        )
        result["receipt"] = {
            "operation_id": operation_id,
            "applied": applied,
        }
        return result

    async def clear_namespace(
        self,
        scope: MemoryScope,
        *,
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Clear durable items only; namespace and receipts remain durable."""
        await self.ensure_schema()

        async def _mutate(session: Any) -> dict[str, Any]:
            await self._ensure_namespace(session, scope)
            result = await session.execute(
                select(MemoryToolItem.id).where(
                    MemoryToolItem.namespace_id == scope.namespace_id
                )
            )
            item_ids = list(result.scalars().all())
            if self._fts_available:
                try:
                    await session.execute(
                        text(
                            "DELETE FROM agent_memory_items_fts "
                            "WHERE namespace_id = :namespace_id"
                        ),
                        {"namespace_id": scope.namespace_id},
                    )
                except Exception:
                    logger.warning(
                        "Failed to clear memory FTS projection", exc_info=True
                    )
                    self._fts_available = False
                    engine = getattr(self.database, "engine", None)
                    if engine is not None:
                        self._engine_fts[engine] = False
            await session.execute(
                delete(MemoryToolEmbeddingProjection).where(
                    MemoryToolEmbeddingProjection.namespace_id
                    == scope.namespace_id
                )
            )
            await session.execute(
                delete(MemoryToolItem).where(
                    MemoryToolItem.namespace_id == scope.namespace_id
                )
            )
            return {"operation": "clear", "cleared": len(item_ids)}

        result, applied = await self.database.run_runtime_mutation(
            resource_type="agent_memory",
            resource_id=scope.namespace_id,
            operation="clear",
            mutate=_mutate,
            mutation_id=operation_id,
        )
        result["receipt"] = {
            "operation_id": operation_id,
            "applied": applied,
        }
        return result


__all__ = [
    "EmbeddingFunction",
    "MemoryItemView",
    "MemoryNotFoundError",
    "MemoryScope",
    "MemoryStoreError",
    "MemoryToolEmbeddingProjection",
    "MemoryToolItem",
    "MemoryToolNamespace",
    "MemoryToolStore",
    "MemoryVersionConflictError",
]
