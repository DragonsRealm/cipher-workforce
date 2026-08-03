"""Capability-driven compaction for Agent Context V2.

The raw journal is immutable.  This module only selects a committed
transaction prefix, asks a provider adapter for a replay candidate, validates
that candidate, and compare-and-swap activates a checkpoint in
``AgentContextStore``.

Provider-specific behavior is registered behind :class:`ProviderContextAdapter`;
the orchestration path never switches on provider names.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from models.agent_context import (
    AgentContextCheckpoint,
    AgentContextCompactionPlan,
    AgentContextEvent,
    AgentContextRef,
)
from services.agent_context.store import AgentContextStore
from services.llm.protocol import message_from_wire, message_to_wire
from services.model_registry import get_model_registry


PortableCompactor = Callable[
    [list[dict[str, Any]], "ContextCompactionPolicy"],
    Awaitable["ContextCompactionCandidate"],
]


@dataclass(frozen=True)
class ContextCompactionPolicy:
    mode: str = "auto"
    trigger_ratio: float = 0.8
    context_window_override: Optional[int] = None
    exact_tail_retention_count: int = 8

    @classmethod
    def from_mapping(
        cls,
        value: Optional[dict[str, Any]],
    ) -> "ContextCompactionPolicy":
        raw = value or {}
        mode = str(raw.get("compaction_mode") or "auto")
        if mode not in {"auto", "native", "portable", "disabled"}:
            mode = "auto"
        try:
            trigger_ratio = float(raw.get("trigger_ratio", 0.8))
        except (TypeError, ValueError):
            trigger_ratio = 0.8
        trigger_ratio = min(0.95, max(0.1, trigger_ratio))
        try:
            override = int(raw.get("context_window_override") or 0) or None
        except (TypeError, ValueError):
            override = None
        if override is not None:
            override = max(1024, override)
        try:
            tail = int(raw.get("exact_tail_retention_count", 8))
        except (TypeError, ValueError):
            tail = 8
        return cls(
            mode=mode,
            trigger_ratio=trigger_ratio,
            context_window_override=override,
            exact_tail_retention_count=max(1, min(1000, tail)),
        )


@dataclass(frozen=True)
class ProviderContextCapabilities:
    context_window: int
    supports_native_compaction: bool = False
    replay_fidelity: str = "provider_replayable"
    native_strategy: Optional[str] = None


@dataclass(frozen=True)
class ContextCompactionCandidate:
    strategy: str
    replay_payload: dict[str, Any]
    active_token_count: int
    lifetime_usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextCompactionResult:
    ref: AgentContextRef
    pressure_tokens: int
    context_window: int
    compacted: bool = False
    checkpoint: Optional[AgentContextCheckpoint] = None
    reason: str = ""
    lifetime_usage: dict[str, int] = field(default_factory=dict)


class ProviderContextAdapter(Protocol):
    """Provider replay/pressure boundary required by Context V2."""

    def capabilities(self, model: str) -> ProviderContextCapabilities: ...

    def measure_active_context(self, rendered_request: Any) -> int: ...

    async def compact(
        self,
        committed_prefix: Sequence[AgentContextEvent],
        policy: ContextCompactionPolicy,
        *,
        base_replay: Optional[dict[str, Any]] = None,
        resolved_messages: Optional[list[dict[str, Any]]] = None,
        portable_compactor: Optional[PortableCompactor] = None,
    ) -> Optional[ContextCompactionCandidate]: ...

    def validate_replay(
        self,
        candidate: ContextCompactionCandidate,
    ) -> bool: ...

    def request_options(
        self,
        *,
        model: str,
        policy: ContextCompactionPolicy,
        context_window: int,
    ) -> Optional[dict[str, Any]]: ...


class PortableContextAdapter:
    """Structured portable fallback used when native support is unverified."""

    def __init__(self, provider: str = "portable") -> None:
        self.provider = provider

    def capabilities(self, model: str) -> ProviderContextCapabilities:
        registry = get_model_registry()
        return ProviderContextCapabilities(
            context_window=registry.get_context_length(model, self.provider),
        )

    def measure_active_context(self, rendered_request: Any) -> int:
        # Used only when a provider did not return exact input usage.  The
        # estimate is deliberately conservative and deterministic.
        encoded = json.dumps(
            rendered_request,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return max(1, (len(encoded) + 2) // 3)

    async def compact(
        self,
        committed_prefix: Sequence[AgentContextEvent],
        policy: ContextCompactionPolicy,
        *,
        base_replay: Optional[dict[str, Any]] = None,
        resolved_messages: Optional[list[dict[str, Any]]] = None,
        portable_compactor: Optional[PortableCompactor] = None,
    ) -> Optional[ContextCompactionCandidate]:
        if portable_compactor is None:
            return None
        wires = (
            deepcopy(resolved_messages)
            if resolved_messages is not None
            else _base_messages(base_replay)
        )
        if resolved_messages is None:
            wires.extend(_message_wires(committed_prefix))
        if not wires:
            return None
        return await portable_compactor(wires, policy)

    def validate_replay(
        self,
        candidate: ContextCompactionCandidate,
    ) -> bool:
        messages = candidate.replay_payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return False
        try:
            for wire in messages:
                if not isinstance(wire, dict):
                    return False
                message_from_wire(wire)
        except (TypeError, ValueError):
            return False
        return candidate.active_token_count >= 0

    def request_options(
        self,
        *,
        model: str,
        policy: ContextCompactionPolicy,
        context_window: int,
    ) -> Optional[dict[str, Any]]:
        del model, policy, context_window
        return None


class NativeOutputContextAdapter(PortableContextAdapter):
    """Replay adapter for providers that emit a compaction output block."""

    def __init__(
        self,
        provider: str,
        *,
        model_pattern: str,
        payload_collection: str,
        compaction_type: str = "compaction",
        native_strategy: str,
        minimum_trigger_tokens: int = 1,
        durability_pause: bool = False,
    ) -> None:
        super().__init__(provider)
        self._model_pattern = re.compile(model_pattern, re.IGNORECASE)
        self._payload_collection = payload_collection
        self._compaction_type = compaction_type
        self._native_strategy = native_strategy
        self._minimum_trigger_tokens = minimum_trigger_tokens
        self._durability_pause = durability_pause

    def capabilities(self, model: str) -> ProviderContextCapabilities:
        base = super().capabilities(model)
        supported = bool(self._model_pattern.search(model or ""))
        return ProviderContextCapabilities(
            context_window=base.context_window,
            supports_native_compaction=supported,
            replay_fidelity=base.replay_fidelity,
            native_strategy=self._native_strategy if supported else None,
        )

    async def compact(
        self,
        committed_prefix: Sequence[AgentContextEvent],
        policy: ContextCompactionPolicy,
        *,
        base_replay: Optional[dict[str, Any]] = None,
        resolved_messages: Optional[list[dict[str, Any]]] = None,
        portable_compactor: Optional[PortableCompactor] = None,
    ) -> Optional[ContextCompactionCandidate]:
        native = self._native_candidate(committed_prefix)
        if native is not None:
            return native
        if policy.mode == "native":
            return None
        return await super().compact(
            committed_prefix,
            policy,
            base_replay=base_replay,
            resolved_messages=resolved_messages,
            portable_compactor=portable_compactor,
        )

    def request_options(
        self,
        *,
        model: str,
        policy: ContextCompactionPolicy,
        context_window: int,
    ) -> Optional[dict[str, Any]]:
        capabilities = self.capabilities(model)
        if (
            policy.mode not in {"auto", "native"}
            or not capabilities.supports_native_compaction
        ):
            return None
        threshold = max(
            self._minimum_trigger_tokens,
            int(context_window * policy.trigger_ratio),
        )
        if threshold >= context_window:
            return None
        return {
            "type": "compaction",
            "compact_threshold": threshold,
            "pause_after_compaction": self._durability_pause,
            "strategy": self._native_strategy,
        }

    def _native_candidate(
        self,
        committed_prefix: Sequence[AgentContextEvent],
    ) -> Optional[ContextCompactionCandidate]:
        for event in reversed(committed_prefix):
            wire = event.message_wire_v2
            if not isinstance(wire, dict):
                continue
            state = wire.get("provider_state")
            if not isinstance(state, dict) or state.get("provider") != self.provider:
                continue
            payload = state.get("payload")
            if not isinstance(payload, dict):
                continue
            blocks = payload.get(self._payload_collection)
            if not isinstance(blocks, list):
                continue
            marker_index = -1
            for index, block in enumerate(blocks):
                if (
                    isinstance(block, dict)
                    and block.get("type") == self._compaction_type
                ):
                    marker_index = index
            if marker_index < 0:
                continue
            compacted_wire = deepcopy(wire)
            compacted_state = compacted_wire["provider_state"]
            compacted_payload = compacted_state["payload"]
            # Provider documentation requires passing the canonical compacted
            # output unchanged.  We only discard blocks preceding the latest
            # provider-issued compaction marker.
            compacted_payload[self._payload_collection] = deepcopy(
                blocks[marker_index:]
            )
            measured = self.measure_active_context([compacted_wire])
            return ContextCompactionCandidate(
                strategy=self._native_strategy,
                replay_payload={"messages": [compacted_wire]},
                active_token_count=measured,
            )
        return None


_ADAPTERS: dict[str, ProviderContextAdapter] = {}


def register_provider_context_adapter(
    provider: str,
    adapter: ProviderContextAdapter,
) -> None:
    normalized = provider.strip().lower()
    if not normalized:
        raise ValueError("provider is required")
    current = _ADAPTERS.get(normalized)
    if current is not None and current is not adapter:
        raise ValueError(f"Context adapter already registered for {normalized}")
    _ADAPTERS[normalized] = adapter


def get_provider_context_adapter(provider: str) -> ProviderContextAdapter:
    normalized = str(provider or "").strip().lower()
    return _ADAPTERS.get(normalized) or PortableContextAdapter(
        normalized or "portable"
    )


def provider_context_request_options(
    *,
    provider: str,
    model: str,
    policy: ContextCompactionPolicy,
) -> Optional[dict[str, Any]]:
    adapter = get_provider_context_adapter(provider)
    capabilities = adapter.capabilities(model)
    context_window = (
        policy.context_window_override or capabilities.context_window
    )
    return adapter.request_options(
        model=model,
        policy=policy,
        context_window=context_window,
    )


class AgentContextCompactionService:
    """CAS checkpoint orchestration over one durable Context store."""

    def __init__(self, store: AgentContextStore) -> None:
        self.store = store

    async def update_pressure_and_compact(
        self,
        ref: AgentContextRef,
        *,
        operation_id: str,
        provider: str,
        model: str,
        policy: ContextCompactionPolicy,
        active_input_tokens: int,
        output_headroom: int,
        rendered_request: Any = None,
        portable_compactor: Optional[PortableCompactor] = None,
    ) -> ContextCompactionResult:
        adapter = get_provider_context_adapter(provider)
        capabilities = adapter.capabilities(model)
        context_window = (
            policy.context_window_override or capabilities.context_window
        )
        measured_input = (
            max(0, int(adapter.measure_active_context(rendered_request)))
            if rendered_request is not None
            else 0
        )
        pressure_tokens = max(
            0,
            active_input_tokens,
            measured_input,
        ) + max(
            0, output_headroom
        )
        current_ref = await self.store.record_active_pressure(
            ref,
            operation_id=f"{operation_id}:pressure",
            active_token_count=pressure_tokens,
        )
        if policy.mode == "disabled":
            return ContextCompactionResult(
                ref=current_ref,
                pressure_tokens=pressure_tokens,
                context_window=context_window,
                reason="disabled",
            )

        state = await self.store.load_active(current_ref)
        native_marker_sequence = _native_compaction_marker_sequence(
            adapter,
            state.tail,
        )
        if native_marker_sequence is not None:
            # A native marker is itself the provider-issued replay boundary.
            # Never claim later tool/final events as covered by a candidate
            # whose payload contains only state through this marker.
            coverage = native_marker_sequence
        else:
            coverage = select_compaction_boundary(
                state.tail,
                exact_tail_retention_count=(
                    policy.exact_tail_retention_count
                ),
            )
        if coverage is None:
            return ContextCompactionResult(
                ref=state.ref,
                pressure_tokens=pressure_tokens,
                context_window=context_window,
                reason="no_committed_prefix",
            )

        native_present = _has_native_compaction_marker(
            adapter,
            state.tail,
            coverage,
        )
        triggered = (
            context_window > 0
            and pressure_tokens >= int(context_window * policy.trigger_ratio)
        )
        if not native_present and not triggered:
            return ContextCompactionResult(
                ref=state.ref,
                pressure_tokens=pressure_tokens,
                context_window=context_window,
                reason="below_threshold",
            )
        if (
            policy.mode == "native"
            and not capabilities.supports_native_compaction
        ):
            return ContextCompactionResult(
                ref=state.ref,
                pressure_tokens=pressure_tokens,
                context_window=context_window,
                reason="native_unsupported",
            )

        strategy = (
            capabilities.native_strategy
            if native_present and capabilities.native_strategy
            else "portable"
        )
        plan = await self.store.prepare_compaction(
            state.ref,
            operation_id=f"{operation_id}:prepare",
            provider=provider,
            strategy=strategy,
            covers_through_sequence=coverage,
        )
        base_replay = await self._base_replay(plan)
        source_wires = await self._resolve_plan_messages(
            plan,
            base_replay=base_replay,
        )
        source_pressure = max(
            pressure_tokens,
            adapter.measure_active_context(source_wires)
            + max(0, output_headroom),
        )
        try:
            candidate = await adapter.compact(
                plan.committed_prefix,
                policy,
                base_replay=base_replay,
                resolved_messages=source_wires,
                portable_compactor=portable_compactor,
            )
            if candidate is None:
                await self.store.fail_compaction(
                    attempt_id=plan.attempt_id,
                    error_code="candidate_unavailable",
                )
                return ContextCompactionResult(
                    ref=state.ref,
                    pressure_tokens=pressure_tokens,
                    context_window=context_window,
                    reason="candidate_unavailable",
                )
            candidate_pressure = (
                candidate.active_token_count + max(0, output_headroom)
            )
            if (
                not adapter.validate_replay(candidate)
                or candidate_pressure >= source_pressure
            ):
                await self.store.fail_compaction(
                    attempt_id=plan.attempt_id,
                    error_code="candidate_validation_failed",
                )
                return ContextCompactionResult(
                    ref=state.ref,
                    pressure_tokens=pressure_tokens,
                    context_window=context_window,
                    reason="candidate_validation_failed",
                    lifetime_usage=candidate.lifetime_usage,
                )
            replay_ref = await self.store.put_blob(
                candidate.replay_payload
            )
            checkpoint = await self.store.commit_checkpoint(
                state.ref,
                attempt_id=plan.attempt_id,
                operation_id=f"{operation_id}:commit",
                replay_payload_ref=replay_ref,
                active_token_count=candidate_pressure,
            )
            latest = (await self.store.load_active(state.ref)).ref
            return ContextCompactionResult(
                ref=latest,
                pressure_tokens=candidate_pressure,
                context_window=context_window,
                compacted=True,
                checkpoint=checkpoint,
                reason=candidate.strategy,
                lifetime_usage=candidate.lifetime_usage,
            )
        except BaseException:
            await self.store.fail_compaction(
                attempt_id=plan.attempt_id,
                error_code="candidate_exception",
            )
            raise

    async def _base_replay(
        self,
        plan: AgentContextCompactionPlan,
    ) -> Optional[dict[str, Any]]:
        if plan.base_checkpoint is None:
            return None
        value = await self.store.get_blob(
            plan.base_checkpoint.replay_payload_ref
        )
        return value if isinstance(value, dict) else None

    async def _resolve_plan_messages(
        self,
        plan: AgentContextCompactionPlan,
        *,
        base_replay: Optional[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        snapshot_messages: Optional[list[dict[str, Any]]] = None
        snapshot_sequence = 0
        for event in reversed(plan.committed_prefix):
            if (
                event.event_type != "request.snapshot"
                or not event.payload_ref
            ):
                continue
            payload = await self.store.get_blob(event.payload_ref)
            candidates = (
                payload.get("messages")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(candidates, list):
                continue
            snapshot_messages = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise ValueError(
                        "request snapshot contains a non-message item"
                    )
                snapshot_messages.append(
                    dict(message_to_wire(message_from_wire(candidate)))
                )
            snapshot_sequence = event.sequence
            break
        wires = (
            snapshot_messages
            if snapshot_messages is not None
            else _base_messages(base_replay)
        )
        wires.extend(
            deepcopy(event.message_wire_v2)
            for event in plan.committed_prefix
            if event.sequence > snapshot_sequence
            and isinstance(event.message_wire_v2, dict)
        )
        return wires


def select_compaction_boundary(
    events: Sequence[AgentContextEvent],
    *,
    exact_tail_retention_count: int,
) -> Optional[int]:
    """Return the newest complete transaction before the retained exact tail."""

    if len(events) <= exact_tail_retention_count:
        return None
    last_candidate_index = len(events) - exact_tail_retention_count - 1
    for index in range(last_candidate_index, -1, -1):
        if _is_complete_transaction(events, index):
            return events[index].sequence
    return None


def _is_complete_transaction(
    events: Sequence[AgentContextEvent],
    index: int,
) -> bool:
    event = events[index]
    if event.event_type in {"response.final", "response.truncated"}:
        return True
    wire = event.message_wire_v2
    if event.event_type == "message.assistant" and isinstance(wire, dict):
        calls = wire.get("tool_calls")
        return not isinstance(calls, list) or not calls
    if event.event_type != "message.tool_result":
        return False

    assistant_index: Optional[int] = None
    for candidate in range(index - 1, -1, -1):
        if events[candidate].event_type == "message.assistant":
            assistant_index = candidate
            break
    if assistant_index is None:
        return False
    assistant_wire = events[assistant_index].message_wire_v2 or {}
    calls = assistant_wire.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return False
    requested = {
        str(call.get("id") or "")
        for call in calls
        if isinstance(call, dict)
    }
    completed: set[str] = set()
    for result in events[assistant_index + 1 : index + 1]:
        if result.event_type == "message.assistant":
            return False
        if result.event_type != "message.tool_result":
            continue
        result_wire = result.message_wire_v2 or {}
        completed.add(str(result_wire.get("tool_call_id") or ""))
    return bool(requested) and requested.issubset(completed)


def _message_wires(
    events: Sequence[AgentContextEvent],
) -> list[dict[str, Any]]:
    return [
        deepcopy(event.message_wire_v2)
        for event in events
        if isinstance(event.message_wire_v2, dict)
    ]


def _base_messages(
    replay: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(replay, dict):
        return []
    messages = replay.get("messages")
    if not isinstance(messages, list):
        return []
    return [
        deepcopy(wire)
        for wire in messages
        if isinstance(wire, dict)
    ]


def _has_native_compaction_marker(
    adapter: ProviderContextAdapter,
    tail: Sequence[AgentContextEvent],
    coverage: int,
) -> bool:
    if not isinstance(adapter, NativeOutputContextAdapter):
        return False
    prefix = [event for event in tail if event.sequence <= coverage]
    return adapter._native_candidate(prefix) is not None


def _native_compaction_marker_sequence(
    adapter: ProviderContextAdapter,
    tail: Sequence[AgentContextEvent],
) -> Optional[int]:
    if not isinstance(adapter, NativeOutputContextAdapter):
        return None
    for event in reversed(tail):
        if adapter._native_candidate([event]) is not None:
            return event.sequence
    return None


def _register_builtin_adapters() -> None:
    # OpenAI Responses emits an opaque encrypted ``compaction`` output item.
    register_provider_context_adapter(
        "openai",
        NativeOutputContextAdapter(
            "openai",
            model_pattern=r"^(gpt-5(?:[.-]|$)|o[134](?:[.-]|$))",
            payload_collection="output",
            native_strategy="openai_responses_compaction",
        ),
    )
    # Anthropic's beta Messages compaction returns a replayable compaction
    # block and supports a pause so the block can be committed durably first.
    register_provider_context_adapter(
        "anthropic",
        NativeOutputContextAdapter(
            "anthropic",
            model_pattern=(
                r"^claude-(?:sonnet-4-6|opus-4-[678]|"
                r"fable-5|mythos(?:-5|-preview))$"
            ),
            payload_collection="content",
            native_strategy="anthropic_compact_20260112",
            minimum_trigger_tokens=50_000,
            durability_pause=True,
        ),
    )
    register_provider_context_adapter(
        "gemini",
        PortableContextAdapter("gemini"),
    )


_register_builtin_adapters()


__all__ = [
    "AgentContextCompactionService",
    "ContextCompactionCandidate",
    "ContextCompactionPolicy",
    "ContextCompactionResult",
    "NativeOutputContextAdapter",
    "PortableCompactor",
    "PortableContextAdapter",
    "ProviderContextAdapter",
    "ProviderContextCapabilities",
    "get_provider_context_adapter",
    "provider_context_request_options",
    "register_provider_context_adapter",
    "select_compaction_boundary",
]
