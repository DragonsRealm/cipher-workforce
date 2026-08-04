# Diagram source snapshot

- Captured: `2026-08-04T16:46:54.1938992+05:30`
- Branch: `main` (`ahead 2` of `origin/main`)
- Commit: `5f7522250eae7b0d02ddfd8ee3740fdf5be4c25e`
- Latest relevant commits:
  - `5f752225` — `fix(workflows): stop destroying node config and descriptions on ordinary saves`
  - `9f2dff7c` — `fix(identity): one principal resolver, cron identity + routing, drop dead route`

## Existing working-tree changes

These files were already modified when diagram production began and are user-owned:

- `.opencompany/workflows/AI Employee_example_workflow-1779102911870-cbc76c82.json`
- `server/config/model_registry.json`

The diagrams describe this working tree. Diagram production must not modify, stage, or revert either file.

## Mid-production re-audit

The following user-owned tracked files changed after kickoff and were re-read before final validation:

- `client/src/components/parameterPanel/ContextPanel.tsx`
- `client/src/components/parameterPanel/MemoryToolPanel.tsx`
- `server/nodes/context/_descriptor.py`
- `server/nodes/context/_handlers.py`
- `server/services/temporal/agent_activities.py`
- `server/services/temporal/agent_workflow.py`
- `server/tests/temporal/test_agent_workflow.py`

The Context panel changes make unknown pressure, fidelity, and provider-binding states explicit; the Memory panel removes a duplicated reset-policy control and stops presenting an unknown indexing state as indexed. The Context descriptor now forwards only declared policy fields instead of unrelated legacy node data, and the Context handler scopes journal reads to the active epoch after Reset or Clear while preserving older epochs as archived history. The Temporal changes preserve generation/session scope while walking agent edges and add an idempotent per-iteration operation identifier. These changes reinforce the Context-versus-Memory and agent-runtime semantics already encoded in diagrams 5, 8, and 11; no component or relationship topology changed. They remain user-owned and are excluded from the diagram commit.

Git could not enumerate `.pytest_cache/`, `server/.pytest_cache/`, or `test-temp-v2/` because access was denied. None is a diagram source.
