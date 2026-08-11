# Data Node, Vision Delegate, and Native Image Blocks

Three related capabilities shipped together (worktree `data-node-multimodal`):
the `dataSource` agent tool (raw local data), the `visionAnalyze` delegate
tool (vision for every host model), and the first increment of native image
blocks in the LLM provider layer. Research grounding: QwenLM/Qwen-MM-Plugins
(budget abstraction, disk-wide/context-narrow) and the 2026 provider APIs
(images in tool results on Anthropic/OpenAI-Responses/Gemini-3).

## `dataSource` (`server/nodes/tool/data_source/`)

A `ToolNode` (`tool_name="data"`, locked split schema like `simpleMemory`)
over **two path namespaces**:

- workspace-relative paths (`reports/q3.csv`) — always read+write;
- external mounts `mnt/<mount_name>/<rel>` — the first segment `mnt` is
  reserved; workspace ops refuse it.

Operations: `list`, `read`, `search`, `metadata`, `write`, `append`,
`copy_to_workspace`. **No delete** — deletion stays human-only in the
gallery. `list` at the root also returns the node's enabled mounts so the
model discovers them at runtime.

**Mounts** are machine-wide rows in the `data_mounts` table
(`services/data/mount_store.py`, owns its table like the memory tool store)
with a per-mount `writable` flag (default read-only). Save-time validation
refuses: relative/missing/non-dir paths, filesystem roots, the home dir
itself, anything overlapping `DATA_DIR` in either direction (credentials.db
/ workflow.db / workspaces), duplicate names/roots. Each node exposes a
subset via `Params.mounts` (names); a mount must be in the node subset AND
still in the global table at call time, so a global revoke wins instantly.

**Containment** for both roots goes through the existing
`resolve_within` / `resolve_entry_within` / `normalize_virtual_path`
(`nodes/filesystem/_backend.py`); `ValueError` is translated to
`NodeUserError` in `_paths.py`. External files never mint a `FileRef` —
they get a `MountEntry` dict whose only address is the `mnt/...` virtual
path (host paths never reach outputs, the DB, or LLM context; locked by a
serialized-output scan in tests). `copy_to_workspace` (never overwrites;
suffixes `-1`, `-2`, …) imports a mount file into the workspace and mints a
real `FileRef` — the bridge to previews, drags, and vision.

**Read tiers** (`_readers.py`, all bounded; `bound_result` caps the
serialized envelope at ~200 KB because tool results have no downstream
truncation): text (line window, encoding fallback + `encoding_guessed`),
csv (Sniffer, 500 rows / 100 cols / 2k-char cells), json (≤5 MiB, depth 8,
`pruned_paths`), pdf (pypdf via the `docs` extra, 20 pages/req), html
(bs4), xlsx (openpyxl `read_only`, 500 rows), image (Pillow **metadata
only** — and the `llm_media` vision opt-in for workspace refs), binary
(ref + sha256 — never bytes/base64; deliberately fixes `fileRead`'s
unbounded base64 fallback). 25 MiB absolute gate (`MEDIA_MAX_READ_BYTES`).

**Panel** (`client/src/components/parameterPanel/DataPanel.tsx`, dispatched
by the `isDataPanel` uiHint): mount CRUD (machine-wide, labeled as such),
per-node enable checkboxes bound to `parameters.mounts`, and a read-only
browser. Pure UI — rows/crumbs/writability come finished from the plugin's
WS handlers (`data_list_mounts/add/update/remove`, `data_browse`), all
`@ws_response` + external-socket + owner + saved-workflow checks (the
memory `_handlers.py` security template).

## `visionAnalyze` (`server/nodes/vision/vision_analyze/`)

The vision-delegate tool (`tool_name="vision"`): gives every agent —
including text-only host models — image understanding. `describe(image,
question?, budget)` and `extract_text(image)` load workspace image bytes at
the provider boundary (`read_media_bytes`, contained), fit them to a
visual-token budget, send them to a vision model (openai / anthropic /
gemini — official SDK request shapes, `detail` always explicit), and return
text. Provider + `vision_model` are operator Params (hidden from the LLM by
the split schema; the field is deliberately not named `model` — reserved
sibling-name magic in ParameterRenderer).

`services/media/image_fit.py` owns the budget abstraction (after
Qwen-MM-Plugins): `small|normal|large` in visual tokens →
`pixels = tokens × patch²`, `smart_resize` (aspect-preserving, patch-grid
snap, floor AND ceiling), JPEG q90 / PNG-for-alpha.

## Native image blocks (`services/llm/media.py` + protocol)

Refs in durable state, bytes only at the provider boundary:

- `ContentBlock.source` (protocol.py): durable
  `{kind:"file_ref", ref:<FileRef>, detail}` (~450 B) or transient
  `{kind:"bytes", media_type, data_b64}`. `_durable_source` in the wire
  codec **raises** on a bytes-kind source — the never-bytes rule enforced
  structurally, so a leak is a loud error, not a 2 MiB Temporal payload.
- Tools opt in by returning `llm_media: [{ref, detail}]` (max 8;
  `ref.workflow_id` required; png/jpeg/webp/gif). Producers today: the
  data node's image tier. Both agent loops attach the blocks
  (`agent_runtime.py` and `temporal/agent_activities.py`).
- `hydrate_image_blocks(messages, provider, model)` runs once per LLM step
  in `run_native_llm_step`, on deep copies — originals never mutate;
  per-image failures degrade to text placeholders; incapable models get
  `[Image attached: … cannot view images; metadata only]`.
- **Capability gate** `provider_supports_vision` reads
  `llm_defaults.json providers.<p>.vision.enabled`. Unknown ⇒ False ⇒
  text fallback — never emit a block a model isn't confirmed to accept
  (a rejected block would be journaled and resent every turn).
- **Encoders**: Anthropic renders hydrated images inside
  `tool_result.content` (the documented shape). OpenAI (Responses
  `input_image` in `function_call_output`; Chat Completions hoist-to-user)
  and Gemini (`inline_data` parts beside `function_response`) are the next
  increments — their `vision.enabled` stays `false` until then, so bytes
  are never hydrated just to be dropped. Text-only providers keep
  `visionAnalyze` as their vision path permanently.

## Tests

`tests/nodes/test_data_source_node.py` (spec/validation/path-security/
tiers/mount flows/no-host-path-leak), `tests/services/data/test_mount_store.py`
(validation matrix), `tests/nodes/test_vision_analyze.py` (budget math +
per-provider request shapes), `tests/llm/test_media_blocks.py` (codec
round-trip + never-bytes raise, `llm_media` contract, gated hydration,
Anthropic tool_result shape).
