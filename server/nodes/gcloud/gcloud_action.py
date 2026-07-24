"""Google Cloud Action — typed core operations over the gcloud CLI,
plus a raw-command passthrough.

The gcloud CLI owns its own auth (gh/Stripe pattern): no pre-flight
check and no token injection here — gcloud reads its own credential
store under the pinned ``CLOUDSDK_CONFIG`` dir (populated by ``gcloud
auth login`` from the credentials modal), and its own "no active
account" error surfaces through the ``NodeUserError`` wrap. JSON-capable
operations use ``--format=json`` for machine-readable results.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.events import run_cli_command
from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._credentials import GCloudCredential

_PROJECT_OPS = [
    "projects_list",
    "compute_instances_list", "compute_instance_start", "compute_instance_stop", "compute_instance_describe",
    "run_deploy", "run_services_list", "run_service_describe",
    "storage_ls", "storage_cp", "storage_rm",
]
_SET_PROJECT = {"displayOptions": {"show": {"operation": ["set_project"]}}}
_COMPUTE = {"displayOptions": {"show": {"operation": ["compute_instances_list", "compute_instance_start", "compute_instance_stop", "compute_instance_describe"]}}}
_COMPUTE_INSTANCE = {"displayOptions": {"show": {"operation": ["compute_instance_start", "compute_instance_stop", "compute_instance_describe"]}}}
_RUN_REGION = {"displayOptions": {"show": {"operation": ["run_deploy", "run_service_describe", "run_services_list"]}}}
_RUN_SERVICE = {"displayOptions": {"show": {"operation": ["run_deploy", "run_service_describe"]}}}
_RUN_DEPLOY = {"displayOptions": {"show": {"operation": ["run_deploy"]}}}
_STORAGE_URL = {"displayOptions": {"show": {"operation": ["storage_ls", "storage_rm"]}}}
_STORAGE_CP = {"displayOptions": {"show": {"operation": ["storage_cp"]}}}
_STORAGE_RECURSIVE = {"displayOptions": {"show": {"operation": ["storage_cp", "storage_rm"]}}}
_PROJECTS_LIST = {"displayOptions": {"show": {"operation": ["projects_list"]}}}
_PATH_OPS = {"displayOptions": {"show": {"operation": ["run_deploy", "storage_cp", "custom"]}}}
_CUSTOM = {"displayOptions": {"show": {"operation": ["custom"]}}}

_ONESHOT_TIMEOUT = 120.0
_MUTATION_TIMEOUT = 300.0
_TRANSFER_TIMEOUT = 600.0
_DEPLOY_TIMEOUT = 900.0
_STDERR_TAIL_CHARS = 2000


class GCloudActionParams(BaseModel):
    operation: Literal[
        "auth_list", "config_list", "projects_list", "set_project",
        "compute_instances_list", "compute_instance_start", "compute_instance_stop", "compute_instance_describe",
        "run_deploy", "run_services_list", "run_service_describe",
        "storage_ls", "storage_cp", "storage_rm",
        "custom",
    ] = "config_list"

    # Shared: gcloud falls back to the configured default project when
    # unset (config set project); set explicitly to operate elsewhere.
    project: str = Field(
        default="",
        description="Target project id (optional — defaults to the configured project)",
        json_schema_extra={
            "placeholder": "my-project-123",
            "displayOptions": {"show": {"operation": _PROJECT_OPS}},
        },
    )

    # set_project
    project_id: str = Field(
        default="",
        description="Project id to set as the configuration default",
        json_schema_extra={"placeholder": "my-project-123", **_SET_PROJECT},
    )

    # compute
    zone: str = Field(
        default="",
        description="Compute zone (required for instance start/stop/describe; optional --zones filter for list)",
        json_schema_extra={"placeholder": "us-central1-a", **_COMPUTE},
    )
    instance: str = Field(
        default="",
        description="Compute Engine instance name",
        json_schema_extra={"placeholder": "my-vm", **_COMPUTE_INSTANCE},
    )

    # cloud run
    region: str = Field(
        default="",
        description="Cloud Run region (required for deploy/describe; optional filter for list)",
        json_schema_extra={"placeholder": "us-central1", **_RUN_REGION},
    )
    service: str = Field(
        default="",
        description="Cloud Run service name",
        json_schema_extra={"placeholder": "my-service", **_RUN_SERVICE},
    )
    source: str = Field(
        default="",
        description="Deploy from source directory (mutually exclusive with image)",
        json_schema_extra={"placeholder": ". or ./app (relative to the workspace)", **_RUN_DEPLOY},
    )
    image: str = Field(
        default="",
        description="Deploy a container image (mutually exclusive with source)",
        json_schema_extra={"placeholder": "gcr.io/my-project/my-image", **_RUN_DEPLOY},
    )
    allow_unauthenticated: bool = Field(default=False, json_schema_extra=_RUN_DEPLOY)

    # storage
    url: str = Field(
        default="",
        description="Cloud Storage URL (optional for ls; required for rm)",
        json_schema_extra={"placeholder": "gs://my-bucket/path", **_STORAGE_URL},
    )
    src: str = Field(
        default="",
        description="Copy source — local path or gs:// URL",
        json_schema_extra={"placeholder": "./file.txt or gs://bucket/file.txt", **_STORAGE_CP},
    )
    dst: str = Field(
        default="",
        description="Copy destination — local path or gs:// URL",
        json_schema_extra={"placeholder": "gs://bucket/dir/ or ./downloads/", **_STORAGE_CP},
    )
    recursive: bool = Field(default=False, json_schema_extra=_STORAGE_RECURSIVE)

    # projects_list
    limit: int = Field(default=50, ge=1, le=500, json_schema_extra=_PROJECTS_LIST)

    # Working directory (relative → workflow workspace).
    path: str = Field(
        default="",
        json_schema_extra={
            "placeholder": "Working directory (defaults to the workflow workspace)",
            **_PATH_OPS,
        },
    )

    # custom
    command: str = Field(
        default="",
        description="gcloud CLI command, exactly as typed after 'gcloud '",
        json_schema_extra={
            "placeholder": "iam service-accounts list | sql instances list | functions deploy ...",
            **_CUSTOM,
        },
    )

    model_config = ConfigDict(extra="ignore")


class GCloudActionOutput(BaseModel):
    operation: Optional[str] = None
    success: Optional[bool] = None
    result: Optional[Any] = None
    stdout: Optional[str] = None
    stderr_tail: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GCloudActionNode(ActionNode):
    type = "gcloudAction"
    display_name = "Google Cloud"
    subtitle = "gcloud CLI"
    group = ("deployment", "tool")
    description = (
        "Google Cloud via the gcloud CLI — projects, Compute Engine instances, "
        "Cloud Run deploys, Cloud Storage, or any gcloud command"
    )
    component_kind = "square"
    tool_name = "gcloud"
    tool_description = (
        "Interact with Google Cloud via the gcloud CLI. Operations: auth_list / config_list "
        "(session + config snapshot), projects_list, set_project (sets the default project — "
        "run this first if commands complain about a missing project), compute_instances_list / "
        "compute_instance_start / compute_instance_stop / compute_instance_describe (zone required "
        "for the last three), run_deploy (Cloud Run — exactly one of source/image, region required; "
        "builds can take minutes), run_services_list / run_service_describe, storage_ls / storage_cp "
        "/ storage_rm (gs:// URLs), custom (any other gcloud command — pass 'command' exactly as "
        "typed after 'gcloud ', e.g. 'iam service-accounts list', 'sql instances list', "
        "'functions deploy my-fn --runtime python312 --trigger-http', 'auth application-default "
        "login' to mint ADC for client libraries). The project/zone/region fields map to "
        "--project/--zone/--region flags. Auth is managed by the gcloud CLI itself — connect via "
        "Credentials -> Google Cloud (Login button). Each call starts a fresh gcloud process "
        "(a few seconds of startup latency is normal). "
        "Reference: https://cloud.google.com/sdk/gcloud/reference"
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    # start/stop/deploy/rm mutate cloud state.
    annotations = {"destructive": True, "readonly": False, "open_world": True}
    # OutputPanel renders textual output preformatted (gcloud tables /
    # copy progress are terminal text, not markdown).
    ui_hints = {"outputMode": "terminal"}
    credentials = (GCloudCredential,)
    task_queue = TaskQueue.REST_API
    usable_as_tool = True

    Params = GCloudActionParams
    Output = GCloudActionOutput

    # ---- shared plumbing -------------------------------------------------

    async def _run(
        self,
        argv: List[str],
        *,
        cwd: Optional[str] = None,
        timeout: float = _ONESHOT_TIMEOUT,
    ) -> Dict[str, Any]:
        """No auth pre-flight (gh/Stripe pattern) — gcloud authenticates
        from its own credential store under the pinned config dir, and
        its error (including "You do not currently have an active
        account selected") surfaces via the NodeUserError wrap below."""
        from ._install import ensure_gcloud_cli
        from ._service import gcloud_env

        try:
            binary = str(await ensure_gcloud_cli())
        except Exception as e:
            raise RuntimeError(f"Google Cloud CLI install failed: {e}") from e

        result = await run_cli_command(
            binary=binary,
            argv=argv,
            timeout=timeout,
            env=gcloud_env(),
            cwd=cwd,
        )
        if not result.get("success"):
            stderr = (result.get("stderr") or "").strip()
            detail = stderr[-_STDERR_TAIL_CHARS:] if stderr else (result.get("error") or "gcloud invocation failed")
            raise NodeUserError(
                f"gcloud {argv[0]} failed: {detail} "
                "(if this is an auth error, connect via Credentials -> Google Cloud -> Login)"
            )
        return result

    def _cwd(self, ctx: NodeContext, path: str, *, required: bool = False) -> Optional[str]:
        from ._service import resolve_workdir

        if not path and not required and not ctx.workspace_dir:
            return None
        return resolve_workdir(ctx.workspace_dir, path.strip())

    @staticmethod
    def _project_flag(params: "GCloudActionParams") -> List[str]:
        return ["--project", params.project.strip()] if params.project.strip() else []

    @staticmethod
    def _shape(operation: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Output-panel shaping: when gcloud returned JSON
        (--format=json ops), the parsed data IS the payload — the raw
        stdout string would just duplicate it as an unreadable blob
        (and pre-stringified JSON violates the output contract). Keys
        are omitted (not None'd) when empty so the panel shows only
        meaningful fields (`exclude_unset` preserves this)."""
        shaped: Dict[str, Any] = {"operation": operation, "success": True}
        parsed = result.get("result")
        stdout = (result.get("stdout") or "").strip()
        if parsed is not None:
            shaped["result"] = parsed
        elif stdout:
            shaped["stdout"] = stdout
        stderr = (result.get("stderr") or "").strip()
        if stderr:
            shaped["stderr_tail"] = stderr[-_STDERR_TAIL_CHARS:]
        return shaped

    # ---- core account / project ops --------------------------------------

    @Operation("auth_list", cost={"service": "gcloud", "action": "auth_list", "count": 1})
    async def auth_list(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        result = await self._run(["auth", "list", "--format=json"])
        return self._shape("auth_list", result)

    @Operation("config_list", cost={"service": "gcloud", "action": "config_list", "count": 1})
    async def config_list(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        result = await self._run(["config", "list", "--format=json"])
        return self._shape("config_list", result)

    @Operation("projects_list", cost={"service": "gcloud", "action": "projects_list", "count": 1})
    async def projects_list(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        argv = ["projects", "list", "--format=json", "--limit", str(params.limit)]
        result = await self._run(argv)
        return self._shape("projects_list", result)

    @Operation("set_project", cost={"service": "gcloud", "action": "set_project", "count": 1})
    async def set_project(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        project_id = params.project_id.strip()
        if not project_id:
            raise NodeUserError("project_id is required (e.g. 'my-project-123')")
        result = await self._run(["config", "set", "project", project_id])
        return self._shape("set_project", result)

    # ---- compute engine ---------------------------------------------------

    @Operation("compute_instances_list", cost={"service": "gcloud", "action": "compute_instances_list", "count": 1})
    async def compute_instances_list(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        argv = ["compute", "instances", "list", "--format=json"]
        if params.zone.strip():
            argv += ["--zones", params.zone.strip()]
        argv += self._project_flag(params)
        result = await self._run(argv)
        return self._shape("compute_instances_list", result)

    def _compute_instance_argv(self, verb: str, params: GCloudActionParams) -> List[str]:
        instance = params.instance.strip()
        if not instance:
            raise NodeUserError(f"instance is required for compute_instance_{verb}")
        zone = params.zone.strip()
        if not zone:
            raise NodeUserError(f"zone is required for compute_instance_{verb} (e.g. 'us-central1-a')")
        return ["compute", "instances", verb, instance, "--zone", zone, "--format=json", *self._project_flag(params)]

    @Operation("compute_instance_start", cost={"service": "gcloud", "action": "compute_instance_start", "count": 1})
    async def compute_instance_start(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        result = await self._run(self._compute_instance_argv("start", params), timeout=_MUTATION_TIMEOUT)
        return self._shape("compute_instance_start", result)

    @Operation("compute_instance_stop", cost={"service": "gcloud", "action": "compute_instance_stop", "count": 1})
    async def compute_instance_stop(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        result = await self._run(self._compute_instance_argv("stop", params), timeout=_MUTATION_TIMEOUT)
        return self._shape("compute_instance_stop", result)

    @Operation("compute_instance_describe", cost={"service": "gcloud", "action": "compute_instance_describe", "count": 1})
    async def compute_instance_describe(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        result = await self._run(self._compute_instance_argv("describe", params))
        return self._shape("compute_instance_describe", result)

    # ---- cloud run ----------------------------------------------------------

    @Operation("run_deploy", cost={"service": "gcloud", "action": "run_deploy", "count": 1})
    async def run_deploy(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        service = params.service.strip()
        if not service:
            raise NodeUserError("service is required (the Cloud Run service name)")
        region = params.region.strip()
        if not region:
            raise NodeUserError("region is required for run_deploy (e.g. 'us-central1')")
        source = params.source.strip()
        image = params.image.strip()
        if bool(source) == bool(image):
            raise NodeUserError("Set exactly one of source (deploy from directory) or image (deploy a container image)")
        argv = ["run", "deploy", service, "--region", region, "--quiet", "--format=json"]
        if source:
            argv += ["--source", source]
        else:
            argv += ["--image", image]
        if params.allow_unauthenticated:
            argv.append("--allow-unauthenticated")
        argv += self._project_flag(params)
        cwd = self._cwd(ctx, params.path, required=bool(source))
        result = await self._run(argv, cwd=cwd, timeout=_DEPLOY_TIMEOUT)
        return self._shape("run_deploy", result)

    @Operation("run_services_list", cost={"service": "gcloud", "action": "run_services_list", "count": 1})
    async def run_services_list(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        argv = ["run", "services", "list", "--format=json"]
        if params.region.strip():
            argv += ["--region", params.region.strip()]
        argv += self._project_flag(params)
        result = await self._run(argv)
        return self._shape("run_services_list", result)

    @Operation("run_service_describe", cost={"service": "gcloud", "action": "run_service_describe", "count": 1})
    async def run_service_describe(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        service = params.service.strip()
        if not service:
            raise NodeUserError("service is required (the Cloud Run service name)")
        region = params.region.strip()
        if not region:
            raise NodeUserError("region is required for run_service_describe (e.g. 'us-central1')")
        argv = ["run", "services", "describe", service, "--region", region, "--format=json", *self._project_flag(params)]
        result = await self._run(argv)
        return self._shape("run_service_describe", result)

    # ---- cloud storage ------------------------------------------------------

    @Operation("storage_ls", cost={"service": "gcloud", "action": "storage_ls", "count": 1})
    async def storage_ls(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        argv = ["storage", "ls"]
        if params.url.strip():
            argv.append(params.url.strip())
        argv += ["--format=json", *self._project_flag(params)]
        result = await self._run(argv)
        return self._shape("storage_ls", result)

    @Operation("storage_cp", cost={"service": "gcloud", "action": "storage_cp", "count": 1})
    async def storage_cp(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        src = params.src.strip()
        dst = params.dst.strip()
        if not src or not dst:
            raise NodeUserError("src and dst are both required (local path or gs:// URL)")
        argv = ["storage", "cp"]
        if params.recursive:
            argv.append("--recursive")
        argv += [src, dst]
        result = await self._run(argv, cwd=self._cwd(ctx, params.path), timeout=_TRANSFER_TIMEOUT)
        return self._shape("storage_cp", result)

    @Operation("storage_rm", cost={"service": "gcloud", "action": "storage_rm", "count": 1})
    async def storage_rm(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        url = params.url.strip()
        if not url.startswith("gs://"):
            raise NodeUserError("url is required and must be a gs:// URL (e.g. 'gs://my-bucket/path')")
        argv = ["storage", "rm"]
        if params.recursive:
            argv.append("--recursive")
        argv.append(url)
        result = await self._run(argv)
        return self._shape("storage_rm", result)

    # ---- passthrough ---------------------------------------------------------

    @Operation("custom", cost={"service": "gcloud", "action": "custom", "count": 1})
    async def custom(self, ctx: NodeContext, params: GCloudActionParams) -> Any:
        cmd = params.command.strip()
        if not cmd:
            raise NodeUserError("command is required (e.g. 'iam service-accounts list', 'sql instances list')")
        argv = shlex.split(cmd)
        result = await self._run(argv, cwd=self._cwd(ctx, params.path), timeout=_TRANSFER_TIMEOUT)
        return self._shape("custom", result)
