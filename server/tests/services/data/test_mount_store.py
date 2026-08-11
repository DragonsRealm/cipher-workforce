"""Validation and CRUD contracts for the machine-wide mount allowlist."""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.data.mount_store import (
    DataMountStore,
    MountStoreError,
    validate_mount_root,
)


@pytest.fixture
async def mount_database():
    # Root conftest stubs core.database for fast unit tests. Load the real
    # implementation privately, matching test_tool_store.py.
    module_name = f"tests._real_data_mount_database_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[3] / "core" / "database.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    db_path = Path.cwd() / f".data-mount-{uuid.uuid4().hex}.db"
    database = module.Database(
        SimpleNamespace(
            database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
            database_echo=False,
            database_pool_size=5,
            database_max_overflow=5,
        )
    )
    await database.startup()
    try:
        yield database
    finally:
        await database.shutdown()
        sys.modules.pop(module_name, None)
        for candidate in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ):
            candidate.unlink(missing_ok=True)


class TestValidateMountRoot:
    def test_relative_path_rejected(self):
        with pytest.raises(MountStoreError, match="absolute"):
            validate_mount_root("relative/folder", writable=False)

    def test_missing_path_rejected(self, tmp_path):
        with pytest.raises(MountStoreError, match="does not exist"):
            validate_mount_root(str(tmp_path / "nope"), writable=False)

    def test_file_rejected(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("x")
        with pytest.raises(MountStoreError, match="directory"):
            validate_mount_root(str(target), writable=False)

    def test_filesystem_root_rejected(self, tmp_path):
        anchor = Path(tmp_path.anchor)
        with pytest.raises(MountStoreError, match="filesystem root"):
            validate_mount_root(str(anchor), writable=False)

    def test_home_directory_rejected(self):
        with pytest.raises(MountStoreError, match="home directory"):
            validate_mount_root(str(Path.home()), writable=False)

    def test_data_dir_overlap_rejected_both_directions(
        self, tmp_path, monkeypatch
    ):
        protected = tmp_path / "container" / "opencompany-data"
        protected.mkdir(parents=True)
        monkeypatch.setattr(
            "core.paths.opencompany_root", lambda: protected
        )
        inner = protected / "workspaces"
        inner.mkdir()
        with pytest.raises(MountStoreError, match="data directory"):
            validate_mount_root(str(inner), writable=False)
        with pytest.raises(MountStoreError, match="data directory"):
            validate_mount_root(str(tmp_path / "container"), writable=False)

    def test_writable_probe(self, tmp_path, monkeypatch):
        target = tmp_path / "readonly"
        target.mkdir()
        real_access = os.access

        def fake_access(path, mode):
            if mode == os.W_OK and Path(path) == target.resolve():
                return False
            return real_access(path, mode)

        monkeypatch.setattr(os, "access", fake_access)
        with pytest.raises(MountStoreError, match="not writable"):
            validate_mount_root(str(target), writable=True)
        # Read-only save of the same folder is fine.
        assert validate_mount_root(str(target), writable=False)

    def test_valid_directory_resolves(self, tmp_path):
        target = tmp_path / "docs"
        target.mkdir()
        assert validate_mount_root(str(target), writable=False) == target.resolve()


class TestMountStoreCrud:
    async def test_crud_roundtrip(self, mount_database, tmp_path):
        store = DataMountStore(mount_database)
        folder = tmp_path / "reports"
        folder.mkdir()

        added = await store.add_mount(
            "owner", name="reports", root_path=str(folder), writable=False
        )
        assert added["name"] == "reports"
        assert added["writable"] is False

        assert [m["name"] for m in await store.list_mounts("owner")] == [
            "reports"
        ]
        assert (await store.get_mount("owner", "reports")) is not None
        # Other owners see nothing.
        assert await store.list_mounts("intruder") == []

        updated = await store.update_mount(
            "owner", name="reports", writable=True
        )
        assert updated["writable"] is True

        removed = await store.remove_mount("owner", name="reports")
        assert removed["removed"] is True
        assert await store.get_mount("owner", "reports") is None

    async def test_duplicate_name_and_root_rejected(
        self, mount_database, tmp_path
    ):
        store = DataMountStore(mount_database)
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        await store.add_mount("owner", name="docs", root_path=str(first))
        with pytest.raises(MountStoreError, match="already exists"):
            await store.add_mount("owner", name="docs", root_path=str(second))
        with pytest.raises(MountStoreError, match="already mounted"):
            await store.add_mount("owner", name="other", root_path=str(first))

    async def test_bad_names_rejected(self, mount_database, tmp_path):
        store = DataMountStore(mount_database)
        folder = tmp_path / "ok"
        folder.mkdir()
        for bad in ("", "Has Spaces", "UPPER", "-leading", "a" * 65, "a/b"):
            with pytest.raises(MountStoreError, match="Mount name"):
                await store.add_mount("owner", name=bad, root_path=str(folder))

    async def test_unknown_mount_operations(self, mount_database):
        store = DataMountStore(mount_database)
        with pytest.raises(MountStoreError, match="No mount"):
            await store.update_mount("owner", name="ghost", writable=True)
        with pytest.raises(MountStoreError, match="No mount"):
            await store.remove_mount("owner", name="ghost")
