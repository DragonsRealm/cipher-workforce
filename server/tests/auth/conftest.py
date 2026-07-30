"""Fixtures for the user-authentication test suite.

`server/tests/conftest.py` stubs `core` and `core.*` with MagicMocks for the
LLM provider tests. This suite needs the real modules -- `core.config`,
`core.database`, `core.rate_limit` -- so it wipes those entries the same way
`tests/credentials/conftest.py` does.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
import pytest_asyncio

SERVER_DIR = Path(__file__).resolve().parents[2]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# `core.credentials_database` declares SQLModel tables at import time, and
# SQLModel's metadata is process-global and NOT wiped here -- so re-importing
# it raises "Table 'credentials_metadata' is already defined". Preserve it;
# nothing in this suite needs a fresh copy (UserAuthService only holds the
# reference and never calls it on any path under test).
_PRESERVE = {"core.credentials_database"}

for mod_name in [
    name
    for name in list(sys.modules)
    if (name == "core" or name.startswith("core.")) and name not in _PRESERVE
]:
    del sys.modules[mod_name]

from core.database import Database  # noqa: E402

# Must be imported before `Database.startup()` runs `SQLModel.metadata.create_all`
# -- the metadata only knows about model classes that have been imported, so
# without this the `users` table is created only if some other import happens
# to pull it in first, making the suite order-dependent.
from models.auth import User  # noqa: E402,F401

pytestmark = pytest.mark.unit


def make_settings(**overrides) -> SimpleNamespace:
    """Minimal Settings stand-in for UserAuthService.

    A SimpleNamespace rather than a real `Settings()`, which would demand the
    full env surface (ports, CORS origins, secrets) for tests that only touch
    five fields.
    """
    base = dict(
        auth_mode="single",
        jwt_secret_key="test-jwt-secret-key-at-least-32-characters-long",
        jwt_expire_minutes=60,
        jwt_cookie_name="opencompany_token",
        jwt_cookie_secure=False,
        jwt_cookie_samesite="lax",
        auth_rate_limit_enabled=False,
        auth_rate_limit_attempts=10,
        auth_rate_limit_window=300,
        vite_auth_enabled="true",
        deployment_mode="local",
        cors_origins=["http://localhost"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    """Real SQLite Database on a temp file, migrated and ready.

    `Database` reads its URL off a settings object, so this passes the four
    attributes `startup()` actually touches rather than a full `Settings()`.
    """
    db_settings = SimpleNamespace(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'workflow.db'}",
        database_echo=False,
        database_pool_size=5,
        database_max_overflow=10,
    )
    db = Database(db_settings)
    await db.startup()
    try:
        yield db
    finally:
        await db.shutdown()


@pytest.fixture
def settings() -> SimpleNamespace:
    return make_settings()


@pytest_asyncio.fixture
async def user_auth(database: Database, settings: SimpleNamespace):
    """UserAuthService backed by a real database.

    Encryption and the credentials DB are stubs: `UserAuthService` stores the
    references but never touches them on any path under test (its `logout()`
    is a no-op, and the encryption key is server-scoped).
    """
    from services.user_auth import UserAuthService

    return UserAuthService(
        database=database,
        settings=settings,
        encryption=SimpleNamespace(is_initialized=lambda: True),
        credentials_db=SimpleNamespace(),
    )
