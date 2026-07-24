"""Google Cloud plugin contract tests.

Locks the plugin's load-bearing seams under the CLI-owns-auth (Stripe /
gh / cloudflare) pattern: env builders (pinned ``CLOUDSDK_CONFIG``
isolation; login env strips ambient credential vars so the session
probe reflects the stored login), install resolution (pinned versioned
archive into ``package_dir("gcloud")``; system gcloud never consulted;
the Windows bundled-python asset's legacy ``google-cloud-sdk-`` filename
prefix), the marker-token + CloudEvents broadcast contract
(``inspect.getsource`` introspection, ``test_credential_broadcasts``
style), op argv building, and the catalogue entry shape (fieldless —
gcloud owns the credentials).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

import nodes.gcloud._handlers as gcloud_handlers
import nodes.gcloud._install as gcloud_install
import nodes.gcloud._service as gcloud_service
import nodes.gcloud.gcloud_action as gcloud_action_mod
from nodes.gcloud import WS_HANDLERS
from nodes.gcloud._credentials import GCloudCredential
from nodes.gcloud.gcloud_action import GCloudActionNode, GCloudActionParams
from services.plugin import NodeContext
from services.plugin.base import NodeUserError

PLUGIN_DIR = Path(gcloud_service.__file__).parent
CONFIG_PATH = Path(gcloud_service.__file__).parents[2] / "config" / "credential_providers.json"


# --- _service env builders ----------------------------------------------------


def _pin_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(gcloud_service, "_config_dir", lambda: str(tmp_path / "gcloud-config"))


def test_gcloud_env_pins_isolated_config_and_hygiene_vars(monkeypatch, tmp_path):
    _pin_config_dir(monkeypatch, tmp_path)
    env = gcloud_service.gcloud_env()
    assert env["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud-config")
    assert env["CLOUDSDK_CORE_DISABLE_PROMPTS"] == "1"
    assert env["CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK"] == "1"
    assert env["CLOUDSDK_CORE_DISABLE_USAGE_REPORTING"] == "1"
    assert env["NO_COLOR"] == "1"


def test_login_env_strips_ambient_credential_vars(monkeypatch, tmp_path):
    """With CLOUDSDK_AUTH_ACCESS_TOKEN (or an account/project override)
    set, the session probe would report the ambient identity instead of
    the stored login — auth paths must consult gcloud's OWN store under
    the pinned config dir."""
    _pin_config_dir(monkeypatch, tmp_path)
    for var in gcloud_service._AMBIENT_CREDENTIAL_VARS:
        monkeypatch.setenv(var, "ambient")
    env = gcloud_service.login_env()
    for var in gcloud_service._AMBIENT_CREDENTIAL_VARS:
        assert var not in env
    assert env["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud-config")


def test_ops_env_keeps_ambient_vars(monkeypatch, tmp_path):
    # Ops leave ambient credential vars alone (headless path — gcloud
    # documents env-first resolution), unlike the auth paths.
    _pin_config_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("CLOUDSDK_AUTH_ACCESS_TOKEN", "ambient")
    env = gcloud_service.gcloud_env()
    assert env["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "ambient"


# --- _install resolution --------------------------------------------------------


def test_version_is_pinned():
    assert gcloud_install._VERSION.count(".") == 2
    assert "latest" not in gcloud_install._VERSION


def test_asset_map_covers_all_platforms():
    assert set(gcloud_install._ASSETS) == {
        ("Windows", "AMD64"),
        ("Windows", "ARM64"),
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
        ("Linux", "arm64"),
        ("Darwin", "x86_64"),
        ("Darwin", "arm64"),
    }


def test_windows_asset_keeps_legacy_sdk_prefix():
    """dl.google.com naming quirk: the Windows bundled-python zip uses
    the legacy ``google-cloud-sdk-`` prefix — the ``google-cloud-cli-``
    spelling 404s. A version bump must not "normalize" it."""
    for key in (("Windows", "AMD64"), ("Windows", "ARM64")):
        asset, binary = gcloud_install._ASSETS[key]
        assert asset.startswith("google-cloud-sdk-")
        assert asset.endswith("-windows-x86_64-bundled-python.zip")
        assert binary == "gcloud.cmd"
    # Every non-Windows asset uses the modern google-cloud-cli- prefix.
    for key, (asset, binary) in gcloud_install._ASSETS.items():
        if key[0] != "Windows":
            assert asset.startswith("google-cloud-cli-")
            assert binary == "gcloud"


def test_extracted_binary_path_uses_constant_inner_root(monkeypatch, tmp_path):
    monkeypatch.setattr(gcloud_install, "_package_root", lambda: tmp_path)
    target = gcloud_install._extracted_binary_path()
    assert target.parts[-3:-1] == ("google-cloud-sdk", "bin")
    assert target.name in ("gcloud", "gcloud.cmd")
    assert str(target).startswith(str(tmp_path))


def test_gcloud_binary_never_resolved_from_system_path():
    # Project-local contract: the system gcloud is never consulted.
    assert "which(" not in inspect.getsource(gcloud_install)
    assert "which(" not in inspect.getsource(gcloud_service)


async def test_ensure_gcloud_cli_installs_into_package_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(gcloud_install, "_cached_path", None)
    monkeypatch.setattr(gcloud_install, "_package_root", lambda: tmp_path)
    target = gcloud_install._extracted_binary_path()

    calls = {"installs": 0}

    def fake_fetch():
        calls["installs"] += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        return target

    monkeypatch.setattr(gcloud_install, "_fetch_cli_sync", fake_fetch)

    resolved = await gcloud_install.ensure_gcloud_cli()
    assert resolved == target
    assert calls["installs"] == 1
    # cached for subsequent calls without re-installing
    assert await gcloud_install.ensure_gcloud_cli() == target
    assert calls["installs"] == 1


# --- operations (no auth pre-flight — Stripe pattern) ---------------------------


def _wire(monkeypatch, tmp_path, run_impl):
    async def fake_ensure():
        return tmp_path / "gcloud"

    monkeypatch.setattr(gcloud_install, "ensure_gcloud_cli", fake_ensure)
    monkeypatch.setattr(gcloud_action_mod, "run_cli_command", run_impl)
    _pin_config_dir(monkeypatch, tmp_path)


def _ctx(tmp_path) -> NodeContext:
    return NodeContext(node_id="gc1", node_type="gcloudAction", workspace_dir=str(tmp_path))


def _capturing_run(captured, payload=None):
    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {"success": True, "result": payload, "stdout": "" if payload is not None else "done", "stderr": "", "error": None}

    return fake_run


async def test_auth_list_argv_and_pinned_env(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload=[{"account": "o@x.dev", "status": "ACTIVE"}]))
    node = GCloudActionNode()
    out = await node.auth_list(_ctx(tmp_path), GCloudActionParams(operation="auth_list"))
    assert captured["argv"] == ["auth", "list", "--format=json"]
    assert captured["env"]["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud-config")
    assert out["result"] == [{"account": "o@x.dev", "status": "ACTIVE"}]


async def test_projects_list_carries_limit(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload=[]))
    node = GCloudActionNode()
    await node.projects_list(_ctx(tmp_path), GCloudActionParams(operation="projects_list", limit=7))
    assert captured["argv"] == ["projects", "list", "--format=json", "--limit", "7"]


async def test_set_project_requires_id_and_is_textual(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured))
    node = GCloudActionNode()

    with pytest.raises(NodeUserError, match="project_id"):
        await node.set_project(_ctx(tmp_path), GCloudActionParams(operation="set_project"))

    out = await node.set_project(_ctx(tmp_path), GCloudActionParams(operation="set_project", project_id="proj-1"))
    assert captured["argv"] == ["config", "set", "project", "proj-1"]
    assert "--format=json" not in captured["argv"]
    assert out["stdout"] == "done"


async def test_compute_instances_list_optional_zone_and_project(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload=[]))
    node = GCloudActionNode()

    await node.compute_instances_list(_ctx(tmp_path), GCloudActionParams(operation="compute_instances_list"))
    assert captured["argv"] == ["compute", "instances", "list", "--format=json"]

    await node.compute_instances_list(
        _ctx(tmp_path),
        GCloudActionParams(operation="compute_instances_list", zone="us-central1-a", project="proj-1"),
    )
    assert captured["argv"] == [
        "compute", "instances", "list", "--format=json",
        "--zones", "us-central1-a", "--project", "proj-1",
    ]


async def test_compute_instance_ops_require_instance_and_zone(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload={}))
    node = GCloudActionNode()

    with pytest.raises(NodeUserError, match="instance"):
        await node.compute_instance_start(_ctx(tmp_path), GCloudActionParams(operation="compute_instance_start"))
    with pytest.raises(NodeUserError, match="zone"):
        await node.compute_instance_stop(
            _ctx(tmp_path), GCloudActionParams(operation="compute_instance_stop", instance="vm1")
        )

    await node.compute_instance_start(
        _ctx(tmp_path),
        GCloudActionParams(operation="compute_instance_start", instance="vm1", zone="us-central1-a"),
    )
    assert captured["argv"] == ["compute", "instances", "start", "vm1", "--zone", "us-central1-a", "--format=json"]
    assert captured["timeout"] == gcloud_action_mod._MUTATION_TIMEOUT

    await node.compute_instance_describe(
        _ctx(tmp_path),
        GCloudActionParams(operation="compute_instance_describe", instance="vm1", zone="us-central1-a", project="p"),
    )
    assert captured["argv"] == [
        "compute", "instances", "describe", "vm1", "--zone", "us-central1-a", "--format=json", "--project", "p",
    ]


async def test_run_deploy_validates_and_builds_argv(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload={}))
    node = GCloudActionNode()

    with pytest.raises(NodeUserError, match="service"):
        await node.run_deploy(_ctx(tmp_path), GCloudActionParams(operation="run_deploy"))
    with pytest.raises(NodeUserError, match="region"):
        await node.run_deploy(_ctx(tmp_path), GCloudActionParams(operation="run_deploy", service="svc"))
    # exactly one of source/image — neither and both must fail
    with pytest.raises(NodeUserError, match="exactly one"):
        await node.run_deploy(
            _ctx(tmp_path), GCloudActionParams(operation="run_deploy", service="svc", region="us-central1")
        )
    with pytest.raises(NodeUserError, match="exactly one"):
        await node.run_deploy(
            _ctx(tmp_path),
            GCloudActionParams(operation="run_deploy", service="svc", region="us-central1", source=".", image="img"),
        )

    await node.run_deploy(
        _ctx(tmp_path),
        GCloudActionParams(
            operation="run_deploy", service="svc", region="us-central1",
            image="gcr.io/p/i", allow_unauthenticated=True, project="p",
        ),
    )
    assert captured["argv"] == [
        "run", "deploy", "svc", "--region", "us-central1", "--quiet", "--format=json",
        "--image", "gcr.io/p/i", "--allow-unauthenticated", "--project", "p",
    ]
    assert captured["timeout"] == gcloud_action_mod._DEPLOY_TIMEOUT

    # --source deploys run from the workspace (cwd resolved).
    await node.run_deploy(
        _ctx(tmp_path),
        GCloudActionParams(operation="run_deploy", service="svc", region="us-central1", source="."),
    )
    assert captured["cwd"] == str(tmp_path)
    argv = captured["argv"]
    assert argv[argv.index("--source") + 1] == "."


async def test_run_services_list_and_describe(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload=[]))
    node = GCloudActionNode()

    await node.run_services_list(_ctx(tmp_path), GCloudActionParams(operation="run_services_list"))
    assert captured["argv"] == ["run", "services", "list", "--format=json"]

    with pytest.raises(NodeUserError, match="region"):
        await node.run_service_describe(
            _ctx(tmp_path), GCloudActionParams(operation="run_service_describe", service="svc")
        )

    await node.run_service_describe(
        _ctx(tmp_path),
        GCloudActionParams(operation="run_service_describe", service="svc", region="us-central1"),
    )
    assert captured["argv"] == ["run", "services", "describe", "svc", "--region", "us-central1", "--format=json"]


async def test_storage_ops(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload=[]))
    node = GCloudActionNode()

    await node.storage_ls(_ctx(tmp_path), GCloudActionParams(operation="storage_ls"))
    assert captured["argv"] == ["storage", "ls", "--format=json"]
    await node.storage_ls(_ctx(tmp_path), GCloudActionParams(operation="storage_ls", url="gs://b/x"))
    assert captured["argv"] == ["storage", "ls", "gs://b/x", "--format=json"]

    with pytest.raises(NodeUserError, match="src and dst"):
        await node.storage_cp(_ctx(tmp_path), GCloudActionParams(operation="storage_cp", src="a.txt"))
    await node.storage_cp(
        _ctx(tmp_path),
        GCloudActionParams(operation="storage_cp", src="./dir", dst="gs://b/dir/", recursive=True),
    )
    assert captured["argv"] == ["storage", "cp", "--recursive", "./dir", "gs://b/dir/"]
    assert "--format=json" not in captured["argv"]
    assert captured["timeout"] == gcloud_action_mod._TRANSFER_TIMEOUT

    with pytest.raises(NodeUserError, match="gs://"):
        await node.storage_rm(_ctx(tmp_path), GCloudActionParams(operation="storage_rm", url="my-bucket"))
    await node.storage_rm(_ctx(tmp_path), GCloudActionParams(operation="storage_rm", url="gs://b/x", recursive=True))
    assert captured["argv"] == ["storage", "rm", "--recursive", "gs://b/x"]


async def test_custom_requires_command_and_shlex_splits(monkeypatch, tmp_path):
    captured = {}
    _wire(monkeypatch, tmp_path, _capturing_run(captured, payload={"ok": True}))
    node = GCloudActionNode()

    with pytest.raises(NodeUserError):
        await node.custom(_ctx(tmp_path), GCloudActionParams(operation="custom", command="  "))

    await node.custom(
        _ctx(tmp_path),
        GCloudActionParams(operation="custom", command="iam service-accounts list --format=json --filter 'displayName:CI Bot'"),
    )
    assert captured["argv"] == ["iam", "service-accounts", "list", "--format=json", "--filter", "displayName:CI Bot"]
    assert captured["timeout"] == gcloud_action_mod._TRANSFER_TIMEOUT


async def test_gcloud_auth_error_surfaces_as_node_user_error(monkeypatch, tmp_path):
    """No pre-flight: gcloud's own 'no active account' error IS the
    auth error, plus the Login-button hint."""

    async def fake_run(**kwargs):
        return {
            "success": False,
            "stdout": "",
            "stderr": "ERROR: (gcloud.projects.list) You do not currently have an active account selected.",
            "error": "exit 1",
        }

    _wire(monkeypatch, tmp_path, fake_run)
    node = GCloudActionNode()
    with pytest.raises(NodeUserError, match="active account"):
        await node.projects_list(_ctx(tmp_path), GCloudActionParams(operation="projects_list"))


def test_shape_prefers_parsed_result_over_stdout():
    shaped = GCloudActionNode._shape("op", {"result": [{"a": 1}], "stdout": '[{"a": 1}]', "stderr": ""})
    assert shaped["result"] == [{"a": 1}]
    assert "stdout" not in shaped

    shaped = GCloudActionNode._shape("op", {"result": None, "stdout": "Updated property [core/project].", "stderr": "warn"})
    assert shaped["stdout"] == "Updated property [core/project]."
    assert "result" not in shaped
    assert shaped["stderr_tail"] == "warn"


def test_node_has_no_auth_preflight():
    # Stripe-strict: the CLI owns auth; the node must not pre-check it.
    assert not hasattr(GCloudActionNode, "_preflight")
    src = inspect.getsource(gcloud_action_mod)
    assert "PermissionError" not in src


# --- login handler --------------------------------------------------------------


def _reset_login_state(monkeypatch):
    monkeypatch.setattr(gcloud_handlers, "_active_login", {"task": None, "proc": None})


async def test_login_answers_within_budget_when_flow_stalls(monkeypatch):
    _reset_login_state(monkeypatch)

    async def stalled_flow():
        await asyncio.sleep(30)
        return {"success": True, "message": "done"}

    monkeypatch.setattr(gcloud_handlers, "_start_login_flow", stalled_flow)
    monkeypatch.setattr(gcloud_handlers, "_RESPONSE_BUDGET_SECONDS", 0.05)
    res = await gcloud_handlers.handle_gcloud_login({}, websocket=None)
    assert res["success"] is True
    assert res.get("pending") is True


def test_response_budget_fits_frontend_window():
    assert gcloud_handlers._RESPONSE_BUDGET_SECONDS <= 25


async def test_login_response_never_proxies_a_url(monkeypatch):
    # The CLI owns the whole interaction — it opens the browser itself.
    _reset_login_state(monkeypatch)

    async def instant_flow():
        return {"success": True, "message": "gcloud is opening your default browser."}

    monkeypatch.setattr(gcloud_handlers, "_start_login_flow", instant_flow)
    res = await gcloud_handlers.handle_gcloud_login({}, websocket=None)
    assert res["success"] is True
    assert "url" not in res
    assert "verification_code" not in res
    src = inspect.getsource(gcloud_handlers._start_login_flow)
    assert '"url"' not in src


async def test_login_is_single_flight(monkeypatch):
    # A repeat Login click while a flow is live must not spawn a second
    # browser tab + competing loopback callback server.
    _reset_login_state(monkeypatch)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_flow():
        started.set()
        await release.wait()
        return {"success": True, "message": "done"}

    monkeypatch.setattr(gcloud_handlers, "_start_login_flow", slow_flow)
    monkeypatch.setattr(gcloud_handlers, "_RESPONSE_BUDGET_SECONDS", 0.05)

    first = await gcloud_handlers.handle_gcloud_login({}, websocket=None)
    assert first.get("pending") is True
    await started.wait()

    def boom():
        raise AssertionError("second click must not spawn a second flow")

    monkeypatch.setattr(gcloud_handlers, "_start_login_flow", boom)
    second = await gcloud_handlers.handle_gcloud_login({}, websocket=None)
    assert second["success"] is True
    assert second.get("pending") is True
    assert "already in progress" in second["message"].lower()

    release.set()


def test_login_flow_short_circuits_when_already_logged_in():
    src = inspect.getsource(gcloud_handlers._start_login_flow)
    assert "active_account" in src
    assert "Already logged in" in src


def test_completion_never_kills_the_login_process():
    # gcloud.cmd is a cmd.exe shim around the bundled Python on Windows:
    # killing the wrapper orphans the python child holding the loopback
    # callback socket.
    src = inspect.getsource(gcloud_handlers._complete_login)
    assert ".kill(" not in src
    assert ".terminate(" not in src


# --- marker-token + broadcast contract (source introspection) -------------------


def test_login_success_gates_on_probe_then_marks_and_broadcasts():
    # Marker + broadcast plumbing is the SHARED module (claude/codex/github/cloudflare/gcloud).
    assert gcloud_handlers.mark_logged_in.__module__ == "services.cli_agent._cli_auth"
    complete = inspect.getsource(gcloud_handlers._complete_login)
    # exit code alone is never trusted — gcloud exits 0 logged-in AND
    # logged-out; the `auth list` probe is the gate (and the
    # account-label email source).
    assert "active_account" in complete
    assert "_mark_connected" in complete
    marked = inspect.getsource(gcloud_handlers._mark_connected)
    assert 'mark_logged_in("gcloud"' in marked
    assert "credential.oauth.connected" in marked
    probe = inspect.getsource(gcloud_service.active_account)
    assert '"auth", "list"' in probe
    assert "status:ACTIVE" in probe


def test_logout_removes_marker_and_broadcasts():
    assert gcloud_handlers.mark_logged_out.__module__ == "services.cli_agent._cli_auth"
    src = inspect.getsource(gcloud_handlers.handle_gcloud_logout)
    assert 'mark_logged_out("gcloud")' in src
    assert "credential.oauth.disconnected" in src
    # revoke is best-effort — offline failure must not block marker removal
    assert '"revoke"' in src or '"auth", "revoke"' in src


def test_login_uses_stripped_env():
    for fn in (gcloud_handlers._start_login_flow, gcloud_handlers.handle_gcloud_logout):
        assert "login_env" in inspect.getsource(fn)
    assert "login_env" in inspect.getsource(gcloud_service.active_account)


def test_ws_handlers_registered():
    assert set(WS_HANDLERS) == {"gcloud_login", "gcloud_logout", "gcloud_status"}


# --- catalogue + assets ----------------------------------------------------------


def test_catalogue_entry_is_github_fieldless_shape():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert "deployment" in config["categories"]
    gcloud = config["providers"]["gcloud"]
    assert gcloud["kind"] == "oauth"
    assert gcloud["category"] == "deployment"
    assert gcloud["icon_ref"] == "lobehub:googlecloud"
    assert gcloud["ws"] == {
        "login": "gcloud_login",
        "logout": "gcloud_logout",
        "status": "gcloud_status",
    }
    # Fieldless (github shape): pure CLI login — no token field, so the
    # Login button is always enabled (OAuthConnect gates on required
    # fields only) and OpenCompany never stores a credential.
    assert "fields" not in gcloud


def test_credential_class_shape():
    assert GCloudCredential.id == "gcloud"
    assert GCloudCredential.auth == "custom"


async def test_credential_resolve_is_empty_marker():
    # gh pattern: nothing to resolve — auth lives in gcloud's own store.
    assert await GCloudCredential.resolve() == {}


def test_plugin_folder_assets():
    # Icon is the official lobehub brand glyph via visuals.json — a
    # co-located icon.svg would silently override it.
    assert not (PLUGIN_DIR / "icon.svg").exists()
    meta = json.loads((PLUGIN_DIR / "meta.json").read_text(encoding="utf-8"))
    assert meta["color"] == "#4285F4"

    visuals = json.loads((PLUGIN_DIR.parent / "visuals.json").read_text(encoding="utf-8"))
    entry = visuals["gcloudAction"]
    assert entry["icon"] == "lobehub:googlecloud"
    assert entry["skill"] == "gcloud-skill"
    # tool_name ("gcloud") != snake_case(node type) — the lowercase
    # alias carries icon + color for the Master Skill row.
    alias = visuals["gcloud"]
    assert alias["icon"] == "lobehub:googlecloud"
    assert alias["color"].startswith("#")

    skill_md = PLUGIN_DIR.parents[1] / "skills" / "gcloud" / "gcloud-skill" / "SKILL.md"
    assert skill_md.exists()
