"""Durable Context V2 storage.

The store is intentionally independent of agent/provider implementations.
Runtimes append exact transitions and receive bounded references; provider
adapters own rendering and replay validation.
"""

from services.agent_context.store import (
    AgentContextError,
    AgentContextStore,
    CompactionConflictError,
    ContextArchivedError,
    ContextNotFoundError,
    RevisionConflictError,
    StaleEpochError,
)
from services.agent_context.runtime import (
    AgentContextTransitionWriter,
    OpaqueCheckpointError,
    reconstruct_message_wire_v2,
    reconstruct_messages,
)
from services.agent_context.compaction import (
    AgentContextCompactionService,
    ContextCompactionCandidate,
    ContextCompactionPolicy,
    ContextCompactionResult,
    ProviderContextAdapter,
    ProviderContextCapabilities,
    get_provider_context_adapter,
    provider_context_request_options,
)
from services.agent_context.legacy import import_generation_zero_handoff

__all__ = [
    "AgentContextError",
    "AgentContextCompactionService",
    "AgentContextStore",
    "AgentContextTransitionWriter",
    "CompactionConflictError",
    "ContextCompactionCandidate",
    "ContextCompactionPolicy",
    "ContextCompactionResult",
    "ContextArchivedError",
    "ContextNotFoundError",
    "OpaqueCheckpointError",
    "RevisionConflictError",
    "ProviderContextAdapter",
    "ProviderContextCapabilities",
    "StaleEpochError",
    "get_provider_context_adapter",
    "import_generation_zero_handoff",
    "provider_context_request_options",
    "reconstruct_message_wire_v2",
    "reconstruct_messages",
]
