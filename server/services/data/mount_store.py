"""Machine-wide external-folder allowlist backing the Data node.

This module deliberately owns its table instead of adding mount-specific
methods to :mod:`core.database` — the same shape as
:mod:`services.memory.tool_store`. Importing the Data plugin registers the
SQLModel table before ``Database.startup()`` calls ``create_all``;
:meth:`DataMountStore.ensure_schema` covers standalone workers/tests that
import later.

A mount row grants every Data node on this machine *visibility* of one
absolute directory; each node then exposes a subset by name. Validation at
save time is the security boundary that keeps the allowlist from covering
locations the platform itself depends on (``DATA_DIR`` holds
``credentials.db``, ``workflow.db`` and every workspace), so the checks
here are load-bearing, not cosmetic.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Field, SQLModel, select

from core.logging import get_logger

logger = get_logger(__name__)

# Mount names appear inside ``mnt/<name>/...`` virtual paths the LLM sees
# and round-trips, so they are constrained to a URL/path-safe shape.
MOUNT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataMount(SQLModel, table=True):
    """One operator-approved external directory."""

    __tablename__ = "data_mounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(default="owner", index=True, max_length=255)
    name: str = Field(index=True, max_length=64)
    root_path: str = Field(max_length=1024)
    writable: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class MountStoreError(ValueError):
    """User-correctable mount configuration failure."""


def _protected_roots() -> list[Path]:
    """Directories the allowlist must never cover (either direction)."""
    from core.paths import opencompany_root

    roots = [Path(opencompany_root()).resolve()]
    # A pre-rebrand data dir may still hold live databases.
    legacy = Path.home() / ".machina"
    if legacy.exists():
        roots.append(legacy.resolve())
    return roots


def _overlaps(candidate: Path, protected: Path) -> bool:
    return candidate.is_relative_to(protected) or protected.is_relative_to(
        candidate
    )


def validate_mount_root(root_path: str, *, writable: bool) -> Path:
    """Validate and canonicalize a mount root. Raises :class:`MountStoreError`.

    Filesystem probes run in the caller's thread; callers on the event loop
    wrap this in ``asyncio.to_thread``.
    """
    raw = Path(str(root_path or "").strip())
    if not str(raw):
        raise MountStoreError("Mount path required")
    if not raw.is_absolute():
        raise MountStoreError("Mount path must be absolute")
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError:
        raise MountStoreError(f"Mount path does not exist: {raw}") from None
    except OSError as exc:
        raise MountStoreError(f"Mount path is not usable: {exc}") from exc
    if not resolved.is_dir():
        raise MountStoreError("Mount path must be a directory")
    # A filesystem/drive root or the home directory itself is too broad to
    # be an allowlist entry — it stops being a list at that point.
    if resolved == Path(resolved.anchor):
        raise MountStoreError("Refusing to mount a filesystem root")
    if resolved == Path.home().resolve():
        raise MountStoreError(
            "Refusing to mount the home directory itself; mount a subfolder"
        )
    for protected in _protected_roots():
        if _overlaps(resolved, protected):
            raise MountStoreError(
                "Refusing to mount the OpenCompany data directory or a "
                "path containing it (credential and workflow storage)"
            )
    if not os.access(resolved, os.R_OK):
        raise MountStoreError("Mount path is not readable by the server")
    if writable and not os.access(resolved, os.W_OK):
        raise MountStoreError(
            "Mount path is not writable; save it read-only instead"
        )
    return resolved


def _serialize(mount: DataMount) -> dict[str, Any]:
    # root_path is shown back to the operator in the panel (they typed it).
    # It must never be copied into node results — the node layer emits only
    # ``mnt/<name>/...`` virtual paths.
    return {
        "name": mount.name,
        "root_path": mount.root_path,
        "writable": bool(mount.writable),
        "created_at": mount.created_at.isoformat()
        if mount.created_at
        else None,
        "updated_at": mount.updated_at.isoformat()
        if mount.updated_at
        else None,
    }


class DataMountStore:
    """CRUD for the machine-wide mount allowlist."""

    _schema_lock = asyncio.Lock()
    _initialized_engines: set[Any] = set()

    def __init__(self, database: Any) -> None:
        self.database = database

    async def ensure_schema(self) -> None:
        engine = getattr(self.database, "engine", None)
        if engine is None:
            raise RuntimeError("Database is not initialized")
        if engine in self._initialized_engines:
            return
        async with self._schema_lock:
            if engine in self._initialized_engines:
                return
            async with engine.begin() as connection:
                await connection.run_sync(
                    lambda sync_connection: DataMount.__table__.create(
                        sync_connection, checkfirst=True
                    )
                )
            self._initialized_engines.add(engine)

    @staticmethod
    def _validate_name(name: str) -> str:
        candidate = str(name or "").strip()
        if not MOUNT_NAME_PATTERN.fullmatch(candidate):
            raise MountStoreError(
                "Mount name must be 1-64 chars of lowercase letters, "
                "digits, '-' or '_', starting with a letter or digit"
            )
        return candidate

    async def list_mounts(self, owner_id: str) -> list[dict[str, Any]]:
        await self.ensure_schema()
        async with self.database.get_session() as session:
            result = await session.execute(
                select(DataMount)
                .where(DataMount.user_id == owner_id)
                .order_by(DataMount.name)
            )
            return [_serialize(row) for row in result.scalars().all()]

    async def get_mount(
        self, owner_id: str, name: str
    ) -> Optional[dict[str, Any]]:
        await self.ensure_schema()
        async with self.database.get_session() as session:
            row = await self._row(session, owner_id, name)
            return _serialize(row) if row else None

    async def add_mount(
        self,
        owner_id: str,
        *,
        name: str,
        root_path: str,
        writable: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_schema()
        candidate = self._validate_name(name)
        resolved = await asyncio.to_thread(
            validate_mount_root, root_path, writable=bool(writable)
        )
        async with self.database.get_session() as session:
            existing = await session.execute(
                select(DataMount).where(DataMount.user_id == owner_id)
            )
            resolved_key = os.path.normcase(str(resolved))
            for row in existing.scalars().all():
                if row.name == candidate:
                    raise MountStoreError(
                        f"A mount named '{candidate}' already exists"
                    )
                if os.path.normcase(row.root_path) == resolved_key:
                    raise MountStoreError(
                        f"That folder is already mounted as '{row.name}'"
                    )
            mount = DataMount(
                user_id=owner_id,
                name=candidate,
                root_path=str(resolved),
                writable=bool(writable),
            )
            session.add(mount)
            await session.commit()
            await session.refresh(mount)
            logger.info(
                "Data mount added",
                mount=candidate,
                writable=bool(writable),
            )
            return _serialize(mount)

    async def update_mount(
        self, owner_id: str, *, name: str, writable: bool
    ) -> dict[str, Any]:
        """Only the writable flag is mutable; path changes are remove+add."""
        await self.ensure_schema()
        async with self.database.get_session() as session:
            row = await self._row(session, owner_id, name)
            if row is None:
                raise MountStoreError(f"No mount named '{name}'")
            if writable:
                await asyncio.to_thread(
                    validate_mount_root, row.root_path, writable=True
                )
            row.writable = bool(writable)
            row.updated_at = _utcnow()
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _serialize(row)

    async def remove_mount(self, owner_id: str, *, name: str) -> dict[str, Any]:
        await self.ensure_schema()
        async with self.database.get_session() as session:
            row = await self._row(session, owner_id, name)
            if row is None:
                raise MountStoreError(f"No mount named '{name}'")
            await session.delete(row)
            await session.commit()
            logger.info("Data mount removed", mount=name)
            return {"removed": True, "name": name}

    @staticmethod
    async def _row(
        session: Any, owner_id: str, name: str
    ) -> Optional[DataMount]:
        result = await session.execute(
            select(DataMount).where(
                DataMount.user_id == owner_id,
                DataMount.name == str(name or "").strip(),
            )
        )
        return result.scalars().first()


__all__ = [
    "DataMount",
    "DataMountStore",
    "MOUNT_NAME_PATTERN",
    "MountStoreError",
    "validate_mount_root",
]
