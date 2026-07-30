"""Contract tests for email nodes: emailSend, emailRead, emailReceive.

These tests freeze the input -> output behaviour documented in
`docs-internal/node-logic-flows/email/`. Email handlers shell out to the
Himalaya CLI via `asyncio.create_subprocess_exec`, so every test uses
`patched_subprocess` (from `_mocks.py`) and stubs `HimalayaService.ensure_binary`
so no real `himalaya` binary or network is required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tests.nodes._mocks import patched_container, patched_pricing, patched_subprocess


pytestmark = pytest.mark.node_contract


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _reset_singletons():
    """Wipe cached singletons so each test gets a clean EmailService / HimalayaService."""
    from nodes.email import _service as email_service
    from nodes.email import _himalaya as himalaya_service

    email_service.EmailService._instance = None
    himalaya_service.HimalayaService._instance = None


# Credentials come from the stored-key store only -- never from node params.
# See TestResolveCredentials in test_email_service.py for why declaring them
# on Params is not an option.
STORED_CREDS = {
    "email_provider": "gmail",
    "email_address": "alice@example.com",
    "email_password": "sekret",
}


def _base_creds_params(**overrides):
    """Node params for an email node.

    Deliberately carries NO credential keys. The previous version injected
    `email`/`password` here *and* every call site also stored them, so the
    tests passed whichever tier the code actually read -- which is how an
    entire dead credential tier survived unnoticed. Keep this params-only.
    """
    params = {"provider": "gmail"}
    params.update(overrides)
    return params


def _patch_ensure_binary():
    """Patch HimalayaService.ensure_binary to return a fake path without shutil.which."""
    return patch(
        "nodes.email._himalaya.HimalayaService.ensure_binary",
        new=AsyncMock(return_value="/usr/bin/himalaya"),
    )


@pytest.fixture(autouse=True)
def _clean_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


# ===========================================================================
# emailSend
# ===========================================================================


class TestEmailSend:
    async def test_happy_path(self, harness):
        stdout = json.dumps({"status": "sent"}).encode()

        with (
            _patch_ensure_binary(),
            patched_subprocess(stdout=stdout, returncode=0) as proc,
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute(
                "emailSend",
                _base_creds_params(
                    to="bob@example.com",
                    subject="hi",
                    body="hello bob",
                    body_type="text",
                ),
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        assert payload["from"] == "alice@example.com"
        assert payload.get("status") == "sent"
        # Exactly one subprocess spawn for a single send
        proc.communicate.assert_awaited()

    async def test_html_body_builds_multipart(self, harness):
        # Return empty JSON so himalaya "succeeds" with no extra fields
        with (
            _patch_ensure_binary(),
            patched_subprocess(stdout=b"{}", returncode=0),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
            patch("nodes.email._himalaya.HimalayaService.execute", new=AsyncMock(return_value={})) as mock_exec,
        ):
            result = await harness.execute(
                "emailSend",
                _base_creds_params(
                    to="bob@example.com",
                    subject="hi",
                    body="<p>hello</p>",
                    body_type="html",
                ),
            )

        harness.assert_envelope(result, success=True)
        # Inspect the MIME stdin passed to himalaya
        call_kwargs = mock_exec.await_args.kwargs
        stdin_data = call_kwargs.get("stdin_data") or mock_exec.await_args.args[-1]
        assert "multipart/alternative" in stdin_data
        assert "text/html" in stdin_data

    async def test_missing_credentials_returns_error(self, harness):
        # No email/password in params AND no stored keys -> ValueError -> envelope
        with _patch_ensure_binary(), patched_subprocess(), patched_container(auth_api_keys={}), patched_pricing():
            result = await harness.execute(
                "emailSend",
                {"to": "bob@example.com", "subject": "s", "body": "b"},
            )

        harness.assert_envelope(result, success=False)
        assert "email" in result["error"].lower() or "password" in result["error"].lower()

    async def test_subprocess_failure_returns_error(self, harness):
        # Non-zero returncode -> HimalayaService raises RuntimeError -> envelope
        with (
            _patch_ensure_binary(),
            patched_subprocess(stdout=b"", stderr=b"auth failed", returncode=1),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute(
                "emailSend",
                _base_creds_params(to="b@x.com", subject="s", body="b"),
            )

        harness.assert_envelope(result, success=False)
        assert "himalaya" in result["error"].lower() or "auth failed" in result["error"].lower()


# ===========================================================================
# emailRead
# ===========================================================================


class TestEmailRead:
    async def test_list_happy_path(self, harness):
        envelopes = [
            {"id": "1", "subject": "first", "from": "a@x.com"},
            {"id": "2", "subject": "second", "from": "b@x.com"},
        ]
        # Himalaya returns a list for envelope list -> wrapped in {data: ...}
        with (
            _patch_ensure_binary(),
            patch("nodes.email._himalaya.HimalayaService.execute", new=AsyncMock(return_value=envelopes)),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute(
                "emailRead",
                _base_creds_params(operation="list", folder="INBOX", page=1, page_size=20),
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        assert payload["operation"] == "list"
        assert payload["folder"] == "INBOX"
        assert payload["data"] == envelopes

    async def test_read_merges_dict_output(self, harness):
        message = {"subject": "hello", "body": "world", "from": "a@x.com"}
        with (
            _patch_ensure_binary(),
            patch("nodes.email._himalaya.HimalayaService.execute", new=AsyncMock(return_value=message)),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute(
                "emailRead",
                _base_creds_params(operation="read", message_id="42", folder="INBOX"),
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        assert payload["operation"] == "read"
        assert payload["subject"] == "hello"
        assert payload["body"] == "world"

    @pytest.mark.parametrize(
        "params,expected_argv",
        [
            (
                {"operation": "search", "query": "from:alice", "folder": "Archive"},
                ["envelope", "list", "-f", "Archive", "--query", "from:alice"],
            ),
            ({"operation": "folders"}, ["folder", "list"]),
            (
                {"operation": "move", "message_id": "42", "target_folder": "Done", "folder": "Archive"},
                ["message", "move", "42", "Done", "-f", "Archive"],
            ),
            (
                {"operation": "delete", "message_id": "42", "folder": "Archive"},
                ["message", "delete", "42", "-f", "Archive"],
            ),
            (
                {"operation": "flag", "message_id": "42", "flag": "Flagged", "flag_action": "remove", "folder": "Archive"},
                ["flag", "remove", "42", "--flag", "Flagged", "-f", "Archive"],
            ),
        ],
        ids=["search", "folders", "move", "delete", "flag"],
    )
    async def test_operation_argv(self, harness, params, expected_argv):
        """The five operations that had no coverage at all.

        Asserting exact argv also pins the non-INBOX `folder` forwarding --
        the UI used to gate `folder` to list/search only, so move/delete/flag
        were silently stuck on INBOX with no way to change the source.
        """
        exec_mock = AsyncMock(return_value={})
        with (
            _patch_ensure_binary(),
            patch("nodes.email._himalaya.HimalayaService.execute", exec_mock),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute("emailRead", _base_creds_params(**params))

        harness.assert_envelope(result, success=True)
        assert exec_mock.await_args.args[2] == expected_argv

    async def test_unknown_operation_returns_error(self, harness):
        # No subprocess should be spawned; router raises ValueError
        with (
            _patch_ensure_binary(),
            patched_subprocess(),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute(
                "emailRead",
                _base_creds_params(operation="nonsense"),
            )

        harness.assert_envelope(result, success=False)
        assert "invalid parameters" in result["error"].lower()

    async def test_subprocess_failure_returns_error(self, harness):
        with (
            _patch_ensure_binary(),
            patched_subprocess(stdout=b"", stderr=b"folder not found", returncode=1),
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
        ):
            result = await harness.execute(
                "emailRead",
                _base_creds_params(operation="list", folder="DoesNotExist"),
            )

        harness.assert_envelope(result, success=False)
        assert "himalaya" in result["error"].lower() or "folder not found" in result["error"].lower()


# ===========================================================================
# emailReceive (polling trigger)
# ===========================================================================


class TestEmailReceive:
    async def test_new_email_detected_on_second_poll(self, harness):
        """Baseline sees {id1}; next poll returns {id1,id2} -> dispatch id2."""
        from nodes.email._service import EmailService

        email_detail = {"from": "c@x.com", "subject": "new!", "body": "hi"}

        # poll_ids: first call returns baseline, second returns baseline+new
        poll_ids_mock = AsyncMock(side_effect=[{"1"}, {"1", "2"}])
        fetch_detail_mock = AsyncMock(
            return_value={
                **email_detail,
                "message_id": "2",
                "folder": "INBOX",
            }
        )

        with (
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
            patch.object(EmailService, "poll_ids", poll_ids_mock),
            patch.object(EmailService, "fetch_detail", fetch_detail_mock),
            patch("asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("services.status_broadcaster.get_status_broadcaster") as bcast,
            patch("services.events.dispatch.emit") as emit_mock,
        ):
            bcast.return_value.update_node_status = AsyncMock(return_value=None)
            emit_mock.return_value = None

            result = await harness.execute(
                "emailReceive",
                _base_creds_params(folder="INBOX", poll_interval=30, mark_as_read=False),
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        assert payload["message_id"] == "2"
        assert payload["folder"] == "INBOX"
        assert payload["subject"] == "new!"
        # Baseline + one diffing poll
        assert poll_ids_mock.await_count == 2
        # Event routed through the canary CloudEvents path (legacy
        # event_waiter.dispatch was retired in Wave 13 — emailReceive
        # is canary-registered and the legacy collector has no consumer).
        emit_mock.assert_called_once()
        envelope = emit_mock.call_args.args[0]
        assert envelope.type == "com.opencompany.email.message.received"
        assert emit_mock.call_args.kwargs["wire_routing_key"] == "email_received"

    async def test_mark_as_read_adds_seen_flag(self, harness):
        from nodes.email._service import EmailService
        from nodes.email._himalaya import HimalayaService

        poll_ids_mock = AsyncMock(side_effect=[set(), {"42"}])
        fetch_detail_mock = AsyncMock(return_value={"message_id": "42", "folder": "INBOX"})
        flag_mock = AsyncMock(return_value={})

        with (
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
            patch.object(EmailService, "poll_ids", poll_ids_mock),
            patch.object(EmailService, "fetch_detail", fetch_detail_mock),
            patch.object(HimalayaService, "flag_message", flag_mock),
            patch("asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("services.status_broadcaster.get_status_broadcaster") as bcast,
            patch("services.events.dispatch.emit"),
        ):
            bcast.return_value.update_node_status = AsyncMock(return_value=None)

            result = await harness.execute(
                "emailReceive",
                _base_creds_params(folder="INBOX", mark_as_read=True),
            )

        harness.assert_envelope(result, success=True)
        flag_mock.assert_awaited_once()
        # Called with message_id="42" and flag="Seen" (from defaults)
        _, kwargs = flag_mock.call_args
        # The handler calls flag_message(creds, msg_id, flag, action, folder) positionally
        args = flag_mock.call_args.args
        assert "42" in args
        assert "Seen" in args

    async def test_missing_credentials_returns_error(self, harness):
        with patched_container(auth_api_keys={}), patched_pricing(), patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            result = await harness.execute(
                "emailReceive",
                {"provider": "gmail", "folder": "INBOX"},
            )

        harness.assert_envelope(result, success=False)
        assert "email" in result["error"].lower() or "password" in result["error"].lower()

    async def test_subprocess_error_surfaces_as_envelope(self, harness):
        """If poll_ids raises (e.g. Himalaya subprocess fails) the handler returns an error envelope."""
        from nodes.email._service import EmailService

        poll_ids_mock = AsyncMock(side_effect=RuntimeError("himalaya error: connection refused"))

        with (
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
            patch.object(EmailService, "poll_ids", poll_ids_mock),
            patch("asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("services.status_broadcaster.get_status_broadcaster") as bcast,
            patch("services.events.dispatch.emit"),
        ):
            bcast.return_value.update_node_status = AsyncMock(return_value=None)

            result = await harness.execute(
                "emailReceive",
                _base_creds_params(folder="INBOX"),
            )

        harness.assert_envelope(result, success=False)
        assert "himalaya" in result["error"].lower() or "connection refused" in result["error"].lower()

    async def test_every_new_message_is_dispatched(self, harness):
        """Two messages arriving in one poll window must produce two events.

        The loop used to take `next(iter(new_ids))` and mark the whole set
        seen, so N-1 events were dropped -- and since `new_ids` is a set,
        *which* one survived was arbitrary. The returned envelope is still
        the first message, by UID order.
        """
        from nodes.email._service import EmailService

        poll_ids_mock = AsyncMock(side_effect=[{"1"}, {"1", "2", "3"}])
        fetch_detail_mock = AsyncMock(
            side_effect=lambda creds, msg_id, folder: {
                "message_id": msg_id,
                "folder": folder,
                "subject": f"msg-{msg_id}",
            }
        )

        with (
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
            patch.object(EmailService, "poll_ids", poll_ids_mock),
            patch.object(EmailService, "fetch_detail", fetch_detail_mock),
            patch("asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("services.status_broadcaster.get_status_broadcaster") as bcast,
            patch("services.events.dispatch.emit") as emit_mock,
        ):
            bcast.return_value.update_node_status = AsyncMock(return_value=None)
            emit_mock.return_value = None

            result = await harness.execute(
                "emailReceive",
                _base_creds_params(folder="INBOX", poll_interval=30),
            )

        harness.assert_envelope(result, success=True)
        assert emit_mock.call_count == 2, "both new messages must be dispatched"
        dispatched = {c.args[0].subject for c in emit_mock.call_args_list}
        assert dispatched == {"2", "3"}
        # Lowest UID first, so the envelope is deterministic.
        assert result["result"]["message_id"] == "2"

    async def test_transient_poll_error_does_not_end_the_run(self, harness):
        """One failing cycle must be retried on the next tick.

        Previously any exception inside the loop propagated to the outer
        handler and ended the interactive Run entirely.
        """
        from nodes.email._service import EmailService

        poll_ids_mock = AsyncMock(
            side_effect=[
                {"1"},                                  # baseline
                RuntimeError("temporary IMAP failure"),  # cycle 1 blows up
                {"1", "2"},                              # cycle 2 recovers
            ]
        )
        fetch_detail_mock = AsyncMock(
            return_value={"message_id": "2", "folder": "INBOX", "subject": "after recovery"}
        )

        with (
            patched_container(auth_api_keys=STORED_CREDS),
            patched_pricing(),
            patch.object(EmailService, "poll_ids", poll_ids_mock),
            patch.object(EmailService, "fetch_detail", fetch_detail_mock),
            patch("asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("services.status_broadcaster.get_status_broadcaster") as bcast,
            patch("services.events.dispatch.emit"),
        ):
            bcast.return_value.update_node_status = AsyncMock(return_value=None)

            result = await harness.execute(
                "emailReceive",
                _base_creds_params(folder="INBOX"),
            )

        harness.assert_envelope(result, success=True)
        assert result["result"]["subject"] == "after recovery"
        assert poll_ids_mock.await_count == 3
