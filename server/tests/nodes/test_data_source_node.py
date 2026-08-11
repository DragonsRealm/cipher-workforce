"""Contracts for the Data node: spec invariants, locked ToolInput, path
security across both namespaces, bounded read tiers, write safety, and the
no-host-path-leak rule for external mounts."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nodes.tool.data_source import (
    DataSourceNode,
    DataToolInput,
    DataToolParams,
    WS_HANDLERS,
)
from nodes.tool.data_source._paths import resolve_data_path, split_mount_path
from services.plugin import NodeContext, NodeUserError

def _ctx(tmp_path: Path, **overrides) -> NodeContext:
    defaults = dict(
        node_id="data-1",
        node_type="dataSource",
        workflow_id="wf-data-test",
        workspace_dir=str(tmp_path / "workspace"),
    )
    defaults.update(overrides)
    ctx = NodeContext(**defaults)
    Path(ctx.workspace_dir).mkdir(parents=True, exist_ok=True)
    return ctx


HOST_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]|/home/|/Users/")


def _assert_no_host_paths(result: dict, *roots: Path) -> None:
    serialized = json.dumps(result, default=str)
    for root in roots:
        assert str(root) not in serialized
        assert root.as_posix() not in serialized
    # url fields legitimately contain "/api/..."; strip them before the
    # generic drive/home scan.
    scrubbed = re.sub(r"/api/workspace/[^\"]*", "", serialized)
    assert not HOST_PATH_PATTERN.search(scrubbed), scrubbed[:500]


class TestSpecInvariants:
    def test_node_identity(self):
        assert DataSourceNode.type == "dataSource"
        assert DataSourceNode.component_kind == "tool"
        assert DataSourceNode.group == ("tool",)
        assert DataSourceNode.tool_name == "data"
        assert DataSourceNode.tool_schema_locked is True
        assert DataSourceNode.Params is DataToolParams
        assert DataSourceNode.ToolInput is DataToolInput
        assert DataSourceNode.server_controlled_fields == frozenset({"mounts"})

    def test_ui_hints(self):
        hints = DataSourceNode.ui_hints
        assert hints["isToolPanel"] is True
        assert hints["isDataPanel"] is True
        assert hints["hideRunButton"] is True

    def test_handles(self):
        assert DataSourceNode.handles == (
            {
                "name": "output-tool",
                "kind": "output",
                "position": "top",
                "label": "Data",
                "role": "tools",
            },
        )

    def test_llm_schema_hides_config_and_has_no_delete(self):
        schema = DataSourceNode.as_tool_schema()
        assert schema["name"] == "data"
        properties = schema["parameters"]["properties"]
        assert "mounts" not in properties
        operations = properties["operation"]["enum"]
        assert "delete" not in operations
        assert "remove" not in operations
        assert set(operations) == {
            "list",
            "read",
            "search",
            "metadata",
            "write",
            "append",
            "copy_to_workspace",
        }

    def test_operations_registry(self):
        assert set(DataSourceNode._operations) == {"data"}

    def test_ws_handlers_registered(self):
        from services.ws_handler_registry import get_ws_handlers

        assert set(WS_HANDLERS).issubset(set(get_ws_handlers()))


class TestToolInputValidation:
    def test_per_operation_required_fields(self):
        for operation, payload in (
            ("read", {}),
            ("metadata", {}),
            ("write", {"path": "a.txt"}),
            ("append", {"path": "a.txt"}),
            ("search", {}),
            ("copy_to_workspace", {}),
        ):
            with pytest.raises(ValidationError):
                DataToolInput(operation=operation, **payload)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            DataToolInput(operation="list", namespace="sneaky")

    def test_params_coerce_blank_and_json_strings(self):
        assert DataToolParams.model_validate({"mounts": ""}).mounts == []
        assert DataToolParams.model_validate(
            {"mounts": '["docs"]'}
        ).mounts == ["docs"]
        assert DataToolParams.model_validate({}).mounts == []


class TestPathSecurity:
    async def test_traversal_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        for hostile in ("../secrets.txt", "..", "a/../../b", "~/x"):
            with pytest.raises(NodeUserError):
                await resolve_data_path(ctx, [], hostile)

    async def test_drive_prefixed_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        with pytest.raises(NodeUserError):
            await resolve_data_path(ctx, [], "C:/Windows/system32")

    async def test_mnt_prefix_reserved(self, tmp_path):
        ctx = _ctx(tmp_path)
        with pytest.raises(NodeUserError, match="not enabled|needs a name"):
            await resolve_data_path(ctx, [], "mnt/anything/file.txt")

    async def test_mount_traversal_name_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        # 'mnt/../..' parses as mount name '..' which can never be enabled.
        with pytest.raises(NodeUserError):
            await resolve_data_path(ctx, ["docs"], "mnt/../../etc/passwd")

    def test_split_mount_path(self):
        assert split_mount_path("mnt/docs/a/b.txt") == ("docs", "a/b.txt")
        assert split_mount_path("mnt/docs") == ("docs", "")
        assert split_mount_path("reports/q3.csv") is None
        assert split_mount_path("") is None


class TestWorkspaceOps:
    async def test_write_read_text_roundtrip(self, tmp_path):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        write = await node._op_write(
            ctx,
            [],
            DataToolInput(
                operation="write", path="notes/hello.txt", content="line1\nline2"
            ),
        )
        assert write["created"] is True
        assert write["source"] == "workspace"

        read = await node._op_read(
            ctx, [], DataToolInput(operation="read", path="notes/hello.txt")
        )
        assert read["type"] == "text"
        assert read["text"] == "line1\nline2"
        assert read["lines_total"] == 2
        assert read["encoding"] == "utf-8"
        _assert_no_host_paths(read, tmp_path)

    async def test_append(self, tmp_path):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        await node._op_write(
            ctx, [], DataToolInput(operation="write", path="log.txt", content="a")
        )
        result = await node._op_append(
            ctx, [], DataToolInput(operation="append", path="log.txt", content="b")
        )
        assert result["appended"] is True
        assert (Path(ctx.workspace_dir) / "log.txt").read_text() == "ab"

    async def test_csv_tier(self, tmp_path):
        ctx = _ctx(tmp_path)
        (Path(ctx.workspace_dir) / "t.csv").write_text(
            "name,age\nada,36\ngrace,45\n"
        )
        node = DataSourceNode()
        result = await node._op_read(
            ctx, [], DataToolInput(operation="read", path="t.csv")
        )
        assert result["type"] == "csv"
        assert result["columns"] == ["name", "age"]
        assert result["rows"] == [["ada", "36"], ["grace", "45"]]
        assert result["rows_total"] == 2

    async def test_json_tier_prunes_depth(self, tmp_path):
        ctx = _ctx(tmp_path)
        nested: dict = {"leaf": 1}
        for _ in range(12):
            nested = {"child": nested}
        (Path(ctx.workspace_dir) / "deep.json").write_text(json.dumps(nested))
        node = DataSourceNode()
        result = await node._op_read(
            ctx, [], DataToolInput(operation="read", path="deep.json")
        )
        assert result["type"] == "json"
        assert result["pruned_paths"]
        assert result["truncated"] is True

    async def test_image_tier_metadata_only(self, tmp_path):
        from PIL import Image

        ctx = _ctx(tmp_path)
        Image.new("RGB", (12, 8)).save(Path(ctx.workspace_dir) / "pic.png")
        node = DataSourceNode()
        result = await node._op_read(
            ctx, [], DataToolInput(operation="read", path="pic.png")
        )
        assert result["type"] == "image"
        assert result["image"] == {
            "width": 12,
            "height": 8,
            "format": "png",
            "mode": "RGB",
        }
        assert result["ref"]["kind"] == "file"
        serialized = json.dumps(result)
        assert "base64" not in serialized
        _assert_no_host_paths(result, tmp_path)

    async def test_binary_tier_never_bytes(self, tmp_path):
        ctx = _ctx(tmp_path)
        payload = bytes(range(256)) * 64
        (Path(ctx.workspace_dir) / "blob.bin").write_bytes(payload)
        node = DataSourceNode()
        result = await node._op_read(
            ctx, [], DataToolInput(operation="read", path="blob.bin")
        )
        assert result["type"] == "binary"
        assert result["sha256"]
        assert result["size_bytes"] == len(payload)
        assert "content" not in result
        assert len(json.dumps(result)) < 2_000

    async def test_search_workspace(self, tmp_path):
        ctx = _ctx(tmp_path)
        root = Path(ctx.workspace_dir)
        (root / "reports").mkdir()
        (root / "reports" / "q3-summary.txt").write_text("x")
        (root / "other.txt").write_text("y")
        node = DataSourceNode()
        result = await node._op_search(
            ctx, [], DataToolInput(operation="search", pattern="summary")
        )
        assert result["count"] == 1
        assert result["entries"][0]["path"] == "reports/q3-summary.txt"

    async def test_metadata(self, tmp_path):
        ctx = _ctx(tmp_path)
        (Path(ctx.workspace_dir) / "m.txt").write_text("hello")
        node = DataSourceNode()
        result = await node._op_metadata(
            ctx, [], DataToolInput(operation="metadata", path="m.txt")
        )
        assert result["size_bytes"] == 5
        assert result["sha256"]
        assert result["type"] == "text"


@pytest.fixture
async def mount_env(tmp_path, monkeypatch):
    """Real private DB + one read-only and one writable mount."""
    module_name = f"tests._real_data_node_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[2] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".data-node-{uuid.uuid4().hex}.db"
    database = module.Database(
        SimpleNamespace(
            database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
            database_echo=False,
            database_pool_size=5,
            database_max_overflow=5,
        )
    )
    await database.startup()
    monkeypatch.setattr(
        "services.plugin.deps.get_database", lambda: database
    )

    from services.data.mount_store import DataMountStore

    store = DataMountStore(database)
    readonly_dir = tmp_path / "external-ro"
    writable_dir = tmp_path / "external-rw"
    readonly_dir.mkdir()
    writable_dir.mkdir()
    (readonly_dir / "doc.txt").write_text("external content")
    await store.add_mount("owner", name="ro", root_path=str(readonly_dir))
    await store.add_mount(
        "owner", name="rw", root_path=str(writable_dir), writable=True
    )
    try:
        yield SimpleNamespace(
            database=database,
            readonly_dir=readonly_dir,
            writable_dir=writable_dir,
        )
    finally:
        await database.shutdown()
        sys.modules.pop(module_name, None)
        for candidate in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ):
            candidate.unlink(missing_ok=True)


class TestMountOps:
    async def test_read_from_mount_no_host_path_leak(self, tmp_path, mount_env):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        result = await node._op_read(
            ctx,
            ["ro", "rw"],
            DataToolInput(operation="read", path="mnt/ro/doc.txt"),
        )
        assert result["text"] == "external content"
        assert result["source"] == "mount"
        assert result["mount"] == "ro"
        _assert_no_host_paths(result, tmp_path, mount_env.readonly_dir)

    async def test_write_to_readonly_mount_refused(self, tmp_path, mount_env):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        with pytest.raises(NodeUserError, match="read-only"):
            await node._op_write(
                ctx,
                ["ro"],
                DataToolInput(
                    operation="write", path="mnt/ro/new.txt", content="x"
                ),
            )

    async def test_write_to_writable_mount(self, tmp_path, mount_env):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        result = await node._op_write(
            ctx,
            ["rw"],
            DataToolInput(
                operation="write", path="mnt/rw/out.txt", content="hello"
            ),
        )
        assert result["created"] is True
        assert (mount_env.writable_dir / "out.txt").read_text() == "hello"

    async def test_disabled_mount_refused(self, tmp_path, mount_env):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        with pytest.raises(NodeUserError, match="not enabled"):
            await node._op_read(
                ctx,
                ["rw"],  # 'ro' exists globally but is not enabled here
                DataToolInput(operation="read", path="mnt/ro/doc.txt"),
            )

    async def test_revoked_mount_dies_at_call_time(self, tmp_path, mount_env):
        from services.data.mount_store import DataMountStore

        await DataMountStore(mount_env.database).remove_mount(
            "owner", name="ro"
        )
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        with pytest.raises(NodeUserError, match="no longer defined"):
            await node._op_read(
                ctx,
                ["ro"],
                DataToolInput(operation="read", path="mnt/ro/doc.txt"),
            )

    async def test_mount_symlink_escape_rejected(self, tmp_path, mount_env):
        try:
            (mount_env.readonly_dir / "escape").symlink_to(
                tmp_path, target_is_directory=True
            )
        except OSError:
            pytest.skip("symlinks unavailable on this platform")
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        with pytest.raises(NodeUserError):
            await node._op_read(
                ctx,
                ["ro"],
                DataToolInput(
                    operation="read", path="mnt/ro/escape/workspace/x.txt"
                ),
            )

    async def test_copy_to_workspace_never_overwrites(
        self, tmp_path, mount_env
    ):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        args = DataToolInput(
            operation="copy_to_workspace", path="mnt/ro/doc.txt"
        )
        first = await node._op_copy(ctx, ["ro"], args)
        second = await node._op_copy(ctx, ["ro"], args)
        assert first["path"] == "imports/doc.txt"
        assert second["path"] == "imports/doc-1.txt"
        assert first["ref"]["kind"] == "file"
        assert first["copied_from"] == "mnt/ro/doc.txt"
        workspace = Path(ctx.workspace_dir)
        assert (workspace / "imports" / "doc.txt").read_text() == (
            "external content"
        )
        _assert_no_host_paths(first, mount_env.readonly_dir)

    async def test_list_root_reports_mounts(self, tmp_path, mount_env):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        result = await node._op_list(
            ctx, ["ro", "rw"], DataToolInput(operation="list")
        )
        mounts = {row["name"]: row for row in result["mounts"]}
        assert mounts["ro"]["writable"] is False
        assert mounts["rw"]["writable"] is True
        assert mounts["ro"]["available"] is True
        assert mounts["ro"]["path"] == "mnt/ro"

    async def test_list_mount_dir(self, tmp_path, mount_env):
        ctx = _ctx(tmp_path)
        node = DataSourceNode()
        result = await node._op_list(
            ctx, ["ro"], DataToolInput(operation="list", path="mnt/ro")
        )
        assert result["source"] == "mount"
        names = [row["name"] for row in result["entries"]]
        assert "doc.txt" in names
        locations = [row["location"] for row in result["entries"]]
        assert all(loc.startswith("mnt/ro/") for loc in locations)
        _assert_no_host_paths(result, mount_env.readonly_dir)


class TestHandlerSecurity:
    async def test_internal_socket_denied(self):
        internal = SimpleNamespace(
            scope={"path": "/ws/internal"}, state=None
        )
        for name, handler in WS_HANDLERS.items():
            result = await handler({}, internal)
            assert result.get("success") is False, name
            assert "authenticated" in str(result.get("error", "")), name
