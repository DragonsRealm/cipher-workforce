"""Tests for the session-cookie posture guard (core.config).

Locks two contracts:
  1. ``cookie_posture_warnings`` warns -- never raises -- about a risky but
     working configuration. It must stay a warning: ``company deploy``
     intentionally ships ``JWT_COOKIE_SECURE=false`` because the VM is
     reached over plain HTTP on its IP, so failing here would brick every
     LAN/IP deployment with the worst possible symptom (login appears to
     succeed, then immediately logs out).
  2. ``SameSite=None`` without ``Secure`` DOES hard-fail, because no browser
     accepts that cookie -- the app would boot fine and then silently refuse
     to keep anyone signed in.
"""

from types import SimpleNamespace

import pytest

from core.config import cookie_posture_warnings


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        jwt_cookie_secure=True,
        jwt_cookie_samesite="lax",
        deployment_mode="cloud",
        cors_origins=["https://app.example.com"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestInsecureCookieWarning:
    def test_warns_when_insecure_outside_local(self):
        warnings = cookie_posture_warnings(_settings(jwt_cookie_secure=False))
        assert any("JWT_COOKIE_SECURE" in w for w in warnings)

    def test_silent_when_insecure_but_local(self):
        """The documented local-dev posture; nagging here would train
        operators to ignore the banner."""
        warnings = cookie_posture_warnings(
            _settings(jwt_cookie_secure=False, deployment_mode="local")
        )
        assert not any("JWT_COOKIE_SECURE" in w for w in warnings)

    def test_silent_when_secure(self):
        assert cookie_posture_warnings(_settings(jwt_cookie_secure=True)) == []

    @pytest.mark.parametrize("mode", ["cloud", "self_hosted"])
    def test_warns_for_every_non_local_mode(self, mode):
        warnings = cookie_posture_warnings(_settings(jwt_cookie_secure=False, deployment_mode=mode))
        assert warnings


class TestWildcardCorsWarning:
    def test_warns_on_wildcard_origin(self):
        warnings = cookie_posture_warnings(_settings(cors_origins=["*"]))
        assert any("CORS_ORIGINS" in w for w in warnings)

    def test_silent_on_explicit_origins(self):
        assert cookie_posture_warnings(_settings(cors_origins=["https://a", "https://b"])) == []

    def test_handles_empty_and_missing_origins(self):
        assert cookie_posture_warnings(_settings(cors_origins=[])) == []
        assert cookie_posture_warnings(_settings(cors_origins=None)) == []


class TestNeverRaises:
    def test_tolerates_a_sparse_settings_object(self):
        """Duck-typed like `dev_secret_offenders`; a missing attribute must
        not explode during startup."""
        assert cookie_posture_warnings(SimpleNamespace()) == []

    def test_returns_both_warnings_together(self):
        warnings = cookie_posture_warnings(
            _settings(jwt_cookie_secure=False, cors_origins=["*"])
        )
        assert len(warnings) == 2


class TestSameSiteNoneValidator:
    """The one cookie misconfiguration worth failing on.

    Constructed with explicit kwargs and ``_env_file=None``, matching
    ``test_dev_secret_guard.py`` -- `Settings()` has many env-required fields,
    so a bare call picks up the developer's own `.env` and tests nothing.
    """

    @staticmethod
    def _kwargs(**overrides):
        base = {
            "host": "127.0.0.1",
            "port": 3010,
            "jwt_secret_key": "x" * 32,
            "secret_key": "y" * 32,
            "cors_origins": ["http://localhost:3001"],
            "workflow_db_filename": "workflow.db",
            "temporal_enabled": False,
            "temporal_server_address": "localhost:5681",
            "temporal_namespace": "default",
            "temporal_task_queue": "machina-tasks",
            "temporal_per_type_dispatch": True,
            "temporal_agent_workflow_enabled": True,
            "temporal_graceful_shutdown_seconds": 30,
            "temporal_frontend_grpc_port": 7233,
            "temporal_ui_port": 8233,
            "temporal_sqlite_path": "temporal.db",
            "temporal_terminate_running_on_startup": True,
            "api_key_encryption_key": "z" * 32,
        }
        base.update(overrides)
        return base

    def test_samesite_none_without_secure_is_rejected(self):
        from core.config import Settings

        with pytest.raises(Exception) as exc_info:
            Settings(
                _env_file=None,
                **self._kwargs(jwt_cookie_samesite="none", jwt_cookie_secure=False),
            )
        assert "JWT_COOKIE_SAMESITE" in str(exc_info.value)

    def test_samesite_none_with_secure_is_accepted(self):
        from core.config import Settings

        settings = Settings(
            _env_file=None,
            **self._kwargs(jwt_cookie_samesite="none", jwt_cookie_secure=True),
        )
        assert settings.jwt_cookie_samesite == "none"
        assert settings.jwt_cookie_secure is True

    def test_lax_without_secure_is_accepted(self):
        """The shipped default, and what `company deploy` relies on."""
        from core.config import Settings

        settings = Settings(
            _env_file=None,
            **self._kwargs(jwt_cookie_samesite="lax", jwt_cookie_secure=False),
        )
        assert settings.jwt_cookie_samesite == "lax"

    def test_auth_rate_limit_defaults(self):
        """Tighter than the generic rate_limit_* fields on purpose."""
        from core.config import Settings

        settings = Settings(_env_file=None, **self._kwargs())
        assert settings.auth_rate_limit_enabled is True
        assert settings.auth_rate_limit_attempts == 10
        assert settings.auth_rate_limit_window == 300
