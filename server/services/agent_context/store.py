"""Transactional, append-only storage for Agent Context V2."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.agent_context import (
    AgentContextActiveState,
    AgentContextAppendResult,
    AgentContextBlobRecord,
    AgentContextCheckpoint,
    AgentContextCheckpointRecord,
    AgentContextCompactionAttemptRecord,
    AgentContextCompactionPlan,
    AgentContextEvent,
    AgentContextEventRecord,
    AgentContextProviderBinding,
    AgentContextProviderBindingRecord,
    AgentContextRef,
    AgentContextThreadRecord,
    AgentContextThreadSummary,
    ContextFidelity,
    ContextThreadKind,
)
from models.database import RuntimeMutation
from services.agent_context.listeners import notify_context_commit
from services.llm.protocol import (
    message_from_wire,
    message_to_wire,
)


ZERO_HASH = "0" * 64
_MAX_CAS_ATTEMPTS = 12


class AgentContextError(RuntimeError):
    """Base class for stable Context-store failures."""


class ContextNotFoundError(AgentContextError):
    pass


class ContextArchivedError(AgentContextError):
    pass


class StaleEpochError(AgentContextError):
    pass


class RevisionConflictError(AgentContextError):
    pass


class CompactionConflictError(AgentContextError):
    pass


class AgentContextStore:
    """Durable journal/checkpoint repository.

    ``database`` can be the application's :class:`core.database.Database` or
    an ``async_sessionmaker``.  Keeping this boundary tiny makes the same store
    usable from in-process execution and Temporal activities.
    """

    def __init__(self, database: Any):
        self._database = database

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        get_session = getattr(self._database, "get_session", None)
        if callable(get_session):
            async with get_session() as session:
                yield session
            return
        async with self._database() as session:
            yield session

    async def resolve_thread(
        self,
        *,
        workflow_id: str,
        context_node_id: str,
        generation: int,
        session_id: Optional[str] = None,
        delegated_task_id: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> AgentContextRef:
        """Resolve the RFC priority order into one isolated durable thread."""

        kind, source_id = _resolve_thread_identity(
            session_id=session_id,
            delegated_task_id=delegated_task_id,
            execution_id=execution_id,
        )
        thread_id = _thread_id(kind, source_id)
        _validate_identity("workflow_id", workflow_id, 255)
        _validate_identity("context_node_id", context_node_id, 255)
        if generation < 0:
            raise ValueError("generation must be non-negative")

        for _ in range(_MAX_CAS_ATTEMPTS):
            async with self._session() as session:
                row = await _select_thread(
                    session,
                    workflow_id=workflow_id,
                    context_node_id=context_node_id,
                    generation=generation,
                    thread_id=thread_id,
                )
                if row is not None:
                    return _to_ref(row)
                row = AgentContextThreadRecord(
                    workflow_id=workflow_id,
                    context_node_id=context_node_id,
                    generation=generation,
                    thread_id=thread_id,
                    thread_kind=kind,
                    last_event_hash=ZERO_HASH,
                )
                session.add(row)
                try:
                    await session.commit()
                    await session.refresh(row)
                    return _to_ref(row)
                except IntegrityError:
                    await session.rollback()
                    # Another worker resolved the same deterministic thread.
                    continue
        raise RevisionConflictError("context_thread_resolution_conflict")

    async def load_active(
        self,
        ref: AgentContextRef,
    ) -> AgentContextActiveState:
        """Load the active checkpoint and exact tail for the current epoch."""

        async with self._session() as session:
            thread = await self._require_thread(session, ref)
            checkpoint_row: Optional[AgentContextCheckpointRecord] = None
            source_row: Optional[AgentContextEventRecord] = None
            if thread.active_checkpoint_id is not None:
                checkpoint_row = await session.get(
                    AgentContextCheckpointRecord,
                    thread.active_checkpoint_id,
                )
                if (
                    checkpoint_row is None
                    or checkpoint_row.context_thread_id != thread.id
                    or checkpoint_row.epoch != thread.epoch
                    or checkpoint_row.status != "active"
                    or checkpoint_row.covers_through_sequence
                    != thread.active_checkpoint_sequence
                    or checkpoint_row.source_revision > thread.revision
                ):
                    raise AgentContextError("active_checkpoint_invariant_broken")
                source_result = await session.execute(
                    select(AgentContextEventRecord).where(
                        AgentContextEventRecord.context_thread_id
                        == thread.id,
                        AgentContextEventRecord.epoch == thread.epoch,
                        AgentContextEventRecord.sequence
                        == checkpoint_row.covers_through_sequence,
                    )
                )
                source_row = source_result.scalar_one_or_none()
                if (
                    source_row is None
                    or source_row.payload_hash != checkpoint_row.source_hash
                ):
                    raise AgentContextError(
                        "checkpoint_source_integrity_broken"
                    )
                await _verified_blob_record(
                    session,
                    checkpoint_row.replay_payload_ref,
                )
            elif thread.active_checkpoint_sequence != 0:
                raise AgentContextError("active_checkpoint_invariant_broken")
            result = await session.execute(
                select(AgentContextEventRecord)
                .where(
                    AgentContextEventRecord.context_thread_id == thread.id,
                    AgentContextEventRecord.epoch == thread.epoch,
                    AgentContextEventRecord.sequence
                    > thread.active_checkpoint_sequence,
                )
                .order_by(AgentContextEventRecord.sequence)
            )
            tail_rows = list(result.scalars().all())
            await _verify_active_event_chain(
                session,
                thread,
                checkpoint_source=source_row,
                tail=tail_rows,
            )
            tail = [_to_event(row) for row in tail_rows]
            return AgentContextActiveState(
                ref=_to_ref(thread),
                checkpoint=(
                    _to_checkpoint(checkpoint_row)
                    if checkpoint_row is not None
                    else None
                ),
                tail=tail,
                active_token_count=max(0, thread.active_token_count),
            )

    async def load_thread_summary(
        self,
        ref: AgentContextRef,
    ) -> AgentContextThreadSummary:
        """Load one exact thread's metadata without a bounded list scan."""

        async with self._session() as session:
            thread = await self._require_thread(session, ref)
            return _to_thread_summary(thread)

    async def list_threads(
        self,
        *,
        workflow_id: str,
        context_node_id: str,
        generation: Optional[int] = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[AgentContextThreadSummary]:
        """List bounded thread metadata without loading journal payloads."""

        _validate_identity("workflow_id", workflow_id, 255)
        _validate_identity("context_node_id", context_node_id, 255)
        limit = max(1, min(int(limit), 1000))
        async with self._session() as session:
            statement = select(AgentContextThreadRecord).where(
                AgentContextThreadRecord.workflow_id == workflow_id,
                AgentContextThreadRecord.context_node_id == context_node_id,
            )
            if generation is not None:
                statement = statement.where(
                    AgentContextThreadRecord.generation == generation
                )
            if not include_archived:
                statement = statement.where(
                    AgentContextThreadRecord.status == "active"
                )
            result = await session.execute(
                statement.order_by(
                    AgentContextThreadRecord.generation.desc(),
                    AgentContextThreadRecord.updated_at.desc(),
                    AgentContextThreadRecord.id.desc(),
                ).limit(limit)
            )
            return [_to_thread_summary(row) for row in result.scalars().all()]

    async def iter_threads(
        self,
        *,
        workflow_id: str,
        context_node_id: str,
        generation: Optional[int] = None,
        include_archived: bool = False,
        page_size: int = 250,
    ) -> AsyncIterator[AgentContextThreadSummary]:
        """Keyset-scan thread metadata without a fixed total-result cap.

        Lifecycle operations mutate ``updated_at`` while consuming this
        iterator, so pagination is deliberately anchored to the immutable
        database row id instead of the presentation ordering used by
        :meth:`list_threads`.
        """

        _validate_identity("workflow_id", workflow_id, 255)
        _validate_identity("context_node_id", context_node_id, 255)
        page_size = max(1, min(int(page_size), 1000))
        after_id = 0
        while True:
            async with self._session() as session:
                statement = select(AgentContextThreadRecord).where(
                    AgentContextThreadRecord.workflow_id == workflow_id,
                    AgentContextThreadRecord.context_node_id
                    == context_node_id,
                    AgentContextThreadRecord.id > after_id,
                )
                if generation is not None:
                    statement = statement.where(
                        AgentContextThreadRecord.generation == generation
                    )
                if not include_archived:
                    statement = statement.where(
                        AgentContextThreadRecord.status == "active"
                    )
                result = await session.execute(
                    statement.order_by(
                        AgentContextThreadRecord.id.asc()
                    ).limit(page_size)
                )
                rows = list(result.scalars().all())
            if not rows:
                return
            for row in rows:
                yield _to_thread_summary(row)
            last_id = rows[-1].id
            if last_id is None:
                raise AgentContextError("context_thread_identity_missing")
            after_id = int(last_id)
            if len(rows) < page_size:
                return

    async def load_journal_page(
        self,
        ref: AgentContextRef,
        *,
        after_sequence: int = 0,
        limit: int = 50,
        epoch: Optional[int] = None,
    ) -> tuple[list[AgentContextEvent], Optional[int]]:
        """Load raw journal without mutating it; optionally narrow an epoch."""

        after_sequence = max(0, int(after_sequence))
        limit = max(1, min(int(limit), 200))
        async with self._session() as session:
            thread = await self._require_thread(session, ref)
            conditions = [
                AgentContextEventRecord.context_thread_id == thread.id,
                AgentContextEventRecord.sequence > after_sequence,
            ]
            if epoch is not None:
                conditions.append(AgentContextEventRecord.epoch == epoch)
            result = await session.execute(
                select(AgentContextEventRecord)
                .where(*conditions)
                .order_by(AgentContextEventRecord.sequence)
                .limit(limit + 1)
            )
            rows = list(result.scalars().all())
            has_more = len(rows) > limit
            page = rows[:limit]
            next_after = page[-1].sequence if has_more and page else None
            return [_to_event(row) for row in page], next_after

    async def list_checkpoints(
        self,
        ref: AgentContextRef,
        *,
        limit: int = 20,
    ) -> list[AgentContextCheckpoint]:
        """List current-epoch checkpoint metadata, newest first."""

        limit = max(1, min(int(limit), 100))
        async with self._session() as session:
            thread = await self._require_thread(session, ref)
            result = await session.execute(
                select(AgentContextCheckpointRecord)
                .where(
                    AgentContextCheckpointRecord.context_thread_id
                    == thread.id,
                    AgentContextCheckpointRecord.epoch == thread.epoch,
                )
                .order_by(
                    AgentContextCheckpointRecord.covers_through_sequence.desc(),
                    AgentContextCheckpointRecord.id.desc(),
                )
                .limit(limit)
            )
            return [
                _to_checkpoint(row) for row in result.scalars().all()
            ]

    async def append_transition(
        self,
        ref: AgentContextRef,
        *,
        event_type: str,
        operation_id: str,
        message_wire: Optional[dict[str, Any]] = None,
        payload_ref: Optional[str] = None,
        provider: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> AgentContextAppendResult:
        """Append one transition with ordering, hash chaining, and fencing."""

        _validate_identity("event_type", event_type, 100)
        _validate_identity("operation_id", operation_id, 512)
        if payload_ref is not None:
            _validate_identity("payload_ref", payload_ref, 512)
        if provider is not None:
            _validate_identity("provider", provider, 100)
        wire = _validate_message_wire(message_wire)

        for _ in range(_MAX_CAS_ATTEMPTS):
            async with self._session() as session:
                thread = await self._require_thread(
                    session,
                    ref,
                    permit_archived=True,
                    permit_stale_epoch=True,
                )
                existing = await _event_by_operation(
                    session,
                    thread_id=int(thread.id),
                    operation_id=operation_id,
                )
                if existing is not None:
                    _assert_idempotent_event_reuse(
                        existing,
                        event_type=event_type,
                        message_wire=wire,
                        payload_ref=payload_ref,
                        provider=provider,
                    )
                    return AgentContextAppendResult(
                        ref=_to_ref(thread),
                        event=_to_event(existing),
                        applied=False,
                    )
                self._assert_writable(thread, ref)
                if (
                    expected_revision is not None
                    and thread.revision != expected_revision
                ):
                    raise RevisionConflictError(
                        f"context_revision_conflict:"
                        f"{expected_revision}:{thread.revision}"
                    )

                sequence = thread.next_sequence
                previous_hash = thread.last_event_hash or ZERO_HASH
                payload_hash = _event_hash(
                    sequence=sequence,
                    epoch=thread.epoch,
                    event_type=event_type,
                    message_wire=wire,
                    payload_ref=payload_ref,
                    operation_id=operation_id,
                    provider=provider,
                    previous_hash=previous_hash,
                )
                now = datetime.now(timezone.utc)
                event = AgentContextEventRecord(
                    context_thread_id=int(thread.id),
                    epoch=thread.epoch,
                    sequence=sequence,
                    event_type=event_type,
                    message_wire=deepcopy(wire),
                    payload_ref=payload_ref,
                    operation_id=operation_id,
                    provider=provider,
                    previous_hash=previous_hash,
                    payload_hash=payload_hash,
                    created_at=now,
                )
                result = await session.execute(
                    update(AgentContextThreadRecord)
                    .where(
                        AgentContextThreadRecord.id == thread.id,
                        AgentContextThreadRecord.epoch == thread.epoch,
                        AgentContextThreadRecord.revision == thread.revision,
                        AgentContextThreadRecord.status == "active",
                    )
                    .values(
                        next_sequence=sequence + 1,
                        last_event_hash=payload_hash,
                        revision=thread.revision + 1,
                        provider=provider or thread.provider,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    if expected_revision is not None:
                        raise RevisionConflictError(
                            "context_revision_conflict"
                        )
                    continue
                try:
                    session.add(event)
                    await session.flush()
                    session.add(
                        RuntimeMutation(
                            mutation_id=operation_id,
                            resource_type="agent_context",
                            resource_id=str(thread.id),
                            operation="append_transition",
                            result={
                                "event_id": event.id,
                                "sequence": sequence,
                                "payload_hash": payload_hash,
                            },
                        )
                    )
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                updated = await self._load_ref(ref)
                # After commit and after the reload, so the notification can
                # never be observed ahead of the state it announces. The
                # idempotent-replay path above returns before reaching here:
                # a replay is not a state change.
                await notify_context_commit(
                    updated,
                    provider=provider or thread.provider,
                    active_token_count=int(thread.active_token_count or 0),
                    sequence=sequence,
                )
                return AgentContextAppendResult(
                    ref=updated,
                    event=_to_event(event),
                    applied=True,
                )
        raise RevisionConflictError("context_append_conflict")

    async def record_active_pressure(
        self,
        ref: AgentContextRef,
        *,
        operation_id: str,
        active_token_count: int,
    ) -> AgentContextRef:
        """Persist next-request context pressure without changing the journal.

        Pressure is operational metadata, not lifetime billing.  The update is
        fenced by the thread revision and idempotent through
        :class:`RuntimeMutation`; transcript or provider payloads never enter
        that ledger.
        """

        _validate_identity("operation_id", operation_id, 512)
        if active_token_count < 0:
            raise ValueError("active_token_count must be non-negative")

        for _ in range(_MAX_CAS_ATTEMPTS):
            async with self._session() as session:
                thread = await self._require_thread(
                    session,
                    ref,
                    permit_stale_epoch=True,
                )
                prior = await _runtime_mutation(
                    session,
                    thread_id=int(thread.id),
                    operation_id=operation_id,
                    operation="record_active_pressure",
                )
                if prior is not None:
                    return _to_ref(thread)
                self._assert_writable(thread, ref)
                now = datetime.now(timezone.utc)
                result = await session.execute(
                    update(AgentContextThreadRecord)
                    .where(
                        AgentContextThreadRecord.id == thread.id,
                        AgentContextThreadRecord.epoch == thread.epoch,
                        AgentContextThreadRecord.revision == thread.revision,
                        AgentContextThreadRecord.status == "active",
                    )
                    .values(
                        active_token_count=active_token_count,
                        revision=thread.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                session.add(
                    RuntimeMutation(
                        mutation_id=operation_id,
                        resource_type="agent_context",
                        resource_id=str(thread.id),
                        operation="record_active_pressure",
                        result={"active_token_count": active_token_count},
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                updated = await self._load_ref(ref)
                await notify_context_commit(
                    updated,
                    provider=thread.provider,
                    active_token_count=active_token_count,
                )
                return updated
        raise RevisionConflictError("context_pressure_update_conflict")

    async def prepare_compaction(
        self,
        ref: AgentContextRef,
        *,
        operation_id: str,
        provider: str,
        strategy: str,
        covers_through_sequence: int,
    ) -> AgentContextCompactionPlan:
        """Lease a stable committed prefix without changing active replay."""

        _validate_identity("operation_id", operation_id, 512)
        _validate_identity("provider", provider, 100)
        _validate_identity("strategy", strategy, 100)
        async with self._session() as session:
            thread = await self._require_thread(session, ref)
            self._assert_writable(thread, ref)
            existing_result = await session.execute(
                select(AgentContextCompactionAttemptRecord).where(
                    AgentContextCompactionAttemptRecord.context_thread_id
                    == thread.id,
                    AgentContextCompactionAttemptRecord.operation_id
                    == operation_id,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing is not None:
                return await _to_plan(session, thread, existing)

            if (
                covers_through_sequence
                <= thread.active_checkpoint_sequence
                or covers_through_sequence >= thread.next_sequence
            ):
                raise ValueError("invalid_compaction_coverage")
            source_result = await session.execute(
                select(AgentContextEventRecord).where(
                    AgentContextEventRecord.context_thread_id == thread.id,
                    AgentContextEventRecord.epoch == thread.epoch,
                    AgentContextEventRecord.sequence
                    == covers_through_sequence,
                )
            )
            source = source_result.scalar_one_or_none()
            if source is None:
                raise ValueError("compaction_source_event_not_found")
            attempt = AgentContextCompactionAttemptRecord(
                context_thread_id=int(thread.id),
                epoch=thread.epoch,
                provider=provider,
                strategy=strategy,
                covers_through_sequence=covers_through_sequence,
                base_checkpoint_id=thread.active_checkpoint_id,
                base_checkpoint_sequence=thread.active_checkpoint_sequence,
                source_revision=thread.revision,
                source_hash=source.payload_hash,
                operation_id=operation_id,
            )
            session.add(attempt)
            try:
                await session.flush()
                session.add(
                    RuntimeMutation(
                        mutation_id=operation_id,
                        resource_type="agent_context",
                        resource_id=str(thread.id),
                        operation="prepare_compaction",
                        result={
                            "attempt_id": attempt.id,
                            "source_hash": source.payload_hash,
                        },
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # The operation can only race with an identical preparation.
                retry = await session.execute(
                    select(AgentContextCompactionAttemptRecord).where(
                        AgentContextCompactionAttemptRecord.context_thread_id
                        == thread.id,
                        AgentContextCompactionAttemptRecord.operation_id
                        == operation_id,
                    )
                )
                attempt = retry.scalar_one_or_none()
                if attempt is None:
                    raise RevisionConflictError(
                        "compaction_prepare_conflict"
                    )
            return await _to_plan(session, thread, attempt)

    async def commit_checkpoint(
        self,
        ref: AgentContextRef,
        *,
        attempt_id: int,
        operation_id: str,
        replay_payload_ref: str,
        active_token_count: int,
    ) -> AgentContextCheckpoint:
        """CAS-activate a validated candidate, retaining concurrent tail."""

        _validate_identity("operation_id", operation_id, 512)
        _validate_identity("replay_payload_ref", replay_payload_ref, 512)
        if active_token_count < 0:
            raise ValueError("active_token_count must be non-negative")

        for _ in range(_MAX_CAS_ATTEMPTS):
            async with self._session() as session:
                thread = await self._require_thread(
                    session,
                    ref,
                    permit_stale_epoch=True,
                )
                existing_result = await session.execute(
                    select(AgentContextCheckpointRecord).where(
                        AgentContextCheckpointRecord.context_thread_id
                        == thread.id,
                        AgentContextCheckpointRecord.operation_id
                        == operation_id,
                    )
                )
                existing = existing_result.scalar_one_or_none()
                if existing is not None:
                    return _to_checkpoint(existing)
                self._assert_writable(thread, ref)
                attempt = await session.get(
                    AgentContextCompactionAttemptRecord,
                    attempt_id,
                )
                if (
                    attempt is None
                    or attempt.context_thread_id != thread.id
                ):
                    raise ContextNotFoundError(
                        "compaction_attempt_not_found"
                    )
                if attempt.status == "committed" and attempt.checkpoint_id:
                    checkpoint = await session.get(
                        AgentContextCheckpointRecord,
                        attempt.checkpoint_id,
                    )
                    if checkpoint is None:
                        raise AgentContextError(
                            "committed_checkpoint_missing"
                        )
                    return _to_checkpoint(checkpoint)
                if attempt.status != "prepared":
                    raise CompactionConflictError(
                        f"compaction_attempt_{attempt.status}"
                    )
                if attempt.epoch != thread.epoch:
                    raise StaleEpochError("compaction_epoch_fenced")
                if (
                    attempt.base_checkpoint_id != thread.active_checkpoint_id
                    or attempt.base_checkpoint_sequence
                    != thread.active_checkpoint_sequence
                ):
                    raise CompactionConflictError(
                        "active_checkpoint_changed"
                    )
                source_result = await session.execute(
                    select(AgentContextEventRecord).where(
                        AgentContextEventRecord.context_thread_id == thread.id,
                        AgentContextEventRecord.epoch == thread.epoch,
                        AgentContextEventRecord.sequence
                        == attempt.covers_through_sequence,
                    )
                )
                source = source_result.scalar_one_or_none()
                if (
                    source is None
                    or source.payload_hash != attempt.source_hash
                ):
                    raise CompactionConflictError(
                        "compaction_source_changed"
                    )

                checkpoint = AgentContextCheckpointRecord(
                    context_thread_id=int(thread.id),
                    epoch=thread.epoch,
                    provider=attempt.provider,
                    strategy=attempt.strategy,
                    covers_through_sequence=attempt.covers_through_sequence,
                    replay_payload_ref=replay_payload_ref,
                    active_token_count=active_token_count,
                    source_revision=attempt.source_revision,
                    source_hash=attempt.source_hash,
                    operation_id=operation_id,
                    status="active",
                )
                session.add(checkpoint)
                await session.flush()
                now = datetime.now(timezone.utc)
                result = await session.execute(
                    update(AgentContextThreadRecord)
                    .where(
                        AgentContextThreadRecord.id == thread.id,
                        AgentContextThreadRecord.epoch == thread.epoch,
                        AgentContextThreadRecord.revision == thread.revision,
                        AgentContextThreadRecord.active_checkpoint_id
                        == attempt.base_checkpoint_id,
                        AgentContextThreadRecord.active_checkpoint_sequence
                        == attempt.base_checkpoint_sequence,
                    )
                    .values(
                        active_checkpoint_id=checkpoint.id,
                        active_checkpoint_sequence=(
                            attempt.covers_through_sequence
                        ),
                        active_token_count=active_token_count,
                        revision=thread.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                if attempt.base_checkpoint_id is not None:
                    await session.execute(
                        update(AgentContextCheckpointRecord)
                        .where(
                            AgentContextCheckpointRecord.id
                            == attempt.base_checkpoint_id
                        )
                        .values(status="superseded")
                    )
                attempt.status = "committed"
                attempt.checkpoint_id = checkpoint.id
                attempt.completed_at = now
                session.add(
                    RuntimeMutation(
                        mutation_id=operation_id,
                        resource_type="agent_context",
                        resource_id=str(thread.id),
                        operation="commit_checkpoint",
                        result={
                            "checkpoint_id": checkpoint.id,
                            "source_hash": attempt.source_hash,
                        },
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                return _to_checkpoint(checkpoint)
        raise CompactionConflictError("checkpoint_activation_conflict")

    async def fail_compaction(
        self,
        *,
        attempt_id: int,
        error_code: str,
    ) -> None:
        """Record a failed candidate without touching active replay state."""

        _validate_identity("error_code", error_code, 255)
        async with self._session() as session:
            attempt = await session.get(
                AgentContextCompactionAttemptRecord,
                attempt_id,
            )
            if attempt is None:
                raise ContextNotFoundError("compaction_attempt_not_found")
            if attempt.status == "prepared":
                attempt.status = "failed"
                attempt.error_code = error_code
                attempt.completed_at = datetime.now(timezone.utc)
                await session.commit()

    async def start_epoch(
        self,
        ref: AgentContextRef,
        *,
        operation_id: str,
        provider: Optional[str] = None,
        handoff_payload_ref: Optional[str] = None,
    ) -> AgentContextRef:
        """Rotate an epoch and fence every writer holding the old ref."""

        _validate_identity("operation_id", operation_id, 512)
        if provider is not None:
            _validate_identity("provider", provider, 100)
        if handoff_payload_ref is not None:
            _validate_identity(
                "handoff_payload_ref",
                handoff_payload_ref,
                512,
            )
        request_hash = _mutation_request_hash(
            "start_epoch",
            {
                "provider": provider,
                "handoff_payload_ref": handoff_payload_ref,
            },
        )
        async with self._session() as session:
            thread = await self._require_thread(
                session,
                ref,
                permit_archived=True,
                permit_stale_epoch=True,
            )
            prior = await _runtime_mutation(
                session,
                thread_id=int(thread.id),
                operation_id=operation_id,
                operation="start_epoch",
            )
            if prior is not None:
                _assert_idempotent_mutation_reuse(
                    prior,
                    request_hash=request_hash,
                )
                return _to_ref(thread)
            self._assert_writable(thread, ref)
            now = datetime.now(timezone.utc)
            next_epoch = thread.epoch + 1
            next_revision = thread.revision + 1
            next_sequence = thread.next_sequence
            last_hash = thread.last_event_hash
            handoff_event: Optional[AgentContextEventRecord] = None
            await session.execute(
                update(AgentContextProviderBindingRecord)
                .where(
                    AgentContextProviderBindingRecord.context_thread_id
                    == thread.id,
                    AgentContextProviderBindingRecord.epoch == thread.epoch,
                    AgentContextProviderBindingRecord.status == "active",
                )
                .values(status="archived")
            )
            await session.execute(
                update(AgentContextCheckpointRecord)
                .where(
                    AgentContextCheckpointRecord.context_thread_id
                    == thread.id,
                    AgentContextCheckpointRecord.epoch == thread.epoch,
                    AgentContextCheckpointRecord.status == "active",
                )
                .values(status="archived")
            )
            await session.execute(
                update(AgentContextCompactionAttemptRecord)
                .where(
                    AgentContextCompactionAttemptRecord.context_thread_id
                    == thread.id,
                    AgentContextCompactionAttemptRecord.epoch == thread.epoch,
                    AgentContextCompactionAttemptRecord.status == "prepared",
                )
                .values(
                    status="stale",
                    error_code="epoch_rotated",
                    completed_at=now,
                )
            )
            if handoff_payload_ref is not None:
                handoff_operation = f"{operation_id}:handoff"
                handoff_hash = _event_hash(
                    sequence=next_sequence,
                    epoch=next_epoch,
                    event_type="provider_handoff",
                    message_wire=None,
                    payload_ref=handoff_payload_ref,
                    operation_id=handoff_operation,
                    provider=provider,
                    previous_hash=last_hash,
                )
                handoff_event = AgentContextEventRecord(
                    context_thread_id=int(thread.id),
                    epoch=next_epoch,
                    sequence=next_sequence,
                    event_type="provider_handoff",
                    payload_ref=handoff_payload_ref,
                    operation_id=handoff_operation,
                    provider=provider,
                    previous_hash=last_hash,
                    payload_hash=handoff_hash,
                )
                next_sequence += 1
                next_revision += 1
                last_hash = handoff_hash
            result = await session.execute(
                update(AgentContextThreadRecord)
                .where(
                    AgentContextThreadRecord.id == thread.id,
                    AgentContextThreadRecord.epoch == thread.epoch,
                    AgentContextThreadRecord.revision == thread.revision,
                    AgentContextThreadRecord.status == "active",
                )
                .values(
                    epoch=next_epoch,
                    revision=next_revision,
                    next_sequence=next_sequence,
                    last_event_hash=last_hash,
                    active_checkpoint_id=None,
                    active_checkpoint_sequence=0,
                    active_token_count=0,
                    provider=provider,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise RevisionConflictError("epoch_rotation_conflict")
            if handoff_event is not None:
                session.add(handoff_event)
            session.add(
                RuntimeMutation(
                    mutation_id=operation_id,
                    resource_type="agent_context",
                    resource_id=str(thread.id),
                    operation="start_epoch",
                    result={
                        "epoch": next_epoch,
                        "last_event_hash": last_hash,
                        "request_hash": request_hash,
                    },
                )
            )
            await session.commit()
        return await self._load_ref(ref)

    async def fork_provider(
        self,
        ref: AgentContextRef,
        *,
        provider: str,
        operation_id: str,
        portable_handoff_ref: str,
    ) -> AgentContextRef:
        """Start a provider epoch with a portable handoff boundary."""

        return await self.start_epoch(
            ref,
            operation_id=operation_id,
            provider=provider,
            handoff_payload_ref=portable_handoff_ref,
        )

    async def bind_provider(
        self,
        ref: AgentContextRef,
        *,
        provider: str,
        binding_type: str,
        binding: Any,
        operation_id: str,
        fidelity: ContextFidelity = "provider_bound",
    ) -> AgentContextProviderBinding:
        """Persist an opaque binding in blob storage and expose only its ref."""

        _validate_identity("provider", provider, 100)
        _validate_identity("binding_type", binding_type, 100)
        _validate_identity("operation_id", operation_id, 512)
        binding_ref = await self.put_blob(binding)
        binding_hash = binding_ref.removeprefix("sha256:")
        request_hash = _mutation_request_hash(
            "bind_provider",
            {
                "provider": provider,
                "binding_type": binding_type,
                "binding_hash": binding_hash,
                "fidelity": fidelity,
            },
        )
        async with self._session() as session:
            thread = await self._require_thread(
                session,
                ref,
                permit_stale_epoch=True,
            )
            result = await session.execute(
                select(AgentContextProviderBindingRecord).where(
                    AgentContextProviderBindingRecord.context_thread_id
                    == thread.id,
                    AgentContextProviderBindingRecord.operation_id
                    == operation_id,
                )
            )
            record = result.scalar_one_or_none()
            if record is not None:
                _assert_idempotent_binding_reuse(
                    record,
                    provider=provider,
                    binding_type=binding_type,
                    binding_hash=binding_hash,
                    fidelity=fidelity,
                )
                prior = await _runtime_mutation(
                    session,
                    thread_id=int(thread.id),
                    operation_id=operation_id,
                    operation="bind_provider",
                )
                if prior is None:
                    raise AgentContextError(
                        "context_binding_idempotency_invariant_broken"
                    )
                _assert_idempotent_mutation_reuse(
                    prior,
                    request_hash=request_hash,
                )
                return _to_binding(record)
            self._assert_writable(thread, ref)
            if record is None:
                record = AgentContextProviderBindingRecord(
                    context_thread_id=int(thread.id),
                    epoch=thread.epoch,
                    provider=provider,
                    binding_type=binding_type,
                    binding_ref=binding_ref,
                    binding_hash=binding_hash,
                    fidelity=fidelity,
                    operation_id=operation_id,
                )
                session.add(record)
                await session.flush()
                session.add(
                    RuntimeMutation(
                        mutation_id=operation_id,
                        resource_type="agent_context",
                        resource_id=str(thread.id),
                        operation="bind_provider",
                        result={
                            "binding_id": record.id,
                            "binding_hash": binding_hash,
                            "request_hash": request_hash,
                        },
                    )
                )
                await session.commit()
            return _to_binding(record)

    async def load_provider_bindings(
        self,
        ref: AgentContextRef,
        *,
        provider: Optional[str] = None,
    ) -> list[AgentContextProviderBinding]:
        async with self._session() as session:
            thread = await self._require_thread(session, ref)
            statement = select(AgentContextProviderBindingRecord).where(
                AgentContextProviderBindingRecord.context_thread_id
                == thread.id,
                AgentContextProviderBindingRecord.epoch == thread.epoch,
                AgentContextProviderBindingRecord.status == "active",
            )
            if provider is not None:
                statement = statement.where(
                    AgentContextProviderBindingRecord.provider == provider
                )
            result = await session.execute(
                statement.order_by(AgentContextProviderBindingRecord.id)
            )
            return [_to_binding(row) for row in result.scalars().all()]

    async def put_blob(
        self,
        payload: Any,
        *,
        media_type: str = "application/json",
    ) -> str:
        """Store exact JSON or bytes by SHA-256 and return an opaque ref."""

        _validate_identity("media_type", media_type, 100)
        if isinstance(payload, (bytes, bytearray, memoryview)):
            binary = bytes(payload)
            encoded = binary
            json_payload = None
        else:
            encoded = _canonical_json(payload)
            binary = None
            json_payload = deepcopy(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        async with self._session() as session:
            existing = await session.get(AgentContextBlobRecord, digest)
            if existing is None:
                session.add(
                    AgentContextBlobRecord(
                        payload_hash=digest,
                        media_type=media_type,
                        json_payload=json_payload,
                        binary_payload=binary,
                        byte_size=len(encoded),
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
        return f"sha256:{digest}"

    async def get_blob(self, payload_ref: str) -> Any:
        async with self._session() as session:
            row = await _verified_blob_record(session, payload_ref)
            if row.binary_payload is not None:
                return bytes(row.binary_payload)
            return deepcopy(row.json_payload)

    async def archive(
        self,
        ref: AgentContextRef,
        *,
        operation_id: str,
    ) -> AgentContextRef:
        """Archive one thread, rotating its epoch to fence late writes."""

        _validate_identity("operation_id", operation_id, 512)
        async with self._session() as session:
            thread = await self._require_thread(
                session,
                ref,
                permit_archived=True,
                permit_stale_epoch=True,
            )
            prior = await _runtime_mutation(
                session,
                thread_id=int(thread.id),
                operation_id=operation_id,
                operation="archive",
            )
            if prior is not None:
                return _to_ref(thread)
            if thread.status == "archived":
                return _to_ref(thread)
            self._assert_writable(thread, ref)
            now = datetime.now(timezone.utc)
            result = await session.execute(
                update(AgentContextThreadRecord)
                .where(
                    AgentContextThreadRecord.id == thread.id,
                    AgentContextThreadRecord.epoch == thread.epoch,
                    AgentContextThreadRecord.revision == thread.revision,
                )
                .values(
                    status="archived",
                    epoch=thread.epoch + 1,
                    revision=thread.revision + 1,
                    active_checkpoint_id=None,
                    active_checkpoint_sequence=0,
                    active_token_count=0,
                    provider=None,
                    archived_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise RevisionConflictError("context_archive_conflict")
            await session.execute(
                update(AgentContextProviderBindingRecord)
                .where(
                    AgentContextProviderBindingRecord.context_thread_id
                    == thread.id,
                    AgentContextProviderBindingRecord.status == "active",
                )
                .values(status="archived")
            )
            await session.execute(
                update(AgentContextCheckpointRecord)
                .where(
                    AgentContextCheckpointRecord.context_thread_id
                    == thread.id,
                    AgentContextCheckpointRecord.status == "active",
                )
                .values(status="archived")
            )
            await session.execute(
                update(AgentContextCompactionAttemptRecord)
                .where(
                    AgentContextCompactionAttemptRecord.context_thread_id
                    == thread.id,
                    AgentContextCompactionAttemptRecord.status == "prepared",
                )
                .values(
                    status="stale",
                    error_code="context_archived",
                    completed_at=now,
                )
            )
            session.add(
                RuntimeMutation(
                    mutation_id=operation_id,
                    resource_type="agent_context",
                    resource_id=str(thread.id),
                    operation="archive",
                    result={"archived_epoch": thread.epoch},
                )
            )
            await session.commit()
        return await self._load_ref(ref)

    async def archive_context(
        self,
        *,
        workflow_id: str,
        context_node_id: str,
        generation: Optional[int] = None,
        operation_id: str,
    ) -> list[AgentContextRef]:
        """Archive Context threads, across every generation by default."""

        _validate_identity("operation_id", operation_id, 500)
        async with self._session() as session:
            statement = select(AgentContextThreadRecord).where(
                AgentContextThreadRecord.workflow_id == workflow_id,
                AgentContextThreadRecord.context_node_id == context_node_id,
            )
            if generation is not None:
                statement = statement.where(
                    AgentContextThreadRecord.generation == generation
                )
            result = await session.execute(
                statement.order_by(
                    AgentContextThreadRecord.generation,
                    AgentContextThreadRecord.thread_id,
                    AgentContextThreadRecord.id,
                )
            )
            refs = [_to_ref(row) for row in result.scalars().all()]
        archived: list[AgentContextRef] = []
        for index, ref in enumerate(refs):
            archived.append(
                await self.archive(
                    ref,
                    operation_id=f"{operation_id}:{index}",
                )
            )
        return archived

    async def purge(
        self,
        *,
        workflow_id: str,
        context_node_id: str,
        generation: Optional[int] = None,
    ) -> int:
        """Permanently delete a Context journal.

        Hash-addressed blobs are intentionally retained here.  A global
        mark-and-sweep performed immediately after this transaction can race
        with another transaction that has stored a blob but has not yet
        committed its reference.  Blob collection belongs in a separately
        coordinated maintenance job with an age/grace-period fence.
        """

        async with self._session() as session:
            statement = select(AgentContextThreadRecord).where(
                AgentContextThreadRecord.workflow_id == workflow_id,
                AgentContextThreadRecord.context_node_id == context_node_id,
            )
            if generation is not None:
                statement = statement.where(
                    AgentContextThreadRecord.generation == generation
                )
            result = await session.execute(statement)
            thread_ids = [
                int(row.id)
                for row in result.scalars().all()
                if row.id is not None
            ]
            if not thread_ids:
                return 0
            for model in (
                AgentContextEventRecord,
                AgentContextCheckpointRecord,
                AgentContextProviderBindingRecord,
                AgentContextCompactionAttemptRecord,
            ):
                await session.execute(
                    delete(model).where(
                        model.context_thread_id.in_(thread_ids)
                    )
                )
            await session.execute(
                delete(RuntimeMutation).where(
                    RuntimeMutation.resource_type == "agent_context",
                    RuntimeMutation.resource_id.in_(
                        [str(value) for value in thread_ids]
                    ),
                )
            )
            await session.execute(
                delete(AgentContextThreadRecord).where(
                    AgentContextThreadRecord.id.in_(thread_ids)
                )
            )
            await session.commit()
        return len(thread_ids)

    async def _require_thread(
        self,
        session: AsyncSession,
        ref: AgentContextRef,
        *,
        permit_archived: bool = False,
        permit_stale_epoch: bool = False,
    ) -> AgentContextThreadRecord:
        thread = await _select_thread(
            session,
            workflow_id=ref.workflow_id,
            context_node_id=ref.context_node_id,
            generation=ref.generation,
            thread_id=ref.thread_id,
        )
        if thread is None:
            raise ContextNotFoundError("agent_context_thread_not_found")
        if thread.epoch != ref.epoch and not permit_stale_epoch:
            raise StaleEpochError(
                f"context_epoch_fenced:{ref.epoch}:{thread.epoch}"
            )
        if thread.status == "archived" and not permit_archived:
            raise ContextArchivedError("agent_context_thread_archived")
        return thread

    def _assert_writable(
        self,
        thread: AgentContextThreadRecord,
        ref: AgentContextRef,
    ) -> None:
        if thread.epoch != ref.epoch:
            raise StaleEpochError("context_epoch_fenced")
        if thread.status != "active":
            raise ContextArchivedError("agent_context_thread_archived")

    async def _load_ref(self, ref: AgentContextRef) -> AgentContextRef:
        async with self._session() as session:
            thread = await _select_thread(
                session,
                workflow_id=ref.workflow_id,
                context_node_id=ref.context_node_id,
                generation=ref.generation,
                thread_id=ref.thread_id,
            )
            if thread is None:
                raise ContextNotFoundError(
                    "agent_context_thread_not_found"
                )
            return _to_ref(thread)


def _resolve_thread_identity(
    *,
    session_id: Optional[str],
    delegated_task_id: Optional[str],
    execution_id: Optional[str],
) -> tuple[ContextThreadKind, str]:
    if session_id is not None and session_id.strip():
        return "session", session_id.strip()
    if delegated_task_id is not None and delegated_task_id.strip():
        return "task", delegated_task_id.strip()
    if execution_id is not None and execution_id.strip():
        return "execution", execution_id.strip()
    raise ValueError(
        "execution_id is required when no explicit session or delegated "
        "task is present"
    )


def _thread_id(kind: ContextThreadKind, source_id: str) -> str:
    _validate_identity(f"{kind}_id", source_id, 4096)
    raw = f"{kind}:{source_id}"
    if len(raw) <= 512:
        return raw
    return f"{kind}:sha256:{hashlib.sha256(source_id.encode()).hexdigest()}"


def _validate_identity(name: str, value: str, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")


async def _select_thread(
    session: AsyncSession,
    *,
    workflow_id: str,
    context_node_id: str,
    generation: int,
    thread_id: str,
) -> Optional[AgentContextThreadRecord]:
    result = await session.execute(
        select(AgentContextThreadRecord).where(
            AgentContextThreadRecord.workflow_id == workflow_id,
            AgentContextThreadRecord.context_node_id == context_node_id,
            AgentContextThreadRecord.generation == generation,
            AgentContextThreadRecord.thread_id == thread_id,
        )
    )
    return result.scalar_one_or_none()


async def _event_by_operation(
    session: AsyncSession,
    *,
    thread_id: int,
    operation_id: str,
) -> Optional[AgentContextEventRecord]:
    result = await session.execute(
        select(AgentContextEventRecord).where(
            AgentContextEventRecord.context_thread_id == thread_id,
            AgentContextEventRecord.operation_id == operation_id,
        )
    )
    return result.scalar_one_or_none()


def _assert_idempotent_event_reuse(
    existing: AgentContextEventRecord,
    *,
    event_type: str,
    message_wire: Optional[dict[str, Any]],
    payload_ref: Optional[str],
    provider: Optional[str],
) -> None:
    """Accept an operation retry only when it is the exact same transition."""

    stored_hash = _event_record_hash(existing)
    retry_hash = _event_hash(
        sequence=existing.sequence,
        epoch=existing.epoch,
        event_type=event_type,
        message_wire=message_wire,
        payload_ref=payload_ref,
        operation_id=existing.operation_id,
        provider=provider,
        previous_hash=existing.previous_hash,
    )
    if (
        existing.event_type != event_type
        or existing.message_wire != message_wire
        or existing.payload_ref != payload_ref
        or existing.provider != provider
        or stored_hash != existing.payload_hash
        or retry_hash != existing.payload_hash
    ):
        raise AgentContextError("context_operation_id_reuse_mismatch")


async def _runtime_mutation(
    session: AsyncSession,
    *,
    thread_id: int,
    operation_id: str,
    operation: str,
) -> Optional[RuntimeMutation]:
    result = await session.execute(
        select(RuntimeMutation).where(
            RuntimeMutation.resource_type == "agent_context",
            RuntimeMutation.resource_id == str(thread_id),
            RuntimeMutation.mutation_id == operation_id,
            RuntimeMutation.operation == operation,
        )
    )
    return result.scalar_one_or_none()


def _mutation_request_hash(
    operation: str,
    inputs: dict[str, Any],
) -> str:
    """Hash the exact server-controlled inputs for one idempotent mutation."""

    return hashlib.sha256(
        _canonical_json(
            {
                "operation": operation,
                "inputs": inputs,
            }
        )
    ).hexdigest()


def _assert_idempotent_mutation_reuse(
    mutation: RuntimeMutation,
    *,
    request_hash: str,
) -> None:
    result = mutation.result if isinstance(mutation.result, dict) else {}
    if result.get("request_hash") != request_hash:
        raise AgentContextError("context_operation_id_reuse_mismatch")


def _assert_idempotent_binding_reuse(
    record: AgentContextProviderBindingRecord,
    *,
    provider: str,
    binding_type: str,
    binding_hash: str,
    fidelity: ContextFidelity,
) -> None:
    if (
        record.provider != provider
        or record.binding_type != binding_type
        or record.binding_hash != binding_hash
        or record.binding_ref != f"sha256:{binding_hash}"
        or record.fidelity != fidelity
    ):
        raise AgentContextError("context_operation_id_reuse_mismatch")


def _validate_message_wire(
    value: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("message_wire must be a JSON object")
    # Round-tripping through the codec enforces JSON-safe provider state,
    # preserves ordered blocks/tool calls, and rejects shapes with no role.
    return dict(message_to_wire(message_from_wire(value)))


def _event_hash(
    *,
    sequence: int,
    epoch: int,
    event_type: str,
    message_wire: Optional[dict[str, Any]],
    payload_ref: Optional[str],
    operation_id: str,
    provider: Optional[str],
    previous_hash: str,
) -> str:
    body = {
        "sequence": sequence,
        "epoch": epoch,
        "event_type": event_type,
        "message_wire": message_wire,
        "payload_ref": payload_ref,
        "operation_id": operation_id,
        "provider": provider,
        "previous_hash": previous_hash,
    }
    return hashlib.sha256(_canonical_json(body)).hexdigest()


def _event_record_hash(row: AgentContextEventRecord) -> str:
    return _event_hash(
        sequence=row.sequence,
        epoch=row.epoch,
        event_type=row.event_type,
        message_wire=row.message_wire,
        payload_ref=row.payload_ref,
        operation_id=row.operation_id,
        provider=row.provider,
        previous_hash=row.previous_hash,
    )


async def _verify_active_event_chain(
    session: AsyncSession,
    thread: AgentContextThreadRecord,
    *,
    checkpoint_source: Optional[AgentContextEventRecord],
    tail: list[AgentContextEventRecord],
) -> None:
    """Validate the bounded replay chain and the thread's journal head."""

    anchor = checkpoint_source or (tail[0] if tail else None)
    predecessor: Optional[AgentContextEventRecord] = None
    if anchor is not None and anchor.sequence > 1:
        predecessor_result = await session.execute(
            select(AgentContextEventRecord).where(
                AgentContextEventRecord.context_thread_id == thread.id,
                AgentContextEventRecord.sequence == anchor.sequence - 1,
            )
        )
        predecessor = predecessor_result.scalar_one_or_none()
        if predecessor is None:
            raise AgentContextError("context_event_sequence_integrity_broken")

    if checkpoint_source is not None:
        _verify_event_record(
            checkpoint_source,
            expected_sequence=checkpoint_source.sequence,
            expected_previous_hash=(
                predecessor.payload_hash if predecessor is not None else ZERO_HASH
            ),
        )
        expected_sequence = checkpoint_source.sequence + 1
        previous_hash = checkpoint_source.payload_hash
    elif tail:
        expected_sequence = tail[0].sequence
        previous_hash = (
            predecessor.payload_hash if predecessor is not None else ZERO_HASH
        )
        if predecessor is None and expected_sequence != 1:
            raise AgentContextError("context_event_sequence_integrity_broken")
    else:
        expected_sequence = thread.next_sequence
        previous_hash = thread.last_event_hash

    for row in tail:
        _verify_event_record(
            row,
            expected_sequence=expected_sequence,
            expected_previous_hash=previous_hash,
        )
        expected_sequence += 1
        previous_hash = row.payload_hash

    latest_result = await session.execute(
        select(AgentContextEventRecord)
        .where(AgentContextEventRecord.context_thread_id == thread.id)
        .order_by(AgentContextEventRecord.sequence.desc())
        .limit(1)
    )
    latest = latest_result.scalar_one_or_none()
    if latest is None:
        if thread.next_sequence != 1 or thread.last_event_hash != ZERO_HASH:
            raise AgentContextError("context_journal_head_integrity_broken")
        return
    if (
        thread.next_sequence != latest.sequence + 1
        or thread.last_event_hash != latest.payload_hash
    ):
        raise AgentContextError("context_journal_head_integrity_broken")


def _verify_event_record(
    row: AgentContextEventRecord,
    *,
    expected_sequence: int,
    expected_previous_hash: str,
) -> None:
    if row.sequence != expected_sequence:
        raise AgentContextError("context_event_sequence_integrity_broken")
    if row.previous_hash != expected_previous_hash:
        raise AgentContextError("context_event_chain_integrity_broken")
    if _event_record_hash(row) != row.payload_hash:
        raise AgentContextError("context_event_hash_integrity_broken")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError("Context payload must be exact JSON data") from exc


def _parse_blob_ref(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("invalid_context_blob_ref")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("invalid_context_blob_ref")
    return digest


async def _verified_blob_record(
    session: AsyncSession,
    payload_ref: str,
) -> AgentContextBlobRecord:
    digest = _parse_blob_ref(payload_ref)
    row = await session.get(AgentContextBlobRecord, digest)
    if row is None:
        raise ContextNotFoundError("context_blob_not_found")
    if row.binary_payload is not None:
        if row.json_payload is not None:
            raise AgentContextError("context_blob_integrity_broken")
        encoded = bytes(row.binary_payload)
    else:
        encoded = _canonical_json(row.json_payload)
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if (
        row.payload_hash != digest
        or actual_digest != digest
        or row.byte_size != len(encoded)
    ):
        raise AgentContextError("context_blob_integrity_broken")
    return row


def _to_ref(row: AgentContextThreadRecord) -> AgentContextRef:
    return AgentContextRef(
        workflow_id=row.workflow_id,
        context_node_id=row.context_node_id,
        generation=row.generation,
        thread_id=row.thread_id,
        epoch=row.epoch,
        revision=row.revision,
    )


def _to_thread_summary(
    row: AgentContextThreadRecord,
) -> AgentContextThreadSummary:
    return AgentContextThreadSummary(
        ref=_to_ref(row),
        thread_kind=row.thread_kind,  # type: ignore[arg-type]
        provider=row.provider,
        status=row.status,
        active_checkpoint_sequence=max(
            0,
            row.active_checkpoint_sequence,
        ),
        active_token_count=max(0, row.active_token_count),
        updated_at=row.updated_at,
    )


def _to_event(row: AgentContextEventRecord) -> AgentContextEvent:
    return AgentContextEvent(
        sequence=row.sequence,
        event_type=row.event_type,
        message_wire=deepcopy(row.message_wire),
        payload_ref=row.payload_ref,
        operation_id=row.operation_id,
        provider=row.provider,
        previous_hash=row.previous_hash,
        payload_hash=row.payload_hash,
    )


def _to_checkpoint(
    row: AgentContextCheckpointRecord,
) -> AgentContextCheckpoint:
    return AgentContextCheckpoint(
        provider=row.provider,
        strategy=row.strategy,
        covers_through_sequence=row.covers_through_sequence,
        replay_payload_ref=row.replay_payload_ref,
        active_token_count=row.active_token_count,
        source_revision=row.source_revision,
        source_hash=row.source_hash,
    )


def _to_binding(
    row: AgentContextProviderBindingRecord,
) -> AgentContextProviderBinding:
    return AgentContextProviderBinding(
        provider=row.provider,
        binding_type=row.binding_type,
        binding_ref=row.binding_ref,
        epoch=row.epoch,
        fidelity=row.fidelity,  # type: ignore[arg-type]
    )


async def _to_plan(
    session: AsyncSession,
    thread: AgentContextThreadRecord,
    attempt: AgentContextCompactionAttemptRecord,
) -> AgentContextCompactionPlan:
    checkpoint: Optional[AgentContextCheckpoint] = None
    if attempt.base_checkpoint_id is not None:
        checkpoint_row = await session.get(
            AgentContextCheckpointRecord,
            attempt.base_checkpoint_id,
        )
        if checkpoint_row is None:
            raise AgentContextError("base_checkpoint_missing")
        checkpoint = _to_checkpoint(checkpoint_row)
    result = await session.execute(
        select(AgentContextEventRecord)
        .where(
            AgentContextEventRecord.context_thread_id == thread.id,
            AgentContextEventRecord.epoch == attempt.epoch,
            AgentContextEventRecord.sequence
            > attempt.base_checkpoint_sequence,
            AgentContextEventRecord.sequence
            <= attempt.covers_through_sequence,
        )
        .order_by(AgentContextEventRecord.sequence)
    )
    prefix = [_to_event(row) for row in result.scalars().all()]
    if not prefix or prefix[-1].payload_hash != attempt.source_hash:
        raise CompactionConflictError("compaction_prefix_changed")
    return AgentContextCompactionPlan(
        attempt_id=int(attempt.id),
        ref=_to_ref(thread),
        provider=attempt.provider,
        strategy=attempt.strategy,
        base_checkpoint=checkpoint,
        committed_prefix=prefix,
        covers_through_sequence=attempt.covers_through_sequence,
        source_revision=attempt.source_revision,
        source_hash=attempt.source_hash,
    )


__all__ = [
    "AgentContextError",
    "AgentContextStore",
    "CompactionConflictError",
    "ContextArchivedError",
    "ContextNotFoundError",
    "RevisionConflictError",
    "StaleEpochError",
]
