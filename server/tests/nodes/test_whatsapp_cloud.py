"""Contract for the official Meta WhatsApp Cloud API plugin.

The security-relevant test here is ``TestModelCannotChooseTheSendingNumber``.
On a dual-purpose ActionNode the split-schema machinery does not apply --
``BaseNode.execute_as_tool`` merges ``{**node_params, **tool_args}`` for
anything that is not a ToolNode -- so the only way to keep a field away from
the model is to keep it out of Params entirely. This asserts that holds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nodes.whatsapp_cloud._base import classify_error, normalize_recipient
from nodes.whatsapp_cloud.whatsapp_cloud_send import (
    WhatsAppCloudSendNode,
    WhatsAppCloudSendParams,
)
from services.plugin import NodeUserError


pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ctx(**raw):
    return SimpleNamespace(
        node_id="wac-1",
        node_type="whatsappCloudSend",
        workflow_id="wf-1",
        session_id="default",
        user_id="owner",
        workspace_dir=None,
        outputs={},
        nodes=[],
        edges=[],
        raw=dict(raw),
    )


class TestParamsShape:
    def test_phone_number_id_is_not_a_parameter(self):
        """It selects the business identity a message is sent FROM.

        Declaring it would make it model-settable on this node kind, so it is
        credential-sourced instead. If someone adds it back, this fails.
        """
        assert "phone_number_id" not in WhatsAppCloudSendParams.model_fields

    def test_tool_schema_never_exposes_the_sending_number(self):
        schema = WhatsAppCloudSendParams.model_json_schema()
        assert "phone_number_id" not in schema.get("properties", {})

    def test_tool_facing_schema_is_flat(self):
        """LLM function-calling rejects $ref; the contract test enforces this
        repo-wide, asserted here too so the reason is local."""
        import json

        schema = WhatsAppCloudSendParams.model_json_schema()
        assert "$defs" not in schema
        assert "$ref" not in json.dumps(schema)


class TestModelCannotChooseTheSendingNumber:
    def test_tool_args_cannot_override_the_credential_number(self):
        """Drive the real tool path, not the schema.

        A prompt injection in an inbound WhatsApp message is the realistic
        source of hostile tool arguments, and sending as another tenant's
        number is the worst outcome available here.
        """
        node = WhatsAppCloudSendNode()
        captured = {}

        async def _fake_post(ctx, path, body=None, **kwargs):
            captured["path"] = path
            captured["body"] = body
            return {
                "messages": [{"id": "wamid.TEST"}],
                "contacts": [{"wa_id": "14155551234"}],
            }

        with (
            patch("nodes.whatsapp_cloud.whatsapp_cloud_send.graph_post", new=_fake_post),
            patch(
                "services.plugin.deps.get_auth_service",
                return_value=SimpleNamespace(get_api_key=AsyncMock(return_value="OPERATOR_NUMBER")),
            ),
        ):
            result = _run(
                node.execute_as_tool(
                    {"to": "+14155551234", "text": "hi", "phone_number_id": "ATTACKER_NUMBER"},
                    {},
                    _ctx(),
                )
            )

        assert "ATTACKER_NUMBER" not in captured["path"]
        assert captured["path"].startswith("OPERATOR_NUMBER/")
        assert result.get("phone_number_id") == "OPERATOR_NUMBER"

    def test_missing_credential_number_is_a_clear_error(self):
        node = WhatsAppCloudSendNode()

        with patch(
            "services.plugin.deps.get_auth_service",
            return_value=SimpleNamespace(get_api_key=AsyncMock(return_value=None)),
        ):
            envelope = _run(
                node.execute("wac-1", {"to": "+14155551234", "text": "hi"}, _ctx())
            )

        assert envelope["success"] is False
        assert envelope["error_type"] == "NodeUserError"
        assert "phone number" in envelope["error"].lower()


class TestSendText:
    def _send(self, params, *, number="PN1"):
        node = WhatsAppCloudSendNode()
        captured = {}

        async def _fake_post(ctx, path, body=None, **kwargs):
            captured["path"] = path
            captured["body"] = body
            return {"messages": [{"id": "wamid.X"}], "contacts": [{"wa_id": "1"}]}

        with (
            patch("nodes.whatsapp_cloud.whatsapp_cloud_send.graph_post", new=_fake_post),
            patch(
                "services.plugin.deps.get_auth_service",
                return_value=SimpleNamespace(get_api_key=AsyncMock(return_value=number)),
            ),
        ):
            _run(node.execute("wac-1", params, _ctx()))
        return captured

    def test_builds_the_documented_text_envelope(self):
        captured = self._send({"to": "+1 (415) 555-1234", "text": "hello", "format_markdown": False})
        assert captured["body"] == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            # Punctuation a user would paste from a contacts app is stripped;
            # Meta rejects it as error 131009 otherwise.
            "to": "+14155551234",
            "type": "text",
            "text": {"preview_url": False, "body": "hello"},
        }

    def test_reply_threads_via_context(self):
        captured = self._send(
            {"to": "+14155551234", "text": "hi", "reply_to_message_id": "wamid.PARENT", "format_markdown": False}
        )
        assert captured["body"]["context"] == {"message_id": "wamid.PARENT"}

    def test_no_context_key_when_not_replying(self):
        captured = self._send({"to": "+14155551234", "text": "hi", "format_markdown": False})
        assert "context" not in captured["body"]

    def test_markdown_is_converted_by_default(self):
        captured = self._send({"to": "+14155551234", "text": "**bold**"})
        assert captured["body"]["text"]["body"] == "*bold*"

    def test_oversize_body_is_refused_not_truncated(self):
        """Silently dropping the tail of a business message is worse than
        failing."""
        node = WhatsAppCloudSendNode()
        with patch(
            "services.plugin.deps.get_auth_service",
            return_value=SimpleNamespace(get_api_key=AsyncMock(return_value="PN1")),
        ):
            envelope = _run(
                node.execute(
                    "wac-1",
                    {"to": "+14155551234", "text": "a" * 5000, "format_markdown": False},
                    _ctx(),
                )
            )
        assert envelope["success"] is False
        assert "4096" in envelope["error"]

    def test_empty_body_is_refused(self):
        node = WhatsAppCloudSendNode()
        with patch(
            "services.plugin.deps.get_auth_service",
            return_value=SimpleNamespace(get_api_key=AsyncMock(return_value="PN1")),
        ):
            envelope = _run(node.execute("wac-1", {"to": "+14155551234", "text": "   "}, _ctx()))
        assert envelope["success"] is False


class TestRecipientNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+1 (415) 555-1234", "+14155551234"),
            ("14155551234", "14155551234"),
            ("+44 20 7946 0958", "+442079460958"),
        ],
    )
    def test_punctuation_is_stripped(self, raw, expected):
        assert normalize_recipient(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "+", "not-a-number"])
    def test_unusable_input_is_refused(self, raw):
        with pytest.raises(NodeUserError):
            normalize_recipient(raw)


class TestErrorClassification:
    @pytest.mark.parametrize(
        "code,category,retryable",
        [
            (190, "auth", True),
            (0, "auth", True),
            (10, "permission", False),
            (200, "permission", False),
            (130429, "throttle", True),
            (131056, "throttle", True),
            (131057, "transient", True),
            (131047, "window_closed", False),
            (131050, "policy", False),
            (132000, "template", False),
            (133015, "account", False),
        ],
    )
    def test_codes_land_in_the_right_class(self, code, category, retryable):
        assert classify_error(code) == (category, retryable)

    def test_throttles_are_not_node_user_errors(self):
        """NodeUserError is non-retryable in the shared policy, so raising one
        for a throttle would defeat the backoff Meta asks for."""
        from nodes.whatsapp_cloud._base import raise_for_graph_error

        with pytest.raises(RuntimeError) as exc:
            raise_for_graph_error(
                {"error": {"code": 130429, "message": "Rate limit hit"}}, 429
            )
        assert not isinstance(exc.value, NodeUserError)

    def test_window_closed_points_at_the_template_node(self):
        from nodes.whatsapp_cloud._base import raise_for_graph_error

        with pytest.raises(NodeUserError) as exc:
            raise_for_graph_error({"error": {"code": 131047, "message": "Re-engagement"}}, 400)
        assert "Template" in str(exc.value)

    def test_auth_failure_carries_credential_annotations(self):
        """The annotated PermissionError is what produces the reconnect chip."""
        from nodes.whatsapp_cloud._base import raise_for_graph_error

        with pytest.raises(PermissionError) as exc:
            raise_for_graph_error({"error": {"code": 190, "message": "expired"}}, 401)
        assert exc.value.provider == "whatsapp_cloud"
        assert exc.value.auth == "api_key"


class TestGraphVersionPin:
    def test_version_is_pinned_not_derived(self):
        """An expired Graph version does not error -- Meta silently falls
        through to the next oldest, so the pin must be explicit."""
        from nodes.whatsapp_cloud._base import GRAPH_API_VERSION

        assert GRAPH_API_VERSION.startswith("v")
        assert GRAPH_API_VERSION[1:].replace(".", "").isdigit()
