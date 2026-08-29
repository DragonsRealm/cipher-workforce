"""Env scrubbing — red-proof test.

Verifies that build_safe_env() never leaks CIPHER_*, ANTHROPIC_*,
STRIPE_*, or GOOGLE_* keys into child process environments.

These tests are intentionally adversarial: they patch os.environ with
toxic keys before calling build_safe_env() and assert zero leakage.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from services.safe_env import SAFE_ENV_KEYS, build_safe_env

# Keys that must NEVER appear in a child process environment.
_BLOCKED_PREFIXES = ("CIPHER_", "ANTHROPIC_", "STRIPE_", "GOOGLE_")


def _toxic_environ() -> dict[str, str]:
    """Return a fake os.environ containing one key per blocked prefix."""
    return {
        "CIPHER_APPROVAL_TOKEN": "super-secret-approval",
        "CIPHER_SESSION_KEY": "session-secret",
        "ANTHROPIC_API_KEY": "sk-ant-abc123",
        "STRIPE_SECRET_KEY": "sk_live_xyz",
        "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "LANG": "en_US.UTF-8",
        "USER": "testuser",
        # A red-herring key that should pass through.
        "TMPDIR": "/tmp",
    }


class TestSafeEnvNeverLeaksBlockedKeys:
    """Core contract: blocked prefixes never appear in build_safe_env output."""

    def test_no_cipher_keys_in_output(self) -> None:
        with patch.dict(os.environ, _toxic_environ(), clear=True):
            result = build_safe_env()
        leaked = [k for k in result if k.startswith("CIPHER_")]
        assert not leaked, f"CIPHER_ keys leaked into child env: {leaked}"

    def test_no_anthropic_keys_in_output(self) -> None:
        with patch.dict(os.environ, _toxic_environ(), clear=True):
            result = build_safe_env()
        leaked = [k for k in result if k.startswith("ANTHROPIC_")]
        assert not leaked, f"ANTHROPIC_ keys leaked into child env: {leaked}"

    def test_no_stripe_keys_in_output(self) -> None:
        with patch.dict(os.environ, _toxic_environ(), clear=True):
            result = build_safe_env()
        leaked = [k for k in result if k.startswith("STRIPE_")]
        assert not leaked, f"STRIPE_ keys leaked into child env: {leaked}"

    def test_no_google_keys_in_output(self) -> None:
        with patch.dict(os.environ, _toxic_environ(), clear=True):
            result = build_safe_env()
        leaked = [k for k in result if k.startswith("GOOGLE_")]
        assert not leaked, f"GOOGLE_ keys leaked into child env: {leaked}"

    def test_all_blocked_prefixes_absent(self) -> None:
        """Aggregate check — all blocked prefixes in one assertion."""
        with patch.dict(os.environ, _toxic_environ(), clear=True):
            result = build_safe_env()
        leaked = [
            k for k in result
            if any(k.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
        ]
        assert not leaked, (
            f"Keys with blocked prefixes leaked into child env: {leaked}"
        )

    def test_approval_token_specifically_absent(self) -> None:
        """CIPHER_APPROVAL_TOKEN must never reach any child process."""
        env = _toxic_environ()
        env["CIPHER_APPROVAL_TOKEN"] = "most-sensitive-key"
        with patch.dict(os.environ, env, clear=True):
            result = build_safe_env()
        assert "CIPHER_APPROVAL_TOKEN" not in result, (
            "CIPHER_APPROVAL_TOKEN must NEVER appear in child env "
            "(belongs to cipherd only — Orion architectural constraint D4)"
        )


class TestSafeEnvAllowsSafeKeys:
    """Safe keys from SAFE_ENV_KEYS pass through correctly."""

    def test_path_passes_through(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "CIPHER_SECRET": "poison"}
        with patch.dict(os.environ, env, clear=True):
            result = build_safe_env()
        assert result.get("PATH") == "/usr/bin:/bin"

    def test_home_passes_through(self) -> None:
        env = {"HOME": "/home/user", "ANTHROPIC_API_KEY": "poison"}
        with patch.dict(os.environ, env, clear=True):
            result = build_safe_env()
        assert result.get("HOME") == "/home/user"

    def test_only_allowlisted_keys_present(self) -> None:
        """Every key in output must be in SAFE_ENV_KEYS or in extra_keys."""
        toxic = _toxic_environ()
        with patch.dict(os.environ, toxic, clear=True):
            result = build_safe_env()
        unexpected = [k for k in result if k not in SAFE_ENV_KEYS]
        assert not unexpected, (
            f"Keys not in SAFE_ENV_KEYS appeared in output: {unexpected}"
        )

    def test_extra_keys_pass_through(self) -> None:
        """Caller-supplied extra_keys are included in the output."""
        env = {"MY_EXTRA_VAR": "allowed", "CIPHER_SECRET": "blocked"}
        with patch.dict(os.environ, env, clear=True):
            result = build_safe_env(extra_keys=frozenset({"MY_EXTRA_VAR"}))
        assert "MY_EXTRA_VAR" in result
        assert "CIPHER_SECRET" not in result

    def test_extra_keys_cannot_smuggle_blocked_prefix(self) -> None:
        """extra_keys is an allowlist; a caller cannot smuggle a blocked key
        by naming it explicitly — the key simply won't exist in os.environ
        unless the operator placed it there, and if they did, the extra_keys
        mechanism lets that through intentionally. This test documents the
        contract: extra_keys allows by NAME, not by prefix. A blocked prefix
        key added to extra_keys WILL pass through if it is in os.environ."""
        # This is the intended behaviour — document it, not forbid it.
        env = {"CIPHER_OVERRIDE": "intentional", "PATH": "/bin"}
        with patch.dict(os.environ, env, clear=True):
            result = build_safe_env(extra_keys=frozenset({"CIPHER_OVERRIDE"}))
        # Intentional: the caller explicitly allowlisted this key.
        assert "CIPHER_OVERRIDE" in result


class TestSafeEnvConstantIsComplete:
    """SAFE_ENV_KEYS has the expected baseline keys."""

    def test_baseline_keys_present(self) -> None:
        required = {"PATH", "HOME", "LANG", "TMPDIR", "USER"}
        missing = required - SAFE_ENV_KEYS
        assert not missing, f"Expected keys missing from SAFE_ENV_KEYS: {missing}"

    def test_blocked_keys_not_in_allowlist(self) -> None:
        """None of the blocked prefixes appear as-is in the static allowlist."""
        for prefix in _BLOCKED_PREFIXES:
            leaked = [k for k in SAFE_ENV_KEYS if k.startswith(prefix)]
            assert not leaked, (
                f"Blocked prefix '{prefix}' found in SAFE_ENV_KEYS: {leaked}"
            )
