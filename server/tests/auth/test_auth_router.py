"""Contract tests for /api/auth/*.

Pins the exact wire shapes the frontend depends on. In particular the 401
body must be `{"detail": "..."}` with a string -- `AuthContext.postAuth`
renders that string directly, and a shape change would silently take the
login page back to showing no error at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests.auth.conftest import make_settings

pytestmark = pytest.mark.unit


def _build_client(user_auth, settings):
    """Bare app with only the auth router, per tests/routers/test_workspace.py."""
    from routers import auth as auth_router

    # The limiters are module-level so a window survives across requests;
    # reset them per client so tests do not inherit each other's counters.
    auth_router._login_limiter = None
    auth_router._register_limiter = None

    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[auth_router.get_user_auth_service] = lambda: user_auth
    app.dependency_overrides[auth_router.get_settings] = lambda: settings
    app.dependency_overrides[auth_router.get_auth_service] = lambda: SimpleNamespace(
        clear_cache=lambda: None
    )

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def client(user_auth, settings):
    return _build_client(user_auth, settings)


REGISTER = {"email": "owner@example.com", "password": "hunter2hunter2", "display_name": "Owner"}
LOGIN = {"email": "owner@example.com", "password": "hunter2hunter2"}


class TestRegisterEndpoint:
    async def test_success_sets_httponly_cookie(self, client, settings):
        async with client as http:
            response = await http.post("/api/auth/register", json=REGISTER)

        assert response.status_code == 200
        assert response.json()["success"] is True
        cookie = response.headers.get("set-cookie", "")
        assert settings.jwt_cookie_name in cookie
        assert "HttpOnly" in cookie

    async def test_duplicate_email_is_400_with_string_detail(self, client):
        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            response = await http.post("/api/auth/register", json=REGISTER)

        assert response.status_code == 400
        assert isinstance(response.json()["detail"], str)

    async def test_overlong_display_name_is_422(self, client):
        async with client as http:
            response = await http.post(
                "/api/auth/register", json={**REGISTER, "display_name": "x" * 101}
            )
        assert response.status_code == 422

    async def test_malformed_email_is_422_with_list_detail(self, client):
        """The shape the UI could not render: `detail` is a list of objects,
        not a string. `extractErrorMessage` joins the `msg` fields."""
        async with client as http:
            response = await http.post("/api/auth/register", json={**REGISTER, "email": "nope"})

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert all("msg" in item for item in detail)


class TestLoginEndpoint:
    async def test_success(self, client, settings):
        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            response = await http.post("/api/auth/login", json=LOGIN)

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["user"]["email"] == "owner@example.com"
        assert "HttpOnly" in response.headers.get("set-cookie", "")

    async def test_bad_password_401_body_is_the_frontend_contract(self, client):
        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            response = await http.post(
                "/api/auth/login", json={**LOGIN, "password": "wrong-password"}
            )

        assert response.status_code == 401
        # Exact shape AND exact text: AuthContext renders `detail` verbatim.
        assert response.json() == {"detail": "Invalid email or password"}

    async def test_unknown_user_401_is_identical(self, client):
        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            unknown = await http.post(
                "/api/auth/login", json={"email": "ghost@example.com", "password": "hunter2hunter2"}
            )
            wrong = await http.post("/api/auth/login", json={**LOGIN, "password": "nope-nope-nope"})

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()


class TestRateLimit:
    async def test_login_trips_after_the_configured_attempts(self, user_auth):
        settings = make_settings(auth_rate_limit_enabled=True, auth_rate_limit_attempts=3)
        client = _build_client(user_auth, settings)

        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            statuses = [
                (await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})).status_code
                for _ in range(4)
            ]

        assert statuses[:3] == [401, 401, 401]
        assert statuses[3] == 429

    async def test_429_carries_retry_after_and_string_detail(self, user_auth):
        settings = make_settings(auth_rate_limit_enabled=True, auth_rate_limit_attempts=1)
        client = _build_client(user_auth, settings)

        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})
            response = await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})

        assert response.status_code == 429
        assert isinstance(response.json()["detail"], str)
        assert int(response.headers["retry-after"]) > 0

    async def test_successful_login_clears_the_counter(self, user_auth):
        """A user who eventually types the right password must not stay
        throttled by their own earlier typos."""
        settings = make_settings(auth_rate_limit_enabled=True, auth_rate_limit_attempts=3)
        client = _build_client(user_auth, settings)

        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})
            await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})
            good = await http.post("/api/auth/login", json=LOGIN)
            # Counter reset, so three more failures are available.
            after = [
                (await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})).status_code
                for _ in range(3)
            ]

        assert good.status_code == 200
        assert after == [401, 401, 401]

    async def test_disabled_by_config(self, user_auth):
        settings = make_settings(auth_rate_limit_enabled=False, auth_rate_limit_attempts=1)
        client = _build_client(user_auth, settings)

        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            statuses = [
                (await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})).status_code
                for _ in range(3)
            ]

        assert statuses == [401, 401, 401]

    async def test_limit_is_per_email(self, user_auth):
        """Keyed on (address, email) so spraying one account does not lock out
        an unrelated one behind the same NAT."""
        settings = make_settings(
            auth_mode="multi", auth_rate_limit_enabled=True, auth_rate_limit_attempts=2
        )
        client = _build_client(user_auth, settings)

        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            for _ in range(2):
                await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})
            blocked = await http.post("/api/auth/login", json={**LOGIN, "password": "wrong"})
            other = await http.post(
                "/api/auth/login", json={"email": "other@example.com", "password": "wrong-pass"}
            )

        assert blocked.status_code == 429
        assert other.status_code == 401


class TestLogoutEndpoint:
    async def test_clears_canonical_and_legacy_cookies(self, client, settings):
        from core.auth_cookies import session_cookie_names

        async with client as http:
            response = await http.post("/api/auth/logout")

        assert response.status_code == 200
        header = response.headers.get_list("set-cookie")
        cleared = " ".join(header)
        for name in session_cookie_names(settings):
            assert name in cleared


class TestStatusEndpoint:
    async def test_reports_registration_open_before_first_user(self, client):
        async with client as http:
            response = await http.get("/api/auth/status")

        assert response.status_code == 200
        assert response.json()["can_register"] is True

    async def test_registration_closes_in_single_mode(self, client):
        async with client as http:
            await http.post("/api/auth/register", json=REGISTER)
            response = await http.get("/api/auth/status")

        assert response.json()["can_register"] is False

    async def test_response_is_json(self, client):
        """AuthContext treats a non-JSON 200 as non-retryable; this pins that
        the real endpoint does return JSON."""
        async with client as http:
            response = await http.get("/api/auth/status")
        assert "application/json" in response.headers["content-type"]
