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

from nodes.whatsapp_business._base import classify_error, normalize_recipient
from nodes.whatsapp_business.whatsapp_business_send import (
    WhatsAppBusinessSendNode,
    WhatsAppBusinessSendParams,
)
from services.plugin import NodeUserError


pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ctx(**raw):
    return SimpleNamespace(
        node_id="wac-1",
        node_type="whatsappBusinessSend",
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
        assert "phone_number_id" not in WhatsAppBusinessSendParams.model_fields

    def test_tool_schema_never_exposes_the_sending_number(self):
        schema = WhatsAppBusinessSendParams.model_json_schema()
        assert "phone_number_id" not in schema.get("properties", {})

    def test_tool_facing_schema_is_flat(self):
        """LLM function-calling rejects $ref; the contract test enforces this
        repo-wide, asserted here too so the reason is local."""
        import json

        schema = WhatsAppBusinessSendParams.model_json_schema()
        assert "$defs" not in schema
        assert "$ref" not in json.dumps(schema)


class TestModelCannotChooseTheSendingNumber:
    def test_tool_args_cannot_override_the_credential_number(self):
        """Drive the real tool path, not the schema.

        A prompt injection in an inbound WhatsApp message is the realistic
        source of hostile tool arguments, and sending as another tenant's
        number is the worst outcome available here.
        """
        node = WhatsAppBusinessSendNode()
        captured = {}

        async def _fake_post(ctx, path, body=None, **kwargs):
            captured["path"] = path
            captured["body"] = body
            return {
                "messages": [{"id": "wamid.TEST"}],
                "contacts": [{"wa_id": "14155551234"}],
            }

        with (
            patch("nodes.whatsapp_business.whatsapp_business_send.graph_post", new=_fake_post),
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
        node = WhatsAppBusinessSendNode()

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
        node = WhatsAppBusinessSendNode()
        captured = {}

        async def _fake_post(ctx, path, body=None, **kwargs):
            captured["path"] = path
            captured["body"] = body
            return {"messages": [{"id": "wamid.X"}], "contacts": [{"wa_id": "1"}]}

        with (
            patch("nodes.whatsapp_business.whatsapp_business_send.graph_post", new=_fake_post),
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
        node = WhatsAppBusinessSendNode()
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
        node = WhatsAppBusinessSendNode()
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
        from nodes.whatsapp_business._base import raise_for_graph_error

        with pytest.raises(RuntimeError) as exc:
            raise_for_graph_error(
                {"error": {"code": 130429, "message": "Rate limit hit"}}, 429
            )
        assert not isinstance(exc.value, NodeUserError)

    def test_window_closed_points_at_the_template_node(self):
        from nodes.whatsapp_business._base import raise_for_graph_error

        with pytest.raises(NodeUserError) as exc:
            raise_for_graph_error({"error": {"code": 131047, "message": "Re-engagement"}}, 400)
        assert "Template" in str(exc.value)

    def test_auth_failure_carries_credential_annotations(self):
        """The annotated PermissionError is what produces the reconnect chip."""
        from nodes.whatsapp_business._base import raise_for_graph_error

        with pytest.raises(PermissionError) as exc:
            raise_for_graph_error({"error": {"code": 190, "message": "expired"}}, 401)
        assert exc.value.provider == "whatsapp_business"
        assert exc.value.auth == "api_key"


class TestGraphVersionPin:
    def test_version_is_pinned_not_derived(self):
        """An expired Graph version does not error -- Meta silently falls
        through to the next oldest, so the pin must be explicit."""
        from nodes.whatsapp_business._base import GRAPH_API_VERSION

        assert GRAPH_API_VERSION.startswith("v")
        assert GRAPH_API_VERSION[1:].replace(".", "").isdigit()


# ==========================================================================
# Trigger — the deployed path
# ==========================================================================
#
# Every failure covered here is invisible on the canvas Run path. That is the
# whole reason they are asserted: pressing Run exercises event_waiter, while
# deploy goes through dispatch.emit -> Temporal Visibility -> a listener whose
# EventType Search Attribute has to match the envelope exactly.


class TestTriggerIsDeployable:
    def test_trigger_types_are_registered_for_deployment(self):
        """find_trigger_nodes filters on this set. Omission means deploy
        silently ignores the node -- no listener, no error."""
        from constants import WORKFLOW_TRIGGER_TYPES

        assert "whatsappBusinessReceive" in WORKFLOW_TRIGGER_TYPES
        assert "whatsappBusinessStatus" in WORKFLOW_TRIGGER_TYPES

    def test_canary_types_match_the_emitted_envelope(self):
        """A mismatch here is the silent killer: the Visibility query asks for
        one string, the listener advertises another, and no signal arrives."""
        from nodes.whatsapp_business._events import MESSAGE_RECEIVED_TYPE, STATUS_UPDATED_TYPE
        from services.deployment.canary_registry import cloudevent_type_for

        assert cloudevent_type_for("whatsappBusinessReceive") == MESSAGE_RECEIVED_TYPE
        assert cloudevent_type_for("whatsappBusinessStatus") == STATUS_UPDATED_TYPE

    def test_emitted_types_match_the_node_prefixes(self):
        from nodes.whatsapp_business._events import MESSAGE_RECEIVED_TYPE, STATUS_UPDATED_TYPE
        from nodes.whatsapp_business.whatsapp_business_receive import (
            WhatsAppBusinessReceiveNode,
            WhatsAppBusinessStatusNode,
        )

        assert MESSAGE_RECEIVED_TYPE.startswith(WhatsAppBusinessReceiveNode.event_type_prefix)
        assert STATUS_UPDATED_TYPE.startswith(WhatsAppBusinessStatusNode.event_type_prefix)

    def test_webhook_path_is_claimed(self):
        from services.events import WEBHOOK_SOURCES

        assert "whatsapp-business" in WEBHOOK_SOURCES

    def test_triggers_have_no_input_handles(self):
        from nodes.whatsapp_business.whatsapp_business_receive import (
            WhatsAppBusinessReceiveNode,
            WhatsAppBusinessStatusNode,
        )

        for node in (WhatsAppBusinessReceiveNode, WhatsAppBusinessStatusNode):
            assert not [h for h in node.handles if h["kind"] == "input"]


def _webhook_body(*, messages=None, statuses=None, entries=1):
    """Build a Meta webhook payload with a controllable nesting shape."""
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550783881", "phone_number_id": "PN1"},
    }
    if messages is not None:
        value["contacts"] = [{"profile": {"name": "Sheena"}, "wa_id": "16505551234"}]
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"value": value, "field": "messages"}]} for _ in range(entries)],
    }


def _text_message(mid: str):
    return {
        "from": "16505551234",
        "id": mid,
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": "Does it come in another color?"},
    }


class TestThreeLevelFanOut:
    """Reading only entry[0].changes[0] is the documented common bug."""

    def test_every_entry_and_message_produces_an_event(self):
        from nodes.whatsapp_business._source import iter_events

        body = _webhook_body(messages=[_text_message("wamid.A"), _text_message("wamid.B")], entries=2)
        events = iter_events(body)

        assert len(events) == 4
        assert {event_id for _, _, event_id in events} == {"wamid.A", "wamid.B"}

    def test_messages_and_statuses_in_one_payload_both_surface(self):
        from nodes.whatsapp_business._source import iter_events

        body = _webhook_body(
            messages=[_text_message("wamid.A")],
            statuses=[{"id": "wamid.OUT", "status": "delivered", "recipient_id": "165"}],
        )
        kinds = [kind for kind, _, _ in iter_events(body)]
        assert kinds == ["message", "status"]

    def test_message_without_an_id_is_dropped(self):
        """A minted id would defeat replay dedup across Meta's 7-day retries."""
        from nodes.whatsapp_business._source import iter_events

        body = _webhook_body(messages=[{"from": "165", "type": "text", "text": {"body": "x"}}])
        assert iter_events(body) == []

    def test_empty_payload_yields_nothing(self):
        from nodes.whatsapp_business._source import iter_events

        assert iter_events({"entry": []}) == []


class TestStatusDedupKey:
    def test_status_ids_are_composite_not_bare_wamids(self):
        """The same wamid reports sent -> delivered -> read. Deduping on the
        wamid alone would collapse the lifecycle into one event and the
        listener would drop two of the three."""
        from nodes.whatsapp_business._source import iter_events

        body = _webhook_body(
            statuses=[
                {"id": "wamid.OUT", "status": "sent", "recipient_id": "165"},
                {"id": "wamid.OUT", "status": "delivered", "recipient_id": "165"},
                {"id": "wamid.OUT", "status": "read", "recipient_id": "165"},
            ]
        )
        ids = [event_id for _, _, event_id in iter_events(body)]
        assert ids == ["wamid.OUT:sent", "wamid.OUT:delivered", "wamid.OUT:read"]
        assert len(set(ids)) == 3


class TestMessageShaping:
    def test_output_is_flat_for_template_resolution(self):
        """Deployed, event["data"] IS the trigger output and {{trigger.field}}
        resolves against its top level."""
        from nodes.whatsapp_business._source import iter_events

        (_, data, _), = iter_events(_webhook_body(messages=[_text_message("wamid.A")]))
        assert data["message_id"] == "wamid.A"
        assert data["from"] == "16505551234"
        assert data["text"] == "Does it come in another color?"
        assert data["profile_name"] == "Sheena"
        assert data["phone_number_id"] == "PN1"

    def test_media_carries_an_id_and_never_bytes(self):
        from nodes.whatsapp_business._source import iter_events

        message = {
            "from": "165",
            "id": "wamid.IMG",
            "type": "image",
            "image": {"id": "MEDIA123", "mime_type": "image/jpeg", "sha256": "abc", "caption": "look"},
        }
        (_, data, _), = iter_events(_webhook_body(messages=[message]))
        assert data["media"]["id"] == "MEDIA123"
        assert data["text"] == "look"
        serialized = str(data)
        assert "base64" not in serialized and "data:" not in serialized

    def test_interactive_reply_reads_as_text(self):
        """A button tap is the user speaking; downstream should not have to
        branch on message type to read it."""
        from nodes.whatsapp_business._source import iter_events

        message = {
            "from": "165",
            "id": "wamid.INT",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "cancel", "title": "Cancel"}},
        }
        (_, data, _), = iter_events(_webhook_body(messages=[message]))
        assert data["text"] == "Cancel"
        assert data["interactive_reply"]["id"] == "cancel"

    def test_status_exposes_the_window_expiry(self):
        from nodes.whatsapp_business._source import iter_events

        status = {
            "id": "wamid.OUT",
            "status": "sent",
            "recipient_id": "165",
            "conversation": {"id": "c1", "expiration_timestamp": "1750116480", "origin": {"type": "marketing"}},
            "pricing": {"billable": True, "pricing_model": "PMP", "category": "marketing"},
        }
        (_, data, _), = iter_events(_webhook_body(statuses=[status]))
        assert data["conversation_expires_at"] == "1750116480"
        assert data["pricing_model"] == "PMP"


class TestSubscriptionHandshake:
    def _request(self, **params):
        return SimpleNamespace(method="GET", headers={}, query_params=params)

    def _source(self, verify_token):
        from nodes.whatsapp_business._source import WhatsAppBusinessWebhookSource

        class _Cred:
            @classmethod
            async def resolve(cls):
                if verify_token is None:
                    raise PermissionError
                return {"whatsapp_business_verify_token": verify_token}

        source = WhatsAppBusinessWebhookSource()
        source.credential = _Cred
        return source

    def test_correct_token_echoes_the_bare_challenge(self):
        """Meta rejects a JSON envelope here -- it wants the raw value."""
        source = self._source("s3cret")
        resp = _run(source.handle_get(self._request(**{"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "1158201444"})))
        assert resp.status_code == 200
        assert resp.body == b"1158201444"
        assert resp.media_type == "text/plain"

    def test_wrong_token_is_refused(self):
        source = self._source("s3cret")
        resp = _run(source.handle_get(self._request(**{"hub.verify_token": "guess", "hub.challenge": "123"})))
        assert resp.status_code == 403

    def test_unconfigured_token_refuses_rather_than_accepting(self):
        source = self._source(None)
        resp = _run(source.handle_get(self._request(**{"hub.verify_token": "anything", "hub.challenge": "123"})))
        assert resp.status_code == 403

    def test_non_handshake_get_falls_through(self):
        source = self._source("s3cret")
        assert _run(source.handle_get(self._request())) is None


class TestSourceReachesDeployedListeners:
    """The single highest-value test in this file.

    WebhookSource.handle only calls event_waiter.dispatch, which serves the
    canvas Run path. Deployed triggers are Temporal listeners reached solely
    by services.events.dispatch.emit. A source that never calls emit works
    perfectly when you press Run and does nothing once deployed -- with no
    error anywhere.
    """

    def test_shape_emits_one_event_per_message(self):
        from nodes.whatsapp_business._source import WhatsAppBusinessWebhookSource

        source = WhatsAppBusinessWebhookSource()
        body = _webhook_body(messages=[_text_message("wamid.A"), _text_message("wamid.B")])

        with patch("services.events.dispatch.emit", new=AsyncMock()) as emit:
            _run(source.shape(SimpleNamespace(), b"{}", body))

        assert emit.await_count == 2
        emitted = [call.args[0] for call in emit.await_args_list]
        assert {ev.id for ev in emitted} == {"wamid.A", "wamid.B"}
        assert {ev.type for ev in emitted} == {"com.opencompany.whatsapp_business.message.received"}

    def test_statuses_emit_under_their_own_type(self):
        """Distinct types are the only discriminator that works deployed."""
        from nodes.whatsapp_business._source import WhatsAppBusinessWebhookSource

        source = WhatsAppBusinessWebhookSource()
        body = _webhook_body(statuses=[{"id": "wamid.OUT", "status": "failed", "recipient_id": "165"}])

        with patch("services.events.dispatch.emit", new=AsyncMock()) as emit:
            _run(source.shape(SimpleNamespace(), b"{}", body))

        assert emit.await_count == 1
        event = emit.await_args_list[0].args[0]
        assert event.type == "com.opencompany.whatsapp_business.status.updated"
        assert event.id == "wamid.OUT:failed"

    def test_emitted_data_is_a_flat_dict(self):
        """A non-dict is coerced to {} upstream, silently emptying the trigger."""
        from nodes.whatsapp_business._source import WhatsAppBusinessWebhookSource

        source = WhatsAppBusinessWebhookSource()
        with patch("services.events.dispatch.emit", new=AsyncMock()) as emit:
            _run(source.shape(SimpleNamespace(), b"{}", _webhook_body(messages=[_text_message("wamid.A")])))

        data = emit.await_args_list[0].args[0].data
        assert isinstance(data, dict)
        assert data["message_id"] == "wamid.A"

    def test_envelope_carries_no_workflow_id(self):
        """Setting it would scope delivery to one deployment; webhook events
        are meant to reach every deployment carrying the trigger."""
        from nodes.whatsapp_business._source import WhatsAppBusinessWebhookSource

        source = WhatsAppBusinessWebhookSource()
        with patch("services.events.dispatch.emit", new=AsyncMock()) as emit:
            _run(source.shape(SimpleNamespace(), b"{}", _webhook_body(messages=[_text_message("wamid.A")])))

        assert emit.await_args_list[0].args[0].workflow_id is None
