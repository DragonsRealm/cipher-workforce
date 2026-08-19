# Memory (`simpleMemory`)

| Field | Value |
|------|-------|
| **Category** | tool / memory (config node on `input-tools`) |
| **Backend handler** | [`server/nodes/tool/simple_memory/__init__.py::SimpleMemoryNode.memory`](../../../server/nodes/tool/simple_memory/__init__.py) |
| **Tests** | [`server/tests/services/memory/test_tool_store.py`](../../../server/tests/services/memory/test_tool_store.py), [`server/tests/nodes/test_tool_call_dispatch.py`](../../../server/tests/nodes/test_tool_call_dispatch.py) |
| **Skill (if any)** | - |
| **Dual-purpose tool** | ToolNode only — tool name `memory` |

> The wire type stays `simpleMemory` (stored verbatim in graphs, claude pool
> keys and migrations); only the display name is "Memory". The pre-RFC-0002
> markdown-transcript model this card once described is retired —
> conversation history lives in the plain conversation store (see the
> `context` card and [agent_context_flow.md](../../agent_context_flow.md)).

## Purpose

Durable, explicitly-invoked long-term memory for agents: stable facts,
preferences and decisions the model chooses to `remember`, later retrieved
with `recall`/`list`/`get` and maintained with `update`/`forget`. It is NOT
conversation history (that is the Context node's store) and nothing is
injected automatically — retrieval is always a tool call. The LLM-facing
`tool_description` is deliberately imperative ("check memory before answering
anything about the user; never claim ignorance without checking") because
with passive wording models answered from priors while the answer sat one
recall away.

## Inputs (handles)

| Handle | Connection type | Required | Purpose |
|--------|-----------------|----------|---------|
| `output-tool` → agent `input-tools` | tools | yes | Exposes the `memory` tool to the connected agent |

## Parameters

| Name | Type | Default | Required | Description |
|------|------|---------|----------|-------------|
| `reset_policy` | `preserve` \| `clear` | `preserve` | no | Whether Workflow Reset clears this node's namespace. In `server_controlled_fields`, so the model cannot override it through tool arguments. |

`SimpleMemoryParams` uses `extra="ignore"` so leftover legacy keys
(`memory_content`, `session_id`, ...) on migrated graphs are inert.

## Tool input (`SimpleMemoryToolInput`, `tool_schema_locked = True`)

One locked multi-operation schema: `operation` ∈ `remember | recall | list |
get | update | forget`, plus `content`/`title`/`category`/`tags`/`expires_at`
(remember/update patch), `query`/`categories`/`limit`/`cursor`
(recall/list), `memory_id`/`expected_version` (get/update/forget). The lock
means the plugin's schema and description always win over stale `ToolSchema`
DB rows.

## Storage

`MemoryToolStore` ([`services/memory/tool_store.py`](../../../server/services/memory/tool_store.py)) —
namespaced by `MemoryScope(owner_id, workflow_id, memory_node_id)`
(`agent_memory_namespaces` / `agent_memory_items` tables + FTS index).
Neither the client nor the model can supply a namespace; the tool derives it
from `NodeContext`, the panel handlers derive it from the authenticated
socket + persisted graph.

## Execution paths

- **In-process agent**: `handlers/tools.py::execute_tool` →
  `execute_as_tool(tool_args, node_params, ctx)` (ToolInput validation).
- **Temporal agent**: the AgentWorkflow schedules `node.simpleMemory.v1`
  with the model's unmerged `tool_args` in the payload; the legacy handler
  routes ToolNodes through `execute_as_tool`. Without that, args validated
  against `Params` (`extra="ignore"`) and every `remember` silently degraded
  to a `list` (locked by `test_tool_call_dispatch.py`).
- **Panel**: WS handlers in
  [`_handlers.py`](../../../server/nodes/tool/simple_memory/_handlers.py)
  (`list_memory_items` / `get_memory_item` / `remember_memory` /
  `update_memory_item` / `forget_memory_item` / `clear_memory_items`),
  authorized against the persisted graph; external sockets only.

## Side Effects

- **Database writes**: `agent_memory_items` (+ namespace row, FTS).
- **Broadcasts**: every durable mutation (both writers) fires the
  identity-only `memory.updated` CloudEvent
  ([`_events.py`](../../../server/nodes/tool/simple_memory/_events.py));
  the frontend invalidates `['memoryItems']` / `['memoryItem']`.
- **Workflow Reset**: `reset_execution_state` honors `reset_policy` —
  `clear` wipes only this node's namespace.

## Edge cases & known limits

- Retrieval is lexical (FTS) with optional embedding projections; recall
  quality depends on the model writing useful `content`/`tags`.
- `update`/`forget` require the item's `expected_version` (optimistic
  concurrency) — a stale version returns a user-correctable error.
- The panel declares `refetchOnMount: 'always'` because the agent mutates
  memory while the panel is closed (see CLAUDE.md notes).

## Related

- **Sibling**: the `context` node — conversation history (plain
  conversation store), deliberately separate from durable facts.
- **Architecture docs**: [agent_context_flow.md](../../agent_context_flow.md),
  [RFC-0002](../../../RFC-0002-AGENT-CONTEXT-AND-MEMORY.md) (Memory
  sections current; Context implementation superseded).
