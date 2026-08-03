"""Durable Agent Context V2 contracts and normalized persistence records.

The public models in this module are deliberately payload-reference oriented.
Only :class:`AgentContextEvent` may expose an exact ``MessageWireV2`` value,
and only when an authorized caller explicitly loads the journal.  Workflow
graphs, node parameters, Temporal commands, status events, and ordinary node
outputs use :class:`AgentContextRef` instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field as PydanticField
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    JSON,
    LargeBinary,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


ContextThreadKind = Literal["session", "task", "execution"]
ContextFidelity = Literal[
    "provider_replayable",
    "provider_bound",
    "observable_only",
]


class AgentContextRef(BaseModel):
    """Bounded reference carried by runtimes and Temporal histories."""

    workflow_id: str
    context_node_id: str
    generation: int
    thread_id: str
    epoch: int
    revision: int

    model_config = ConfigDict(frozen=True)


class AgentContextEvent(BaseModel):
    """One exact, hash-chained observable transition in the raw journal."""

    sequence: int = PydanticField(ge=1)
    event_type: str
    message_wire_v2: Optional[dict[str, Any]] = None
    payload_ref: Optional[str] = None
    operation_id: str
    provider: Optional[str] = None
    previous_hash: str
    payload_hash: str

    model_config = ConfigDict(frozen=True)


class AgentContextCheckpoint(BaseModel):
    """Provider-replayable checkpoint activated over a committed prefix."""

    provider: str
    strategy: str
    covers_through_sequence: int = PydanticField(ge=0)
    replay_payload_ref: str
    active_token_count: int = PydanticField(ge=0)
    source_revision: int = PydanticField(ge=0)
    source_hash: str

    model_config = ConfigDict(frozen=True)


class AgentContextActiveState(BaseModel):
    """Checkpoint plus the exact, still-uncompacted event tail."""

    ref: AgentContextRef
    checkpoint: Optional[AgentContextCheckpoint] = None
    tail: list[AgentContextEvent] = PydanticField(default_factory=list)
    active_token_count: int = PydanticField(default=0, ge=0)

    model_config = ConfigDict(frozen=True)


class AgentContextAppendResult(BaseModel):
    ref: AgentContextRef
    event: AgentContextEvent
    applied: bool

    model_config = ConfigDict(frozen=True)


class AgentContextCompactionPlan(BaseModel):
    """Lease returned to a compactor before it performs provider work."""

    attempt_id: int
    ref: AgentContextRef
    provider: str
    strategy: str
    base_checkpoint: Optional[AgentContextCheckpoint] = None
    committed_prefix: list[AgentContextEvent]
    covers_through_sequence: int
    source_revision: int
    source_hash: str

    model_config = ConfigDict(frozen=True)


class AgentContextProviderBinding(BaseModel):
    """Metadata-only view of an external provider continuation binding."""

    provider: str
    binding_type: str
    binding_ref: str
    epoch: int
    fidelity: ContextFidelity

    model_config = ConfigDict(frozen=True)


class AgentContextThreadSummary(BaseModel):
    """Metadata-only thread listing for an authorized Context panel."""

    ref: AgentContextRef
    thread_kind: ContextThreadKind
    provider: Optional[str] = None
    status: str
    active_checkpoint_sequence: int = PydanticField(ge=0)
    active_token_count: int = PydanticField(ge=0)
    updated_at: datetime

    model_config = ConfigDict(frozen=True)


class AgentContextThreadRecord(SQLModel, table=True):
    """Current control row for one isolated Context thread."""

    __tablename__ = "agent_context_threads"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "context_node_id",
            "generation",
            "thread_id",
            name="uq_agent_context_thread",
        ),
        Index(
            "ix_agent_context_node_generation",
            "workflow_id",
            "context_node_id",
            "generation",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    workflow_id: str = Field(max_length=255)
    context_node_id: str = Field(max_length=255)
    generation: int = Field(index=True)
    thread_id: str = Field(max_length=512)
    thread_kind: str = Field(max_length=20)
    epoch: int = Field(default=1)
    revision: int = Field(default=0)
    next_sequence: int = Field(default=1)
    last_event_hash: str = Field(default="0" * 64, max_length=64)
    active_checkpoint_id: Optional[int] = Field(default=None, index=True)
    active_checkpoint_sequence: int = Field(default=0)
    active_token_count: int = Field(default=0)
    provider: Optional[str] = Field(default=None, max_length=100)
    status: str = Field(default="active", index=True, max_length=20)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
        ),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
        ),
    )
    archived_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class AgentContextEventRecord(SQLModel, table=True):
    """Append-only exact journal entry."""

    __tablename__ = "agent_context_events"
    __table_args__ = (
        UniqueConstraint(
            "context_thread_id",
            "sequence",
            name="uq_agent_context_event_sequence",
        ),
        UniqueConstraint(
            "context_thread_id",
            "operation_id",
            name="uq_agent_context_event_operation",
        ),
        Index(
            "ix_agent_context_event_epoch_sequence",
            "context_thread_id",
            "epoch",
            "sequence",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    context_thread_id: int = Field(
        foreign_key="agent_context_threads.id",
        index=True,
    )
    epoch: int = Field(index=True)
    sequence: int
    event_type: str = Field(max_length=100)
    message_wire_v2: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
    )
    payload_ref: Optional[str] = Field(default=None, max_length=512)
    operation_id: str = Field(max_length=512)
    provider: Optional[str] = Field(default=None, max_length=100)
    previous_hash: str = Field(max_length=64)
    payload_hash: str = Field(max_length=64, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AgentContextCheckpointRecord(SQLModel, table=True):
    """Immutable provider replay payload and its journal coverage."""

    __tablename__ = "agent_context_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "context_thread_id",
            "operation_id",
            name="uq_agent_context_checkpoint_operation",
        ),
        Index(
            "ix_agent_context_checkpoint_epoch",
            "context_thread_id",
            "epoch",
            "covers_through_sequence",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    context_thread_id: int = Field(
        foreign_key="agent_context_threads.id",
        index=True,
    )
    epoch: int = Field(index=True)
    provider: str = Field(max_length=100)
    strategy: str = Field(max_length=100)
    covers_through_sequence: int
    replay_payload_ref: str = Field(max_length=512)
    active_token_count: int = Field(default=0)
    source_revision: int
    source_hash: str = Field(max_length=64)
    operation_id: str = Field(max_length=512)
    status: str = Field(default="active", index=True, max_length=20)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AgentContextProviderBindingRecord(SQLModel, table=True):
    """Opaque external continuation identity stored by payload reference."""

    __tablename__ = "agent_context_provider_bindings"
    __table_args__ = (
        UniqueConstraint(
            "context_thread_id",
            "operation_id",
            name="uq_agent_context_binding_operation",
        ),
        Index(
            "ix_agent_context_binding_lookup",
            "context_thread_id",
            "epoch",
            "provider",
            "binding_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    context_thread_id: int = Field(
        foreign_key="agent_context_threads.id",
        index=True,
    )
    epoch: int = Field(index=True)
    provider: str = Field(max_length=100)
    binding_type: str = Field(max_length=100)
    binding_ref: str = Field(max_length=512)
    binding_hash: str = Field(max_length=64)
    fidelity: str = Field(default="provider_bound", max_length=32)
    operation_id: str = Field(max_length=512)
    status: str = Field(default="active", index=True, max_length=20)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AgentContextCompactionAttemptRecord(SQLModel, table=True):
    """CAS lease separating expensive compaction from checkpoint activation."""

    __tablename__ = "agent_context_compaction_attempts"
    __table_args__ = (
        UniqueConstraint(
            "context_thread_id",
            "operation_id",
            name="uq_agent_context_compaction_operation",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    context_thread_id: int = Field(
        foreign_key="agent_context_threads.id",
        index=True,
    )
    epoch: int = Field(index=True)
    provider: str = Field(max_length=100)
    strategy: str = Field(max_length=100)
    covers_through_sequence: int
    base_checkpoint_id: Optional[int] = Field(default=None)
    base_checkpoint_sequence: int = Field(default=0)
    source_revision: int
    source_hash: str = Field(max_length=64)
    operation_id: str = Field(max_length=512)
    status: str = Field(default="prepared", index=True, max_length=20)
    checkpoint_id: Optional[int] = Field(default=None)
    error_code: Optional[str] = Field(default=None, max_length=255)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class AgentContextBlobRecord(SQLModel, table=True):
    """Optional hash-addressed storage for large or opaque exact payloads."""

    __tablename__ = "agent_context_blobs"

    payload_hash: str = Field(primary_key=True, max_length=64)
    media_type: str = Field(default="application/json", max_length=100)
    json_payload: Optional[Any] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
    )
    binary_payload: Optional[bytes] = Field(
        default=None,
        sa_column=Column(LargeBinary, nullable=True),
    )
    byte_size: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


__all__ = [
    "AgentContextActiveState",
    "AgentContextAppendResult",
    "AgentContextBlobRecord",
    "AgentContextCheckpoint",
    "AgentContextCheckpointRecord",
    "AgentContextCompactionAttemptRecord",
    "AgentContextCompactionPlan",
    "AgentContextEvent",
    "AgentContextEventRecord",
    "AgentContextProviderBinding",
    "AgentContextProviderBindingRecord",
    "AgentContextRef",
    "AgentContextThreadRecord",
    "AgentContextThreadSummary",
    "ContextFidelity",
    "ContextThreadKind",
]
