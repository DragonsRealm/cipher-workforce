# Google Cloud Service

Self-contained plugin at [`server/nodes/gcloud/`](../server/nodes/gcloud/)
wrapping the official **Google Cloud CLI** (`gcloud`, pinned versioned
archive in `_install.py`). One dual-purpose node — `gcloudAction`
(workflow node + AI tool `gcloud`) — with typed core operations across
four surfaces (account/config, Compute Engine, Cloud Run, Cloud
Storage) plus a `custom` passthrough covering the entire gcloud surface
(IAM, Cloud SQL, Functions, GKE, logging, `services enable`, ...).

**Auth model: the gcloud CLI owns its own auth** — the Stripe/gh
pattern, with the cloudflare login variant (the CLI opens the browser
itself). OpenCompany never stores, reads, or injects a credential:

- `gcloud auth login --quiet` runs Google's OAuth flow with a loopback
  callback on a **random port** and opens the default browser itself —
  the handler never parses or proxies the authorize URL (no custom
  login UI; cf precedent, minus cf's fixed-port collision hazard).
- Credentials land in **gcloud's own store under the pinned config
  dir** (see isolation below), not the operator's global gcloud state.
- The credentials modal's connected badge is a synthetic `cli-managed`
  **marker OAuth row** (`store_oauth_tokens(provider="gcloud", ...)`),
  written only after the `gcloud auth list --filter=status:ACTIVE
  --format=json` probe confirms a live session (gcloud exits 0 in both
  auth states — exit codes are never trusted) and removed on logout.
- There is **no auth pre-flight** in the node: gcloud's own "You do
  not currently have an active account selected" error surfaces
  verbatim through the `NodeUserError` wrap (plus a Login-button hint).

**Config isolation — the load-bearing divergence from gh.** Every
invocation (ops AND auth) pins `CLOUDSDK_CONFIG=<DATA_DIR>/gcloud/`
(the vercel `--global-config` / `CLAUDE_CONFIG_DIR` idiom), so node
auth state never collides with the operator's own `~/.config/gcloud` /
`%APPDATA%\gcloud`. Consequence: a terminal `gcloud auth login` against
the operator's global config is **NOT visible to this node** — by
design (gh shares terminal sessions; gcloud deliberately does not,
because gcloud config also carries mutable project/region defaults the
node must own).

**ADC vs user credentials.** The node login mints gcloud USER
credentials only — gcloud CLI commands never read Application Default
Credentials, so nothing more is needed for any node op. Code using
Google client libraries will NOT see this session via ADC; the
documented escape hatch is the `custom` op running `auth
application-default login` (ADC then lands isolated at
`<DATA_DIR>/gcloud/application_default_credentials.json`).

## File map

| File | Role |
|---|---|
| `__init__.py` | `register_ws_handlers(WS_HANDLERS)` + `register_output_schema("gcloudAction", …)`; node auto-registers on import |
| `gcloud_action.py` | `GCloudActionNode(ActionNode)`, `usable_as_tool=True`, `tool_name="gcloud"`, multi-`@Operation` dispatch; `_run` = `ensure_gcloud_cli()` → `run_cli_command(env=gcloud_env(), cwd=…)` → non-zero exit → `NodeUserError` (stderr tail + Login hint). `ui_hints = {"outputMode": "terminal"}`; `_shape` omits empty keys and never ships raw stdout alongside a parsed `result` |
| `_handlers.py` | `gcloud_login` / `gcloud_logout` / `gcloud_status`; single-flight guard, 22 s response-budget shield with `pending` fallback (the cold install is ~100 MB), never-kill completion watcher, probe-gated marker + broadcast |
| `_service.py` | `gcloud_env()` (pinned `CLOUDSDK_CONFIG` + automation baseline: `CLOUDSDK_CORE_DISABLE_PROMPTS=1`, update-check + usage-reporting off, `NO_COLOR=1`), `login_env()` (strips `GOOGLE_APPLICATION_CREDENTIALS` / `CLOUDSDK_AUTH_ACCESS_TOKEN` / account+project overrides), `resolve_gcloud_light()` (no-download binary probe), `active_account()` (the session probe, 30 s — first call pays Python startup), `resolve_workdir()` (workspace-relative cwd) |
| `_install.py` | `ensure_gcloud_cli()` — **project-local, pooch-driven** (gh idiom): pinned versioned archive extracted under `package_dir("gcloud")`. The system-global gcloud is never consulted. Constant inner root `google-cloud-sdk/`, entry point `bin/gcloud(.cmd)`; `install.sh` deliberately not run (PATH/completion setup only); `gcloud storage` is native core — no `components install` |
| `_credentials.py` | `GCloudCredential` thin marker (`auth="custom"`, `resolve()` → `{}`); provider id `gcloud` deliberately separate from the Google Workspace `google` OAuth2 provider (different auth model, no storage-key overlap) |
| `meta.json` | Node color `#4285F4`. No co-located `icon.svg` — icon via `visuals.json: {"gcloudAction": {"icon": "lobehub:googlecloud", "skill": "gcloud-skill"}}` plus the lowercase `"gcloud"` alias (icon + color) required because the skill's `allowed-tools` token is the tool name, not the node type's snake_case |

Paired skill: [`server/skills/gcloud/gcloud-skill/SKILL.md`](../server/skills/gcloud/gcloud-skill/SKILL.md).
Palette group: `deployment` (shared with vercel/cloudflare — no
`groups.py` edit). Catalogue entry: fieldless github shape in
`credential_providers.json` (`kind: "oauth"`, `ws` block, no `fields`)
— zero frontend edits.

## Install matrix (verified against dl.google.com, v577.0.0)

Base URL `https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/`.
**Naming quirk locked by test:** the Windows bundled-python zip uses
the legacy `google-cloud-sdk-` filename prefix; every other versioned
asset uses `google-cloud-cli-` — the "normalized" Windows `-cli-`
spelling 404s. Do not fix it on a version bump.

| (system, machine) | Asset | Python |
|---|---|---|
| Windows/AMD64 + ARM64 | `google-cloud-sdk-<V>-windows-x86_64-bundled-python.zip` (~111 MB) | bundled (ARM64 runs the x86_64 zip under emulation — no ARM asset exists) |
| Linux/x86_64 | `google-cloud-cli-<V>-linux-x86_64.tar.gz` (~88 MB) | bundled |
| Linux/aarch64+arm64 | `google-cloud-cli-<V>-linux-arm.tar.gz` (~61 MB) | system python3 3.9–3.13 (`CLOUDSDK_PYTHON` override) |
| Darwin/x86_64 + arm64 | `google-cloud-cli-<V>-darwin-{x86_64,arm}.tar.gz` (~61 MB) | system python3 3.9–3.13 |

Cold-install budget: ~700 MB disk under `packages/gcloud/` (pooch
keeps the archive beside the ~15k-file extraction), 1–4 min wall —
far past the 22 s WS budget, hence the `pending: True` login response.
Per-call latency: every op is a fresh Python interpreter (~2–6 s on
Windows) — noted in `tool_description` so agents don't retry slow
calls.

## The login flow

`gcloud_login` spawns `gcloud auth login --quiet` with `login_env()`
and returns `{success, message}` — no url/verification_code (the CLI
opens the browser itself). Fast path: if `active_account()` is already
truthy the handler marks + returns without spawning. Hazards handled
(cf precedent): single-flight guard (repeat clicks return "already in
progress" instead of a second browser tab), pipes drained for the
process lifetime, and the completion watcher **never kills the
process** — on Windows `gcloud.cmd` is a cmd.exe shim around the
bundled Python; killing the wrapper orphans the child holding the
loopback callback socket. Background completion: `proc.wait()` ≤ 600 s
→ gate on `active_account()` → `mark_logged_in("gcloud",
email=account)` + `credential.oauth.connected` broadcast (shared
`services/cli_agent/_cli_auth.py` plumbing).

`gcloud_logout`: `gcloud auth revoke --all --quiet` (best-effort —
hits Google's revocation endpoint; offline failure downgrades to
success so the marker is still removed) → `credential.oauth.disconnected`.
`gcloud_status`: `{connected, logged_in, email}` from the probe — no
side effects.

## Operations

All JSON-capable ops run with `--format=json` (parsed into `result`);
`project` / `zone` / `region` fields map to `--project` / `--zone` /
`--region`, appended only when set.

| Op | argv shape | Notes |
|---|---|---|
| `auth_list` | `auth list --format=json` | |
| `config_list` | `config list --format=json` | |
| `projects_list` | `projects list --format=json --limit N` | |
| `set_project` | `config set project <id>` | Text output → `stdout` |
| `compute_instances_list` | `compute instances list --format=json [--zones Z] [--project P]` | |
| `compute_instance_start/stop` | `compute instances start\|stop <vm> --zone Z --format=json [--project P]` | zone+instance required; 300 s |
| `compute_instance_describe` | `compute instances describe <vm> --zone Z --format=json [--project P]` | |
| `run_deploy` | `run deploy <svc> --region R --quiet --format=json (--source D \| --image I) [--allow-unauthenticated] [--project P]` | exactly one of source/image; source cwd = workspace; 900 s (Cloud Build) |
| `run_services_list` | `run services list --format=json [--region R] [--project P]` | |
| `run_service_describe` | `run services describe <svc> --region R --format=json [--project P]` | region required |
| `storage_ls` | `storage ls [gs://url] --format=json [--project P]` | omit url → buckets |
| `storage_cp` | `storage cp [--recursive] <src> <dst>` | text/progress → `stdout`; cwd = workspace; 600 s |
| `storage_rm` | `storage rm [--recursive] <gs://url>` | `gs://` prefix enforced |
| `custom` | verbatim after `gcloud ` (shlex-split) | no `--format` injection; 600 s; cwd = `path`/workspace |

## Tests

[`server/tests/test_gcloud_plugin.py`](../server/tests/test_gcloud_plugin.py):
env builders (pinned config + hygiene vars; login stripping), install
resolution (7-key asset map, the Windows `google-cloud-sdk-` prefix
lock, constant inner root, no `shutil.which`, cached
`ensure_gcloud_cli`), op argv builders (required-field
`NodeUserError`s, exactly-one source/image, `gs://` enforcement,
per-op timeouts, `--project` only-when-set),
gcloud-error-surfaces-verbatim + `test_node_has_no_auth_preflight`,
login budget/single-flight/never-kill/no-url-proxying, marker +
broadcast introspection, fieldless catalogue shape, folder assets +
skill. Plus the generic `test_plugin_contract.py` /
`test_plugin_self_containment.py` suites (`gcloud` is in
`_MIGRATED_PLUGINS` + `_PLUGINS_WITH_HANDLERS`).
