---
name: gcloud-skill
description: Work with Google Cloud via the official gcloud CLI — check auth/config, list and switch projects, manage Compute Engine instances, deploy and inspect Cloud Run services, work with Cloud Storage, and run any other gcloud command. Output is parsed JSON.
allowed-tools: "gcloud"
metadata:
  author: opencompany
  version: "1.0"
  category: deployment

---

# Google Cloud Skill

Wrapper over the official [Google Cloud CLI](https://cloud.google.com/sdk/gcloud/reference)
(`gcloud`). Typed operations for the core flows plus a `custom`
passthrough that covers the entire gcloud surface. JSON-capable
operations run with `--format=json`, so results come back parsed in
`result`.

Each call starts a fresh gcloud process — a few seconds of startup
latency per call is normal; don't retry a slow call.

## Tool: gcloud

### Operations

| Operation | Purpose | Key fields |
|---|---|---|
| `auth_list` | Credentialed accounts + which is active | — |
| `config_list` | Current configuration (account, project, region defaults) | — |
| `projects_list` | Projects the account can access (parsed JSON) | `limit` (default 50) |
| `set_project` | Set the configuration's default project | `project_id` (required) |
| `compute_instances_list` | Compute Engine instances (parsed JSON) | `zone` (optional filter), `project` |
| `compute_instance_start` | Start an instance | `instance`, `zone` (both required), `project` |
| `compute_instance_stop` | Stop an instance | `instance`, `zone` (both required), `project` |
| `compute_instance_describe` | Full instance details | `instance`, `zone` (both required), `project` |
| `run_deploy` | Deploy a Cloud Run service | `service`, `region` (required), exactly one of `source` / `image`, `allow_unauthenticated`, `project` |
| `run_services_list` | Cloud Run services (all regions unless filtered) | `region` (optional), `project` |
| `run_service_describe` | Cloud Run service details (URL, revisions) | `service`, `region` (both required), `project` |
| `storage_ls` | List buckets or objects | `url` (optional `gs://` URL; omit to list buckets), `project` |
| `storage_cp` | Copy files local <-> `gs://` | `src`, `dst` (required), `recursive`, `path` (working dir) |
| `storage_rm` | Delete objects | `url` (required `gs://` URL), `recursive` |
| `custom` | Any other gcloud command | `command` — exactly what you would type after `gcloud ` |

The `project` / `zone` / `region` fields map to `--project` /
`--zone` / `--region`. When `project` is empty, gcloud falls back to
the configured default — run `set_project` once instead of repeating
`project` on every call.

### Response

```json
{
  "operation": "projects_list",
  "success": true,
  "result": [{ "projectId": "my-project-123", "name": "My Project", "lifecycleState": "ACTIVE" }]
}
```

Parsed JSON lands in `result`; plain text (`set_project`, `storage_cp`
progress) lands in `stdout`. On failure the tool raises an error
carrying gcloud's own message — surface it verbatim; gcloud's errors
are precise (including "You do not currently have an active account
selected", which means the user needs to log in via Credentials ->
Google Cloud).

## Typical flows

Orient first when state is unknown:

```json
{ "operation": "auth_list" }
{ "operation": "config_list" }
{ "operation": "projects_list" }
{ "operation": "set_project", "project_id": "my-project-123" }
```

Compute Engine — zone is required for single-instance operations (get
it from the list output's `zone` field):

```json
{ "operation": "compute_instances_list" }
{ "operation": "compute_instance_stop", "instance": "my-vm", "zone": "us-central1-a" }
```

Cloud Run — deploy from a container image, or from source (source
deploys run Cloud Build and can take minutes; the 15-minute timeout
accommodates that):

```json
{ "operation": "run_deploy", "service": "my-api", "region": "us-central1", "image": "gcr.io/my-project/my-api:latest", "allow_unauthenticated": true }
{ "operation": "run_deploy", "service": "my-api", "region": "us-central1", "source": ".", "path": "app" }
{ "operation": "run_service_describe", "service": "my-api", "region": "us-central1" }
```

The deployed URL is in the describe/deploy result under
`status.url`.

Cloud Storage — local paths resolve against the workflow workspace:

```json
{ "operation": "storage_ls", "url": "gs://my-bucket/reports/" }
{ "operation": "storage_cp", "src": "gs://my-bucket/reports/latest.csv", "dst": "./downloads/" }
{ "operation": "storage_cp", "src": "./build", "dst": "gs://my-bucket/site/", "recursive": true }
```

## The full gcloud surface via custom

```json
{ "operation": "custom", "command": "iam service-accounts list" }
{ "operation": "custom", "command": "sql instances list" }
{ "operation": "custom", "command": "functions deploy my-fn --runtime python312 --trigger-http --region us-central1" }
{ "operation": "custom", "command": "container clusters list" }
{ "operation": "custom", "command": "logging read \"severity>=ERROR\" --limit 20 --format=json" }
{ "operation": "custom", "command": "services enable run.googleapis.com" }
```

Notes for `custom`:

- Add `--format=json` yourself when you want parsed output — it is not
  injected for you.
- `command` is parsed with `shlex.split` — wrap filter expressions and
  JSON in quotes as you would in a shell.
- Long-running commands get a 10-minute timeout; for very long
  operations prefer the `--async` flag and poll.
- `auth application-default login` mints Application Default
  Credentials for client-library code (stored isolated under
  OpenCompany's data directory). gcloud CLI commands themselves never
  need ADC.

## Authentication

Auth is owned by the gcloud CLI itself, isolated under OpenCompany's
data directory (a login you did in your own terminal is NOT visible
here):

- **Credentials Modal -> Google Cloud -> Login** — gcloud opens the
  browser itself for Google sign-in (localhost callback, so the
  browser must be on the server's machine). First use auto-installs
  the CLI (~100 MB; allow a few minutes).

If a command fails with an auth error, point the user at the Login
button; don't ask them to paste credentials in chat. If it fails with
"API not enabled", enable it via
`{ "operation": "custom", "command": "services enable <api>.googleapis.com" }`.

## Best practices

1. **Run `config_list` first** when state is unknown — it shows the
   active account and default project without side effects.
2. **Set the project once** (`set_project`) instead of repeating the
   `project` field.
3. **Destructive operations** (instance stop, `storage_rm`, deletes
   via `custom`) — confirm with the user before running unless they
   explicitly asked.
4. **Surface gcloud error messages verbatim** — don't paraphrase; they
   name the exact flag, permission, or API to fix.
5. **Costs are real** — starting instances and deploying services
   incurs billing; mention it when the user's intent is exploratory.
