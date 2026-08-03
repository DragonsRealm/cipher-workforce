"""Backend-only Context V2 bridge for specialized agent runtimes.

Canvas Context nodes carry policy and topology only.  This module resolves the
durable thread, writes exact observable provider events to the append-only
journal, and owns opaque provider continuation bindings.  Specialized node
plugins pass the descriptor returned by ``collect_agent_connections``; they do
not read or mutate Context node parameters themselves.

The bridge is intentionally absent for legacy ``input-memory`` descriptors so
already-recorded V1 generations retain their original Simple Memory behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal, Optional

from models.agent_context import (
    AgentContextActiveState,
    AgentContextRef,
    ContextFidelity,
)
from services.agent_context import (
    AgentContextStore,
    AgentContextTransitionWriter,
    OpaqueCheckpointError,
    import_generation_zero_handoff,
    reconstruct_message_wire_v2,
)
from services.llm.protocol import Message, message_from_wire, message_to_wire


ProviderFidelity = Literal[
    "provider_replayable",
    "provider_bound",
    "observable_only",
]


def is_context(value: Any) -> bool:
    """Return whether an edge-walker descriptor is a Context V2 reference."""

    return isinstance(value, dict) and value.get("kind") == "context"


def _jsonable(value: Any) -> Any:
    """Convert provider SDK/dataclass values into exact JSON-safe payloads."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value):
        return _jsonable(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json", exclude_none=False))
        except TypeError:
            return _jsonable(model_dump())
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _portable_wire(value: dict[str, Any]) -> dict[str, Any]:
    """Validate a portable wire and remove prior-provider opaque state."""

    wire = dict(message_to_wire(message_from_wire(value)))
    wire["provider_state"] = {}
    return wire


async def _observable_handoff_messages(
    store: AgentContextStore,
    state: AgentContextActiveState,
    *,
    include_message_wires: bool,
) -> list[dict[str, Any]]:
    """Render exact observable boundaries when native replay is unavailable."""

    messages: list[dict[str, Any]] = []
    for event in state.tail:
        if event.event_type in {
            "provider_handoff",
            "request.snapshot",
        }:
            continue
        if event.message_wire_v2 is not None:
            if include_message_wires:
                messages.append(_portable_wire(event.message_wire_v2))
            continue

        payload = (
            await store.get_blob(event.payload_ref)
            if event.payload_ref
            else None
        )
        envelope = {
            "sequence": event.sequence,
            "event_type": event.event_type,
            "provider": event.provider,
            "operation_id": event.operation_id,
            "payload": _jsonable(payload),
        }
        role = (
            "user"
            if event.event_type in {"provider.request", "request.snapshot"}
            else "assistant"
        )
        messages.append(
            dict(
                message_to_wire(
                    Message(
                        role=role,
                        content=json.dumps(
                            envelope,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                )
            )
        )
    return messages


async def _build_portable_handoff(
    store: AgentContextStore,
    state: AgentContextActiveState,
    *,
    from_provider: str,
    to_provider: str,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Persist a provider-neutral replay payload for an epoch fork."""

    fidelity = "portable"
    reconstructed: list[dict[str, Any]] = []
    try:
        _, reconstructed = await reconstruct_message_wire_v2(
            store,
            state.ref,
        )
        messages = [_portable_wire(item) for item in reconstructed]
    except OpaqueCheckpointError:
        messages = []
    observable_messages = await _observable_handoff_messages(
        store,
        state,
        include_message_wires=not reconstructed,
    )
    if observable_messages:
        messages.extend(observable_messages)
    if not reconstructed:
        fidelity = "observable_only"

    payload_ref = await store.put_blob(
        {
            "format": "portable_provider_handoff_v2",
            "fidelity": fidelity,
            "from_provider": from_provider,
            "to_provider": to_provider,
            "source_epoch": state.ref.epoch,
            "messages": messages,
            "source_checkpoint": (
                {
                    "provider": state.checkpoint.provider,
                    "strategy": state.checkpoint.strategy,
                    "covers_through_sequence": (
                        state.checkpoint.covers_through_sequence
                    ),
                    "source_hash": state.checkpoint.source_hash,
                }
                if state.checkpoint is not None
                else None
            ),
        }
    )
    return payload_ref, tuple(messages)


async def _pending_handoff_messages(
    store: AgentContextStore,
    ref: AgentContextRef,
) -> tuple[AgentContextRef, tuple[dict[str, Any], ...]]:
    """Recover an unconsumed handoff after a crash between fork and request."""

    state = await store.load_active(ref)
    handoff = next(
        (
            event
            for event in reversed(state.tail)
            if event.event_type == "provider_handoff"
            and event.payload_ref
        ),
        None,
    )
    if handoff is None:
        return state.ref, ()
    if any(
        event.sequence > handoff.sequence
        and event.event_type
        in {
            "provider.result",
            "provider.response",
            "provider.final",
        }
        for event in state.tail
    ):
        return state.ref, ()
    payload = await store.get_blob(handoff.payload_ref)
    candidates = (
        payload.get("messages") if isinstance(payload, dict) else None
    )
    if not isinstance(candidates, list):
        raise ValueError("provider handoff is missing portable messages")
    messages: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError(
                "provider handoff contains a non-message item"
            )
        messages.append(_portable_wire(candidate))
    return state.ref, tuple(messages)


@dataclass
class SpecializedAgentContextBridge:
    """One epoch-bound Context writer for a specialized provider.

    ``fidelity`` and ``resumable`` are attached to every opaque event.  That
    makes the fidelity boundary explicit in the durable record rather than an
    inference made later by the UI.
    """

    store: AgentContextStore
    writer: AgentContextTransitionWriter
    provider: str
    operation_prefix: str
    fidelity: ProviderFidelity
    resumable: bool
    portable_handoff_messages: tuple[dict[str, Any], ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        self._sequence = 0
        self._sequence_lock = asyncio.Lock()

    @property
    def ref(self) -> AgentContextRef:
        return self.writer.ref

    @property
    def pool_key(self) -> tuple[str, str, int]:
        """Claude pool identity; generation is already isolated by node id."""

        return (
            self.ref.context_node_id,
            self.ref.thread_id,
            self.ref.epoch,
        )

    @classmethod
    async def resolve(
        cls,
        database: Any,
        descriptor: dict[str, Any],
        *,
        provider: str,
        fidelity: ProviderFidelity,
        resumable: bool,
        operation_prefix: Optional[str] = None,
    ) -> "SpecializedAgentContextBridge":
        if not is_context(descriptor):
            raise ValueError("specialized Context bridge requires context_v2")
        workflow_id = str(descriptor.get("workflow_id") or "")
        context_node_id = str(
            descriptor.get("context_node_id")
            or descriptor.get("node_id")
            or ""
        )
        execution_id = descriptor.get("execution_id")
        delegated_task_id = descriptor.get("delegated_task_id")
        session_id = descriptor.get("session_id")
        generation = int(descriptor.get("generation") or 0)
        if generation <= 0:
            raise ValueError(
                "specialized Context bridge requires an admitted generation"
            )
        store = AgentContextStore(database)
        ref = await store.resolve_thread(
            workflow_id=workflow_id,
            context_node_id=context_node_id,
            generation=generation,
            session_id=str(session_id) if session_id else None,
            delegated_task_id=(
                str(delegated_task_id) if delegated_task_id else None
            ),
            execution_id=str(execution_id) if execution_id else None,
        )
        ref = await import_generation_zero_handoff(store, ref)
        # A provider change is an epoch boundary. Preserve a portable index
        # of the active checkpoint/tail in blob storage, archive opaque
        # bindings, and fence every writer holding the previous ref.
        summary = await store.load_thread_summary(ref)
        prior_provider = summary.provider
        portable_messages: tuple[dict[str, Any], ...] = ()
        if (
            prior_provider
            and prior_provider != "legacy"
            and prior_provider != provider
        ):
            state = await store.load_active(ref)
            handoff_ref, portable_messages = (
                await _build_portable_handoff(
                    store,
                    state,
                    from_provider=prior_provider,
                    to_provider=provider,
                )
            )
            identity = (
                f"{workflow_id}:{context_node_id}:"
                f"{ref.generation}:{ref.thread_id}:{ref.epoch}:{provider}"
            )
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            ref = await store.fork_provider(
                ref,
                provider=provider,
                operation_id=f"specialized-provider-fork:{digest}",
                portable_handoff_ref=handoff_ref,
            )
        else:
            ref, portable_messages = await _pending_handoff_messages(
                store,
                ref,
            )
        prefix = operation_prefix or (
            f"specialized-context:{provider}:"
            f"{execution_id or delegated_task_id or session_id or uuid.uuid4().hex}"
        )
        return cls(
            store=store,
            writer=AgentContextTransitionWriter(store, ref),
            provider=provider,
            operation_prefix=prefix,
            fidelity=fidelity,
            resumable=resumable,
            portable_handoff_messages=portable_messages,
        )

    def augment_prompt(self, prompt: str) -> str:
        """Render a pending portable handoff into a prompt-only adapter."""

        if not self.portable_handoff_messages:
            return prompt
        transcript = json.dumps(
            list(self.portable_handoff_messages),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            "## Prior agent context (portable MessageWireV2 replay)\n"
            "The backend reconstructed the following committed context "
            "from the previous provider. Treat it as prior conversation "
            "state and continue from it.\n"
            f"{transcript}\n\n"
            "## Current request\n"
            f"{prompt}"
        )

    async def append_observable(
        self,
        event_type: str,
        payload: Any,
        *,
        operation_suffix: Optional[str] = None,
    ) -> None:
        """Append one exact provider boundary/event before presentation."""

        if operation_suffix is None:
            async with self._sequence_lock:
                self._sequence += 1
                operation_suffix = f"event:{self._sequence}"
        await self.writer.append_transition(
            event_type=event_type,
            operation_id=f"{self.operation_prefix}:{operation_suffix}",
            provider=self.provider,
            payload={
                "fidelity": self.fidelity,
                "resumable": self.resumable,
                "observable_only": self.fidelity == "observable_only",
                "non_resumable": not self.resumable,
                "event": _jsonable(payload),
            },
        )

    async def capture_provider_event(self, event: dict[str, Any]) -> None:
        """Raw stream sink called before buffers or 4,000-char UI truncation."""

        await self.append_observable("provider.stream", event)

    async def load_binding(
        self,
        binding_type: str,
    ) -> Optional[dict[str, Any]]:
        """Load the most recently committed binding of ``binding_type``."""

        bindings = await self.store.load_provider_bindings(
            self.writer.ref,
            provider=self.provider,
        )
        for binding in reversed(bindings):
            if binding.binding_type != binding_type:
                continue
            payload = await self.store.get_blob(binding.binding_ref)
            return dict(payload) if isinstance(payload, dict) else None
        return None

    async def bind_provider(
        self,
        binding_type: str,
        binding: dict[str, Any],
        *,
        operation_suffix: str,
        fidelity: ContextFidelity = "provider_bound",
    ) -> None:
        """Persist an opaque continuation identity outside graph/runtime data."""

        await self.store.bind_provider(
            self.writer.ref,
            provider=self.provider,
            binding_type=binding_type,
            binding=_jsonable(binding),
            operation_id=f"{self.operation_prefix}:{operation_suffix}",
            fidelity=fidelity,
        )


__all__ = [
    "SpecializedAgentContextBridge",
    "is_context",
]
