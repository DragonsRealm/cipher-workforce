"""Contract for the Discord plugin.

The security-relevant test is ``TestModelCannotChooseTheAccount``. On a
dual-purpose ActionNode ``BaseNode.execute_as_tool`` merges
``{**node_params, **tool_args}`` with model arguments winning, so the only
thing standing between a prompt injection in an inbound Discord message and
sending as a different bot is ``server_controlled_fields``. This asserts it
actually holds through the real tool path, not just the schema.
"""

from __future__ import annotations

import asyncio
import typing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nodes.discord import _base, _ratelimit
from nodes.discord._accounts import (
    DEFAULT_ACCOUNT,
    account_id_from_scope,
    storage_scope,
)
from nodes.discord.discord_action import DiscordActionNode, DiscordActionParams
from nodes.discord.discord_send import (
    MAX_CONTENT,
    DiscordSendNode,
    DiscordSendParams,
    split_content,
)
from services.plugin import NodeUserError

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ctx(**raw):
    return SimpleNamespace(
        node_id="discord-1",
        node_type="discordSend",
        workflow_id="wf-1",
        session_id="default",
        user_id="owner",
        workspace_dir=None,
        outputs={},
        nodes=[],
        edges=[],
        raw=dict(raw),
    )


class TestAccountScoping:
    """account_id <-> session_id is the whole multi-account mechanism."""

    def test_default_account_uses_the_unprefixed_scope(self):
        """The credentials modal writes session_id="default" with no account
        concept at all. If that did not map to the default account, a
        single-bot install would see no credential."""
        assert storage_scope("") == DEFAULT_ACCOUNT
        assert storage_scope(DEFAULT_ACCOUNT) == DEFAULT_ACCOUNT

    def test_named_account_is_namespaced(self):
        assert storage_scope("123456") == "discord:123456"

    @pytest.mark.parametrize("account_id", ["", "default", "123456", "999"])
    def test_scope_round_trips(self, account_id):
        expected = account_id or DEFAULT_ACCOUNT
        assert account_id_from_scope(storage_scope(account_id)) == expected

    def test_scope_does_not_collide_with_another_provider(self):
        """The prefix is what keeps a Discord account from reading a scope
        some other plugin created."""
        assert storage_scope("123").startswith("discord:")


class TestPathGuard:
    """discordAction's custom operation lets a workflow name its own route.

    Without this guard that is an SSRF primitive that would send the bot
    token to an arbitrary host.
    """

    def test_relative_path_is_joined_onto_the_pinned_base(self):
        assert _base.build_url("users/@me") == f"{_base.API_BASE_URL}/{_base.API_VERSION}/users/@me"

    def test_leading_slash_is_tolerated(self):
        assert _base.build_url("/users/@me").endswith("/users/@me")

    @pytest.mark.parametrize(
        "path",
        [
            "https://evil.example/steal",
            "http://evil.example/steal",
            "//evil.example/steal",
            "../../../etc/passwd",
            "users/../../admin",
            "",
            "   ",
        ],
    )
    def test_escaping_paths_are_refused(self, path):
        with pytest.raises(NodeUserError):
            _base.build_url(path)

    @pytest.mark.parametrize("url", ["https://evil.example/api/webhooks/1/t", "https://discord.com.evil.io/x"])
    def test_webhook_host_is_whitelisted(self, url):
        with pytest.raises(NodeUserError):
            _base.assert_discord_host(url)

    def test_real_webhook_host_is_allowed(self):
        _base.assert_discord_host("https://discord.com/api/webhooks/1/token")


class TestErrorClassification:
    """The split that matters is retryable vs terminal.

    NodeUserError is non-retryable in the shared policy, so classifying a
    throttle as one fails fast instead of backing off; classifying a
    permanent rejection as retryable burns three attempts to reach the same
    answer.
    """

    @pytest.mark.parametrize(
        "code, status, category, retryable",
        [
            (0, 401, "auth", False),
            (40001, 401, "auth", False),
            (50001, 403, "permission", False),
            (50013, 403, "permission", False),
            (10003, 404, "not_found", False),
            (0, 429, "throttle", True),
            (0, 500, "transient", True),
            (0, 502, "transient", True),
            (0, 400, "unknown", False),
        ],
    )
    def test_classification(self, code, status, category, retryable):
        assert _base.classify_error(code, status) == (category, retryable)

    def test_auth_failure_raises_annotated_permission_error(self):
        """An annotated PermissionError gets the framework's credential
        envelope and a reconnect affordance; a NodeUserError does not."""
        with pytest.raises(PermissionError) as excinfo:
            _base.raise_for_discord_error({"code": 40001, "message": "Unauthorized"}, 401)
        assert excinfo.value.provider == "discord"
        assert excinfo.value.reason == "invalid"
        assert excinfo.value.auth == "api_key"

    def test_permission_failure_is_a_user_error(self):
        with pytest.raises(NodeUserError):
            _base.raise_for_discord_error({"code": 50013, "message": "Missing Permissions"}, 403)

    def test_server_failure_is_retryable_not_a_user_error(self):
        with pytest.raises(RuntimeError) as excinfo:
            _base.raise_for_discord_error({"code": 0, "message": "boom"}, 502)
        assert not isinstance(excinfo.value, NodeUserError)


class TestRateLimit:
    def test_body_retry_after_beats_the_rounded_header(self):
        """The header is whole seconds; the body carries sub-second
        precision. Preferring the header would over-wait on every 429."""
        assert _ratelimit.parse_retry_after({"retry_after": 0.35}, {"Retry-After": "1"}) == 0.35

    def test_header_is_the_fallback(self):
        assert _ratelimit.parse_retry_after(None, {"Retry-After": "2"}) == 2.0

    def test_malformed_retry_after_degrades_instead_of_raising(self):
        """These values are attacker-adjacent; a parse error must not become
        an unhandled exception mid-send."""
        assert _ratelimit.parse_retry_after({"retry_after": "soon"}, {"Retry-After": "nope"}) == 1.0

    def test_global_limit_detected_from_body_or_scope_header(self):
        assert _ratelimit.is_global_limit({"global": True}, {})
        assert _ratelimit.is_global_limit(None, {"X-RateLimit-Scope": "global"})
        assert not _ratelimit.is_global_limit({"global": False}, {"X-RateLimit-Scope": "user"})

    def test_non_json_429_is_recognised_as_the_edge_ban(self):
        """Discord's own 429 is always JSON. An HTML one is Cloudflare
        rejecting the host IP, which has a different remedy and would
        otherwise read as an hour of unexplained rate limiting."""
        assert _ratelimit.is_cloudflare_ban({"Content-Type": "text/html"})
        assert not _ratelimit.is_cloudflare_ban({"Content-Type": "application/json"})

    def test_invalid_request_guard_trips_before_the_ban(self):
        guard = _ratelimit._InvalidRequestGuard()
        for _ in range(_ratelimit.INVALID_REQUEST_SAFETY_MARGIN):
            guard.record(401, now=1000.0)
        with pytest.raises(_ratelimit.InvalidRequestBudgetExhausted):
            guard.check(now=1000.0)

    def test_guard_only_counts_rejections(self):
        guard = _ratelimit._InvalidRequestGuard()
        for _ in range(100):
            guard.record(200, now=1000.0)
        assert guard.count(now=1000.0) == 0

    def test_guard_window_expires(self):
        guard = _ratelimit._InvalidRequestGuard()
        guard.record(429, now=1000.0)
        assert guard.count(now=1000.0) == 1
        assert guard.count(now=1000.0 + _ratelimit.INVALID_REQUEST_WINDOW_SECONDS + 1) == 0

    def test_guard_is_process_wide(self):
        """The ban is enforced per source IP, not per token, so three bots on
        one host share one budget. A per-account guard would not stop the
        ban it exists to prevent."""
        assert _ratelimit.invalid_request_guard() is _ratelimit.invalid_request_guard()


class TestContentSplitting:
    def test_short_text_is_one_chunk(self):
        assert split_content("hello") == ["hello"]

    def test_empty_text_produces_no_chunks(self):
        assert split_content("") == []

    def test_every_chunk_is_within_the_limit(self):
        chunks = split_content("word " * 2000)
        assert chunks
        assert all(len(c) <= MAX_CONTENT for c in chunks)

    def test_split_prefers_a_paragraph_boundary(self):
        first = "a" * 1200
        second = "b" * 1200
        chunks = split_content(f"{first}\n\n{second}")
        assert chunks[0] == first

    def test_unsplittable_text_is_hard_cut_rather_than_dropped(self):
        text = "x" * 5000
        chunks = split_content(text)
        assert all(len(c) <= MAX_CONTENT for c in chunks)
        assert "".join(chunks) == text


class TestModelCannotChooseTheAccount:
    """Which bot sends is operator configuration, not a model decision."""

    def test_tool_args_cannot_override_the_configured_account(self):
        node = DiscordSendNode()
        captured = {}

        async def _fake_post(path, body=None, *, account_id=DEFAULT_ACCOUNT, **kwargs):
            captured["account_id"] = account_id
            captured["path"] = path
            return {"id": "123", "channel_id": "c1"}

        with patch("nodes.discord.discord_send._base.post", new=_fake_post):
            _run(
                node.execute_as_tool(
                    {"channel_id": "c1", "message": "hi", "account_id": "ATTACKER"},
                    {"account_id": "operator-account", "channel_id": "c1"},
                    _ctx(),
                )
            )

        assert captured["account_id"] == "operator-account"

    def test_account_is_declared_server_controlled_on_both_nodes(self):
        for node_cls in (DiscordSendNode, DiscordActionNode):
            assert "account_id" in node_cls.server_controlled_fields

    def test_action_node_account_cannot_be_overridden(self):
        node = DiscordActionNode()
        captured = {}

        async def _fake_get(path, *, account_id=DEFAULT_ACCOUNT, params=None):
            captured["account_id"] = account_id
            return []

        with patch("nodes.discord.discord_action._base.get", new=_fake_get):
            _run(
                node.execute_as_tool(
                    {"operation": "list_guilds", "account_id": "ATTACKER"},
                    {"operation": "list_guilds", "account_id": "operator-account"},
                    _ctx(),
                )
            )

        assert captured["account_id"] == "operator-account"


class TestSendShape:
    def _capture(self, params, **patches):
        node = DiscordSendNode()
        captured = {"posts": []}

        async def _fake_post(path, body=None, *, account_id=DEFAULT_ACCOUNT, **kwargs):
            captured["posts"].append({"path": path, "body": body, "kwargs": kwargs})
            return {"id": f"m{len(captured['posts'])}", "channel_id": "c1"}

        with patch("nodes.discord.discord_send._base.post", new=_fake_post):
            captured["result"] = _run(node.send(_ctx(), params))
        return captured

    def test_channel_send_posts_to_the_channel_messages_route(self):
        captured = self._capture(DiscordSendParams(channel_id="c1", message="hi"))
        assert captured["posts"][0]["path"] == "channels/c1/messages"
        assert captured["posts"][0]["body"]["content"] == "hi"

    def test_dm_opens_a_channel_first(self):
        """Discord has no send-to-user route; a DM channel must be opened."""
        captured = self._capture(DiscordSendParams(target_type="user", user_id="u1", message="hi"))
        assert captured["posts"][0]["path"] == "users/@me/channels"

    def test_long_message_is_split_across_posts(self):
        captured = self._capture(DiscordSendParams(channel_id="c1", message="word " * 2000))
        assert len(captured["posts"]) > 1
        assert captured["result"].parts == len(captured["posts"])
        assert len(captured["result"].message_ids) == len(captured["posts"])

    def test_reply_reference_rides_only_the_first_message(self):
        captured = self._capture(
            DiscordSendParams(channel_id="c1", message="word " * 2000, reply_to_message_id="m0")
        )
        assert "message_reference" in captured["posts"][0]["body"]
        assert all("message_reference" not in p["body"] for p in captured["posts"][1:])

    def test_embeds_ride_the_final_message(self):
        """So they render after the text they belong to."""
        captured = self._capture(
            DiscordSendParams(channel_id="c1", message="word " * 2000, embeds=[{"title": "t"}])
        )
        assert "embeds" not in captured["posts"][0]["body"]
        assert captured["posts"][-1]["body"]["embeds"] == [{"title": "t"}]

    def test_empty_send_is_refused(self):
        node = DiscordSendNode()
        with pytest.raises(NodeUserError):
            _run(node.send(_ctx(), DiscordSendParams(channel_id="c1")))

    def test_too_many_embeds_is_refused_locally(self):
        """Discord rejects the whole message, so failing here is clearer."""
        node = DiscordSendNode()
        with pytest.raises(NodeUserError):
            _run(
                node.send(
                    _ctx(),
                    DiscordSendParams(channel_id="c1", embeds=[{"title": str(i)} for i in range(11)]),
                )
            )

    def test_missing_channel_is_a_clear_error(self):
        node = DiscordSendNode()
        with pytest.raises(NodeUserError):
            _run(node.send(_ctx(), DiscordSendParams(message="hi")))


class TestActionShape:
    def test_every_operation_has_a_method(self):
        declared = set(typing.get_args(DiscordActionParams.model_fields["operation"].annotation))
        implemented = {spec.name for spec in DiscordActionNode._operations.values()}
        assert declared == implemented

    def test_download_attachments_without_input_is_a_clear_error(self):
        node = DiscordActionNode()
        with pytest.raises(NodeUserError):
            _run(node.download_attachments(_ctx(), DiscordActionParams(operation="download_attachments")))

    def test_required_ids_are_validated(self):
        node = DiscordActionNode()
        with pytest.raises(NodeUserError):
            _run(node.list_channels(_ctx(), DiscordActionParams(operation="list_channels")))


class TestToolSchema:
    """Corpus-wide invariant, asserted locally so the reason is visible."""

    @pytest.mark.parametrize("node_cls", [DiscordSendNode, DiscordActionNode])
    def test_tool_schema_carries_no_unresolvable_ref(self, node_cls):
        """A $ref with no $defs alongside it is a pointer the LLM cannot
        follow. Nested models are fine -- they are inlined before emission."""
        schema = node_cls.Params.model_json_schema()
        rendered = str(schema)
        assert "$ref" not in rendered or "$defs" in rendered

    @pytest.mark.parametrize("node_cls", [DiscordSendNode, DiscordActionNode])
    def test_locked_fields_are_stripped_from_model_arguments(self, node_cls):
        """The guarantee AccountScopedNode exists to provide, at the class
        level: every locked field must be removed from tool_args before the
        merge that would otherwise let the model win."""
        assert node_cls.server_controlled_fields
        assert node_cls.execute_as_tool is not _base.ActionNode.execute_as_tool

    @pytest.mark.parametrize("node_cls", [DiscordSendNode, DiscordActionNode])
    def test_no_param_is_named_type(self, node_cls):
        """A Params field called `type` is silently dropped from the served
        schema, so the node would ship a parameter the panel never renders."""
        assert "type" not in node_cls.Params.model_fields
