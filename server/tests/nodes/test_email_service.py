"""Unit tests for the email plugin's service layer.

These exercise `EmailService` / `HimalayaService` directly, with no
NodeExecutor in the loop -- hence `unit` rather than the `node_contract`
marker carried by `test_email.py`.

Most of this file exists because of a specific class of bug: the email code
had a documented credential-precedence tier that was dead, a `custom` provider
whose ports could not be configured, and a config generator that interpolated
passwords into TOML unescaped -- and not one of those produced a failing test,
because every existing test supplied node params *and* stored keys together
and never inspected the generated config. The assertions below are written to
fail if any of those regress.
"""

from __future__ import annotations

import asyncio
import tomllib
from unittest.mock import AsyncMock, patch

import pytest

from tests.nodes._mocks import patched_container

pytestmark = pytest.mark.unit


def _reset_singletons():
    from nodes.email import _himalaya as himalaya_service
    from nodes.email import _service as email_service

    email_service.EmailService._instance = None
    himalaya_service.HimalayaService._instance = None


@pytest.fixture(autouse=True)
def _clean_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


def _service():
    from nodes.email._service import get_email_service

    return get_email_service()


def _himalaya():
    from nodes.email._himalaya import get_himalaya_service

    return get_himalaya_service()


# The full set of keys `_generate_config` is allowed to emit. An injection
# that smuggles in an extra key shows up here as a set difference, which is
# the real guard -- a substring assertion would miss `backend.host` being
# overridden by a second occurrence.
_EXPECTED_CONFIG_KEYS = {
    "email",
    "display-name",
    "backend.type",
    "backend.host",
    "backend.port",
    "backend.encryption",
    "backend.login",
    "backend.auth.type",
    "backend.auth.raw",
    "message.send.backend.type",
    "message.send.backend.host",
    "message.send.backend.port",
    "message.send.backend.encryption",
    "message.send.backend.login",
    "message.send.backend.auth.type",
    "message.send.backend.auth.raw",
}


def _flatten(data: dict, prefix: str = "") -> dict:
    """Flatten nested TOML tables back into dotted keys."""
    flat = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _render(**creds):
    """Render a config and return (parsed_account_table, raw_text)."""
    base = {
        "email": "alice@example.com",
        "password": "sekret",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_encryption": "tls",
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_encryption": "tls",
    }
    base.update(creds)
    svc = _himalaya()
    raw = svc._generate_config(svc._account_name(base), base)
    parsed = tomllib.loads(raw)
    account = next(iter(parsed["accounts"].values()))
    return _flatten(account), raw


class TestGenerateConfigEscaping:
    """A `"` in any credential used to break the TOML; a `"` plus a newline
    injected arbitrary himalaya keys -- including a `backend.host` pointing
    somewhere else entirely."""

    @pytest.mark.parametrize(
        "password",
        [
            'has"quote',
            "has\\backslash",
            'quote"and\nnewline',
            'evil"\nbackend.host = "attacker.example.com',
            "tab\there",
            "null\x00byte",
        ],
    )
    def test_password_survives_round_trip(self, password):
        flat, _ = _render(password=password)
        assert flat["backend.auth.raw"] == password
        assert flat["message.send.backend.auth.raw"] == password

    def test_injection_cannot_add_keys(self):
        flat, _ = _render(password='x"\nbackend.host = "attacker.example.com')
        assert set(flat) <= _EXPECTED_CONFIG_KEYS
        # The real host must survive unchanged.
        assert flat["backend.host"] == "imap.example.com"

    def test_host_injection_cannot_add_keys(self):
        flat, _ = _render(imap_host='imap.example.com"\nbackend.auth.raw = "leaked')
        assert set(flat) <= _EXPECTED_CONFIG_KEYS
        assert flat["backend.auth.raw"] == "sekret"

    def test_display_name_escaped(self):
        flat, _ = _render(display_name='Alice "The Boss"')
        assert flat["display-name"] == 'Alice "The Boss"'

    def test_display_name_omitted_when_blank(self):
        flat, _ = _render(display_name="")
        assert "display-name" not in flat


class TestGenerateConfigPorts:
    """`resolve_credentials` always emits the port keys, sometimes as None.
    `dict.get(key, default)` finds the key present and returns None, which
    rendered `backend.port = None` -- unparseable, since ports are unquoted.

    This was masked only by the `custom` preset hardcoding 993/465. Blanking
    those (so self-hosted servers can set their own) removes the mask, which
    is why these cases matter.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, 993),
            ("", 993),
            ("993", 993),
            (1143, 1143),
            ("1143", 1143),
            (0, 993),
            (70000, 993),
            (-1, 993),
            ("abc", 993),
            (True, 1),  # bool is an int subclass; 1 is a legal port
        ],
    )
    def test_imap_port_coercion(self, value, expected):
        flat, _ = _render(imap_port=value)
        assert flat["backend.port"] == expected
        assert isinstance(flat["backend.port"], int)

    def test_smtp_port_defaults_independently(self):
        flat, _ = _render(smtp_port=None)
        assert flat["message.send.backend.port"] == 465

    def test_none_port_still_parses(self):
        """The regression in one line: this raised a TOMLDecodeError before."""
        _, raw = _render(imap_port=None, smtp_port=None)
        tomllib.loads(raw)


class TestGenerateConfigEncryption:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "tls"),
            ("", "tls"),
            ("tls", "tls"),
            ("start-tls", "start-tls"),
            ("none", "none"),
            ("TLS", "tls"),
            ("  StartTLS  ".replace("StartTLS", "start-tls"), "start-tls"),
            ("garbage", "tls"),
            ('injected"\nx = "y', "tls"),
        ],
    )
    def test_encryption_allowlist(self, value, expected):
        flat, _ = _render(imap_encryption=value)
        assert flat["backend.encryption"] == expected


class TestAccountName:
    @pytest.mark.parametrize(
        "email,expected",
        [
            ("jane.doe+bot@example.com", "jane_doe_bot"),
            ("alice@example.com", "alice"),
            ("o'brien@example.com", "o_brien"),
            ("a!#$%&'*@example.com", "a_______"),
            ("-dash@example.com", "dash"),
            ("@example.com", "default"),
            ("", "default"),
        ],
    )
    def test_account_name_is_a_valid_bare_key(self, email, expected):
        svc = _himalaya()
        name = svc._account_name({"email": email})
        assert name == expected
        # Must be usable as a TOML bare key AND as an argv value that clap
        # will not read as a flag.
        assert not name.startswith("-")
        tomllib.loads(f"[accounts.{name}]\nemail = \"x@y.z\"")

    def test_missing_email_key(self):
        assert _himalaya()._account_name({}) == "default"


class TestScrub:
    def test_password_redacted(self):
        from nodes.email._himalaya import _scrub

        text = "config error at /tmp/x.toml: backend.auth.raw = \"sekret\""
        assert "sekret" not in _scrub(text, "sekret")
        assert "[REDACTED]" in _scrub(text, "sekret")

    def test_empty_secret_is_noop(self):
        from nodes.email._himalaya import _scrub

        assert _scrub("unchanged", "") == "unchanged"


class TestExecuteTimeout:
    """`asyncio.wait_for` cancels the wrapper, not the child. The subprocess
    stayed alive holding the temp config open, and the `finally` unlink then
    raised PermissionError on Windows -- masking the timeout entirely."""

    async def test_timeout_kills_child_and_removes_config(self, tmp_path):
        from pathlib import Path

        captured = {}

        class _HangingProc:
            pid = 4242
            returncode = None

            async def communicate(self, input=None):
                await asyncio.sleep(3600)

            async def wait(self):
                captured["waited"] = True
                return -9

        async def _fake_exec(*args, **kwargs):
            captured["config_path"] = Path(args[2])
            captured["config_existed"] = captured["config_path"].exists()
            return _HangingProc()

        svc = _himalaya()
        with (
            patch.object(
                type(svc), "ensure_binary", new=AsyncMock(return_value="/usr/bin/himalaya")
            ),
            patch.object(type(svc), "_timeout_seconds", return_value=0.05),
            patch("asyncio.create_subprocess_exec", new=_fake_exec),
            patch("services.events.cli.kill_tree") as kill_tree,
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                await svc.execute("alice", {"email": "a@b.c", "password": "p"}, ["folder", "list"])

        assert captured["config_existed"] is True
        assert kill_tree.called, "the orphaned child must be tree-killed"
        assert kill_tree.call_args[0][0] == 4242
        assert captured.get("waited") is True, "the killed child must be reaped"
        assert not captured["config_path"].exists(), "temp config must be unlinked"


class TestResolveCredentials:
    """The precedence tests that did not exist -- which is exactly why a whole
    documented tier could be dead code without a red test."""

    async def test_stored_keys_only(self):
        with patched_container(
            auth_api_keys={
                "email_provider": "gmail",
                "email_address": "alice@example.com",
                "email_password": "sekret",
            }
        ):
            creds = await _service().resolve_credentials({})

        assert creds["email"] == "alice@example.com"
        assert creds["password"] == "sekret"
        assert creds["imap_host"] == "imap.gmail.com"
        assert creds["smtp_host"] == "smtp.gmail.com"

    async def test_credential_params_are_ignored(self):
        """Node parameters must never carry credentials.

        They cannot be declared on Params without also handing the LLM a
        `password` argument, since `as_tool_schema` dumps the model schema
        wholesale with no field-exclusion hook.
        """
        with patched_container(
            auth_api_keys={
                "email_provider": "gmail",
                "email_address": "stored@example.com",
                "email_password": "stored-pass",
            }
        ):
            creds = await _service().resolve_credentials(
                {
                    "email": "attacker@evil.com",
                    "password": "attacker-pass",
                    "imap_host": "imap.evil.com",
                    "smtp_host": "smtp.evil.com",
                }
            )

        assert creds["email"] == "stored@example.com"
        assert creds["password"] == "stored-pass"
        assert creds["imap_host"] == "imap.gmail.com"
        assert creds["smtp_host"] == "smtp.gmail.com"

    async def test_custom_provider_uses_stored_server_settings(self):
        """The whole point of blanking the `custom` preset: a self-hosted
        server on a non-standard port used to be unconfigurable, because the
        preset's 993/465/tls won the `or` chain."""
        with patched_container(
            auth_api_keys={
                "email_provider": "custom",
                "email_address": "alice@self.host",
                "email_password": "sekret",
                "email_imap_host": "mail.self.host",
                "email_imap_port": "1143",
                "email_imap_encryption": "start-tls",
                "email_smtp_host": "mail.self.host",
                "email_smtp_port": "1587",
                "email_smtp_encryption": "start-tls",
            }
        ):
            creds = await _service().resolve_credentials({})

        assert creds["imap_host"] == "mail.self.host"
        assert creds["imap_port"] == 1143
        assert creds["imap_encryption"] == "start-tls"
        assert creds["smtp_port"] == 1587
        assert creds["smtp_encryption"] == "start-tls"

    async def test_custom_ports_reach_the_generated_config(self):
        """End-to-end for the A1/A5 ordering hazard."""
        with patched_container(
            auth_api_keys={
                "email_provider": "custom",
                "email_address": "alice@self.host",
                "email_password": "sekret",
                "email_imap_host": "mail.self.host",
                "email_imap_port": "1143",
                "email_smtp_host": "mail.self.host",
                "email_smtp_port": "1587",
            }
        ):
            creds = await _service().resolve_credentials({})

        svc = _himalaya()
        flat = _flatten(
            tomllib.loads(svc._generate_config("alice", creds))["accounts"]["alice"]
        )
        assert flat["backend.port"] == 1143
        assert flat["message.send.backend.port"] == 1587
        # Encryption was never written by the panel; the code default applies.
        assert flat["backend.encryption"] == "tls"

    async def test_display_name_from_stored_key(self):
        with patched_container(
            auth_api_keys={
                "email_provider": "gmail",
                "email_address": "alice@example.com",
                "email_password": "sekret",
                "email_display_name": "Alice Example",
            }
        ):
            creds = await _service().resolve_credentials({})
        assert creds["display_name"] == "Alice Example"

    async def test_missing_email_raises(self):
        with patched_container(auth_api_keys={"email_password": "sekret"}):
            with pytest.raises(ValueError, match="Email address not configured"):
                await _service().resolve_credentials({})

    async def test_missing_password_raises(self):
        with patched_container(auth_api_keys={"email_address": "a@b.c"}):
            with pytest.raises(ValueError, match="Email password not configured"):
                await _service().resolve_credentials({})

    @pytest.mark.parametrize(
        "provider", ["gmail", "outlook", "yahoo", "icloud", "protonmail", "fastmail"]
    )
    async def test_every_preset_resolves(self, provider):
        """Six of seven providers had no coverage at all."""
        import json
        from pathlib import Path

        from nodes.email._service import _CONFIG_PATH

        expected = json.loads(Path(_CONFIG_PATH).read_text())["providers"][provider]

        with patched_container(
            auth_api_keys={
                "email_provider": provider,
                "email_address": "alice@example.com",
                "email_password": "sekret",
            }
        ):
            creds = await _service().resolve_credentials({})

        assert creds["imap_host"] == expected["imap_host"]
        assert creds["imap_port"] == expected["imap_port"]
        assert creds["smtp_host"] == expected["smtp_host"]
        assert creds["smtp_port"] == expected["smtp_port"]


class TestResolvePollParams:
    """`emailReceive.execute()` passes raw params, so the Pydantic
    `ge=30, le=3600` guard never runs here."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, 60),
            ("", 60),
            ("abc", 60),
            (5, 30),
            (30, 30),
            (120, 120),
            ("120", 120),
            (99999, 3600),
            (-10, 30),
        ],
    )
    def test_interval_clamped(self, value, expected):
        assert _service().resolve_poll_params({"poll_interval": value})["interval"] == expected

    def test_absent_interval_uses_config_default(self):
        assert _service().resolve_poll_params({})["interval"] == 60

    def test_explicit_none_does_not_raise(self):
        """This raised `TypeError: '<' not supported between NoneType and int`."""
        assert _service().resolve_poll_params({"poll_interval": None, "folder": None})

    def test_folder_falls_back_when_none(self):
        assert _service().resolve_poll_params({"folder": None})["folder"] == "INBOX"

    def test_mark_as_read_coerced_to_bool(self):
        assert _service().resolve_poll_params({"mark_as_read": None})["mark_as_read"] is False
        assert _service().resolve_poll_params({"mark_as_read": "yes"})["mark_as_read"] is True


class TestBuildFilter:
    def _filter(self, **params):
        from nodes.email._filters import build_filter

        return build_filter(params)

    def test_folder_match(self):
        assert self._filter(folder="INBOX")({"folder": "INBOX"}) is True
        assert self._filter(folder="INBOX")({"folder": "Archive"}) is False

    def test_all_is_a_wildcard(self):
        assert self._filter(folder="all")({"folder": "Whatever"}) is True

    def test_default_folder_when_absent(self):
        assert self._filter()({"folder": "INBOX"}) is True
        assert self._filter()({"folder": "Spam"}) is False

    def test_filter_query_matches_subject(self):
        f = self._filter(folder="INBOX", filter_query="invoice")
        assert f({"folder": "INBOX", "subject": "Your Invoice is ready"}) is True
        assert f({"folder": "INBOX", "subject": "Lunch?"}) is False

    def test_filter_query_is_case_insensitive_across_fields(self):
        f = self._filter(folder="INBOX", filter_query="ALICE")
        assert f({"folder": "INBOX", "from": "alice@example.com"}) is True
        assert f({"folder": "INBOX", "body": "regards, Alice"}) is True

    def test_filter_query_tolerates_missing_fields(self):
        f = self._filter(folder="INBOX", filter_query="x")
        assert f({"folder": "INBOX"}) is False
        assert f({"folder": "INBOX", "subject": None, "body": "box"}) is True

    def test_blank_filter_query_is_inert(self):
        f = self._filter(folder="INBOX", filter_query="   ")
        assert f({"folder": "INBOX", "subject": "anything"}) is True
