# RFC-0002 — Agent Context V2 and Tool-Based Memory

Status: Implementing  
Graph version: 2  
Compatibility boundary: new workflow generations only

## 1. Summary

OpenCompany separates two concepts that were previously conflated:

- **Context** is the exact, backend-owned execution journal used to reconstruct
  the next provider request.
- **Memory** is an explicitly invoked tool used to store and retrieve durable
  facts, decisions, and user preferences.

The visible canvas nodes are declarative UI and policy surfaces. They never own
the journal, provider bindings, tenant scope, mutation rules, compaction
algorithm, or durable Memory items. Those responsibilities live in backend
services and are reached through the plugin and tool registries.

Each agent that declares the `requiresContext` capability has exactly one
system-managed Context companion. A Context owns multiple isolated threads:

1. an explicit chat/session ID selects a persistent session thread;
2. otherwise a delegated task ID selects a task thread;
3. otherwise the execution ID selects an execution thread.

The same Memory tool may be shared by multiple agents. Contexts are never
shared.

## 2. Decisions

- Reuse `MessageWireV2`, including ordered blocks, raw malformed tool
  arguments, signed/thought blocks, and provider continuation state.
- Preserve existing Temporal V1 histories. Context V2 is selected only by a
  new graph/generation and uses new workflow/activity type names.
- Keep Gemini on `generate_content`; an Interactions migration is out of
  scope.
- Do not reintroduce LangChain compatibility.
- Do not put runtime state in node parameters, node outputs, workflow JSON,
  status caches, ordinary WebSocket events, CloudEvents, or Temporal Event
  History.
- Use canonical IDs assigned by the existing workflow canonicalizer:
  `<workflow_id>:context:<ordinal>`.
- Memory survives Workflow Reset by default. Each Memory node may opt into
  `reset_policy=clear`.
- Provider/runtime fidelity is explicit. No integration claims access to
  hidden state a provider does not expose.

## 3. Plugin and UI contract

The backend NodeSpec is the source of truth.

### 3.1 Context node

The `context` plugin is a passive, system-managed configuration node:

```text
context.output-context -> agent.input-context
```

Its parameters are policy only:

```text
compaction_mode: auto | native | portable | disabled
trigger_ratio: float = 0.8
context_window_override?: integer
exact_tail_retention_count: integer
```

Its NodeSpec advertises `isContextPanel` and `systemManaged`. The frontend
renders the panel and calls backend handlers; it does not create journals,
choose thread IDs, calculate pressure, compact messages, or mutate epochs.

### 3.2 Memory node

`simpleMemory` remains the public workflow type but is a normal `ToolNode`:

```text
simpleMemory.output-tool -> agent.input-tools
```

The LLM-visible name is `memory`. Persisted configuration is limited to
operator policy such as `reset_policy`. Model arguments use a separate,
plugin-locked `ToolInput`; model arguments cannot override backend scope or
configuration.

The frontend Memory panel is a client of authorized backend CRUD/search
handlers. It does not hold items or implement mutation semantics.

## 4. Public backend contracts

```text
AgentContextRef {
  workflow_id,
  context_node_id,
  generation,
  thread_id,
  epoch,
  revision
}

AgentContextEvent {
  sequence,
  event_type,
  message_wire_v2?,
  payload_ref?,
  operation_id,
  provider,
  previous_hash,
  payload_hash
}

AgentContextCheckpoint {
  provider,
  strategy,
  covers_through_sequence,
  replay_payload_ref,
  active_token_count,
  source_revision,
  source_hash
}
```

`AgentContextStore` exposes:

```text
resolve_thread
load_active
append_transition
prepare_compaction
commit_checkpoint
start_epoch
fork_provider
archive
purge
```

It persists normalized thread, event, checkpoint, provider-binding,
compaction-attempt, and optional hash-addressed blob records. Append and
checkpoint activation are transactional, operation-ID idempotent, hash
chained, revision guarded, and epoch fenced. `RuntimeMutation` stores only
committed identifiers and hashes.

## 5. Fidelity

`provider_replayable` means the retained state can construct the provider's
next public API request without losing exposed continuation data.

| Runtime | Fidelity |
|---|---|
| OpenAI native | provider-replayable Responses output and encrypted reasoning |
| Anthropic native | provider-replayable ordered blocks and signatures |
| Gemini native | provider-replayable `generate_content` parts/thought signatures |
| OpenAI-compatible | provider-replayable only where capability verification passes; otherwise portable |
| Claude Code | exact observable stream plus explicit provider UUID binding |
| Vertex | exact observable events plus interaction/environment binding |
| Codex CLI | observable-only, non-resumable |
| RLM | observable-only, non-resumable |

No row or UI label implies retention of hidden provider/runtime state.

## 6. Native transition order

`run_native_agent_loop` accepts an optional bound Context transition sink.
For every iteration it commits:

1. effective request snapshot, including resolved messages/system instruction,
   provider/model/settings, compiled tool definitions, attachments, and
   dynamic tool surface;
2. complete assistant `MessageWireV2`, before any requested tool executes;
3. each tool result, validation error, execution error, or ambiguous outcome,
   before another provider request;
4. final response and usage through the same journal.

Sink failure stops execution at the durability boundary. Progress broadcast
failure remains observational.

## 7. Usage and pressure

Two counters are never conflated:

- **Lifetime usage/cost** is append-only billing across provider and
  compaction calls.
- **Active context pressure** is retained next-request input plus requested
  output headroom.

Provider context adapters implement:

```text
capabilities(model)
measure_active_context(rendered_request)
compact(committed_prefix, policy)
validate_replay(candidate)
```

Central compaction orchestration branches on capabilities, not provider names.

## 8. Compaction invariants

- Only committed prefixes are compacted.
- A boundary never splits an assistant tool call from its tool results.
- Final-only responses are eligible.
- The active checkpoint plus exact event tail is the source; startup Markdown
  is never a source.
- A candidate must serialize for the provider, cover the claimed sequence, and
  reduce active tokens before activation.
- Activation is compare-and-swap against checkpoint and source hash.
- Concurrent writes remain an exact uncompacted tail.
- Raw journal events are immutable.
- Failure records the attempt and preserves the prior active checkpoint and
  pressure state.

OpenAI and Anthropic may use verified native compaction. Gemini and unsupported
providers use portable structured checkpoints. A provider switch starts a new
epoch with a portable handoff while archiving opaque provider state.

## 9. Memory tool contract

One multi-operation schema is exposed:

```text
remember(content, title?, category?, tags?, expires_at?)
recall(query, categories?, tags?, limit?, cursor?)
list(categories?, tags?, limit?, cursor?)
get(memory_id)
update(memory_id, expected_version, patch)
forget(memory_id, expected_version)
```

The backend derives the namespace from authenticated owner, workflow, and
Memory node. Namespace fields are absent from `ToolInput`. Items use optimistic
versions. Mutation receipts make retries idempotent. Expired items are omitted.
SQL/FTS is authoritative and remains usable when embedding generation fails;
embedding projections are rebuildable accelerators.

Memory does not automatically inject prompts, recall vectors, persist
transcripts, own provider identity, or compact Context.

## 10. Temporal

**Superseded.** This section originally specified a parallel `…V2` identity set
(`AgentWorkflowV2`, `agent.*.v2`) so V1 and V2 histories could run side by side.
That split was never needed — history back-compatibility is not a requirement
here — and it has been folded away. There is one workflow class, `AgentWorkflow`,
and one unsuffixed activity per concern: `agent.prepare_context`,
`agent.execute_llm_step`, `agent.append_context`, `agent.compact_context`.

What still holds: the child workflow carries only `AgentContextRef`, operation
IDs/hashes and iteration state, so provider messages and large tool results stay
out of Event History. The LLM activity keeps its one-attempt retry policy while
database commits retry idempotently, so an ambiguous provider outcome is recorded
rather than silently rebilled. `AgentWorkflow` uses Continue-As-New with a
bounded Context reference.

**Corrected by implementation — the Context journal does not feed the request.**
This RFC's replay-first framing led to an LLM step that reconstructed its request
from the journal whenever a Context node was attached. That is wrong in kind: the
Context node is an observation surface, and a node whose purpose is to *show* the
agent's state must not change what the agent sends. It also failed in practice —
the reconstruction dropped the user's prompt, so connecting a Context node made
the agent answer an empty question.

The rule is now inverted and load-bearing: **the request is always built from
`messages`; the journal records what was sent.** `agent.execute_llm_step` writes
each turn from the exact list it hands to `ChatUnifier.chat`, after the call
returns. `agent.prepare_context` journals nothing, because it runs before a
request exists and could only fabricate one. Journal operation ids derive from
the per-firing `context_execution_id`; deriving them from the generation-scoped
`execution_id` made every turn in a generation collide on idempotency and
silently discarded all but the first.

## 11. Graph normalization and lifecycle

`normalize_workflow_graph` is the single versioned pipeline used by
save/get/import/deploy and persisted-graph loading.

For each legacy `simpleMemory -> input-memory` edge it:

1. creates one Context for the destination agent;
2. canonicalizes IDs;
3. connects `output-context -> input-context`;
4. reconnects Simple Memory through `output-tool -> input-tools`;
5. stores recognized Markdown as an immutable `legacy_partial` artifact;
6. moves recoverable Claude/Vertex bindings into Context;
7. never converts transcript text into durable Memory items;
8. reports that process-local vectors cannot be recovered.

State import receipts are idempotent and committed before legacy topology is
retired. APIs return the authoritative normalized graph and aliases; the
frontend replaces its draft rather than reproducing migration rules.

Backend lifecycle rules:

- create/copy/hot-spawn/Agent-Builder add a fresh Context companion;
- copying never copies a journal;
- deleting an agent archives its Context;
- deleting a required system edge is rejected or repaired;
- a Context cannot attach to two agents;
- capability discovery uses `requiresContext`, never renderer kind or a
  hardcoded agent type list.

## 12. Reset

- Clear Context archives the current epoch, fences late writes, clears active
  replay/bindings, and preserves Memory.
- Clear Memory removes items only.
- Workflow Reset always rotates/archives Context and execution state, then
  applies each Memory node's `reset_policy`.
- Todos retain their independent reset hook.
- Permanent Context deletion is an explicit purge.

## 13. Security and observability

Ordinary broadcasts contain metadata only:

```text
context.updated
context.compacted
context.epoch.started
memory.changed
```

`context.updated` is emitted from `AgentContextStore`'s commit boundary through
`services/agent_context/listeners.py`, so every writer is covered without
call-site code; `context.epoch.started` stays with `start_epoch`'s callers,
which own the `reason`. All three broadcast directly through the status
broadcaster rather than the Temporal event dispatcher — they have no workflow
consumers, so a Visibility query per journal append would buy nothing.
Notifications fire only after a durable commit, never on an idempotent replay,
and can never fail the commit that triggered them.

Authorized, paginated backend handlers fetch journal or Memory content on
demand. Provider bindings, signatures, recalled secrets, raw journal payloads,
and Memory contents are excluded from workflow exports, logs, CloudEvents,
status caches, generation `runtime_data`, and Temporal payloads. Server-side
sanitization is authoritative; the client sanitizer remains defense in depth
for legacy fields.

## 14. Rollout

1. Contracts, storage, node plugins, graph version.
2. Context topology behind a generation cutover; optional shadow dual-write.
3. Native transition sink and restart hash comparison.
4. Context-backed in-process generations and checkpoint compaction.
5. Temporal V2 and replay/Continue-As-New fixtures.
6. Vertex, Claude Code, Codex, and RLM fidelity adapters.
7. `ToolInput` and durable Memory; then remove automatic legacy injection.
8. Automatic graph/state normalization.
9. Retire V1 only when deployment telemetry reports no active V1 histories.

There is no fixed compatibility-release deadline.

## 15. Acceptance gates

Tests cover exact ordering/hashes, provider serialization, malformed arguments,
restart reconstruction, isolated thread kinds, concurrent append/CAS behavior,
crash boundaries, final-only compaction, rollback, checkpoint-plus-tail replay,
V1 and V2 Temporal replay, Continue-As-New, bounded payloads, provider binding
semantics, explicit CLI fidelity, Memory isolation/idempotency/expiry/versioning
and fallback search, Reset policies, lifecycle pairing/migration idempotency,
authorized pagination, and export/log/event redaction.

Release gates are the full backend suite, frontend Vitest, TypeScript
typecheck, lint, and production build.

## 16. Primary references

- OpenAI conversation state:
  <https://developers.openai.com/api/docs/guides/conversation-state>
- OpenAI compaction:
  <https://developers.openai.com/api/docs/guides/compaction>
- Anthropic compaction:
  <https://platform.claude.com/docs/en/build-with-claude/compaction>
- Temporal Continue-As-New:
  <https://docs.temporal.io/develop/python/continue-as-new>
