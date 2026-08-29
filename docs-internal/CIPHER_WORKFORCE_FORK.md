# CIPHER Workforce — Fork Provenance

## Upstream

| Field | Value |
|---|---|
| Upstream repository | `zeenie-ai/OpenCompany` |
| Upstream URL | https://github.com/zeenie-ai/OpenCompany |
| License | MIT |
| Fork cut date | 2026-08-28 |
| Baseline commit (short) | `49d667e2` |
| DCS fork repository | `DragonsRealm/cipher-workforce` |

## Why we forked

cipher-workforce is CIPHER OS's native AI workforce capability layer. The
`zeenie-ai/OpenCompany` codebase provides the plugin-first workflow execution
engine (FastAPI backend + React Flow frontend) on which the DCS soul-dispatch
infrastructure is built. We forked rather than contributing upstream because:

1. DCS architectural constraints (cipherd as authoritative dispatcher;
   `CIPHER_APPROVAL_TOKEN` isolation; per-soul memory namespacing) require
   changes the upstream project does not intend to adopt.
2. The Telegram credential isolated-env pattern (`~/.cipheros/workforce/.env`)
   is CIPHER OS-specific and not appropriate for the general OpenCompany
   audience.
3. The `server/services/safe_env.py` env-scrubbing layer is tailored to the
   DCS blocked-prefix policy (`CIPHER_*`, `ANTHROPIC_*`, `STRIPE_*`,
   `GOOGLE_*`).

## DCS-specific changes (Phase 1)

| Change | File(s) | Purpose |
|---|---|---|
| Isolated credential env | `server/nodes/telegram/_cw_env.py` | Telegram bot token never reads `os.environ` |
| Shared env allowlist | `server/services/safe_env.py` | Single blocked-prefix constant for subprocess envs |
| Filesystem env scrub | `server/nodes/filesystem/_backend.py` | `inherit_env=False` + `build_safe_env()` |
| Process service env scrub | `server/services/process_service.py` | `build_safe_env()` replaces `{**os.environ, ...}` |
| Per-soul memory namespacing | `server/nodes/document/vector_store/__init__.py` | `soul_<id>` ChromaDB collections |
| Soul namespace helpers | `server/services/memory/soul_namespace.py` | `soul_collection_name()` / `soul_namespace()` |
| Config isolated env override | `server/core/config.py` | `env_file` list; isolated env loads last, overrides dev placeholders |
| Env scrubbing tests | `server/tests/test_env_scrubbing.py` | 13 red-proof tests (13/13 passing) |
| Fork provenance doc | This file | |
| Drift check script | `scripts/check_upstream_drift.py` | Weekly upstream comparison |
| Drift check CI | `.github/workflows/drift-check.yml` | Monday 09:00 UTC schedule |
| Reeve seed workflow | `.opencompany/workflows/DCS PM Reeve Soul Dispatch.json` | TaskManager + simpleMemory wired to AI Agent |

## Security notes

- `CIPHER_APPROVAL_TOKEN` must NEVER appear in `~/.cipheros/workforce/.env`
  or any cipher-workforce configuration. That token belongs to `cipherd` only
  (Orion architectural constraint D4). Violation would allow the capability
  plane to impersonate the dispatcher.
- The env allowlist in `server/services/safe_env.py` is the single source of
  truth for what child processes may inherit. All modifications require a
  corresponding test update in `server/tests/test_env_scrubbing.py`.
- The encrypted `API_KEY_ENCRYPTION_KEY` in `~/.cipheros/workforce/.env` was
  rotated from the committed dev placeholder at fork-cut time. Never use the
  committed dev placeholder in production.

## Argus review items (required before merge)

These items were positively identified by Argus as correctly implemented and
must not be regressed:

1. **Approval surface fails closed**: constant-time comparison preserved.
2. **Telegram token stays out of `os.environ`**: both `owner_chat_id` and bot
   token now read from `_cw_env.py` (isolated env), never `os.environ`.
3. **Soul-dispatch argv**: allowlist validated, no model content in argv,
   audit written before spawn.

## Drift monitoring

A weekly CI job (`.github/workflows/drift-check.yml`) compares the upstream
default branch against `BASELINE_SHA = 49d667e2` and posts a GitHub Actions
step summary. Security-relevant upstream commits should be reviewed and
cherry-picked manually.

The drift check script can also be run locally:

```bash
python3 scripts/check_upstream_drift.py
python3 scripts/check_upstream_drift.py --json
```

Set `GITHUB_TOKEN` in the environment to avoid API rate limits.
