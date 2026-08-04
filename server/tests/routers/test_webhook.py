"""Webhook router dispatch — registered sources vs the legacy fallback.

The router owns one decision that the sources cannot make for themselves:
whether a GET is a provider subscription handshake (answered by the source
with its own Response) or ordinary traffic (handled by ``handle()``).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from routers.webhook import router
from services.events import WEBHOOK_SOURCES, WebhookSource, WorkflowEvent


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def clean_registry():
    """Restore WEBHOOK_SOURCES so tests cannot leak paths into each other."""
    original = dict(WEBHOOK_SOURCES)
    try:
        yield WEBHOOK_SOURCES
    finally:
        WEBHOOK_SOURCES.clear()
        WEBHOOK_SOURCES.update(original)


class _RecordingSource(WebhookSource):
    """Unverified source that records whether handle() ran."""

    type = "recording.hook"
    path = "recording"

    def __init__(self):
        super().__init__()
        self.handled = 0

    async def shape(self, request, body, payload):
        return WorkflowEvent(source="rec://x", type="recording.event", data=payload)

    async def handle(self, request):
        self.handled += 1
        return await super().handle(request)


class _HandshakeSource(_RecordingSource):
    """Answers a GET carrying hub.challenge, declines every other GET."""

    type = "handshake.hook"
    path = "handshake"

    async def handle_get(self, request):
        challenge = request.query_params.get("hub.challenge")
        if challenge is None:
            return None
        return PlainTextResponse(challenge)


class TestGetHandshake:
    def test_handshake_response_is_returned_verbatim(self, client, clean_registry):
        """The source's body reaches the caller unwrapped.

        Meta rejects a JSON envelope here -- it wants the bare challenge.
        """
        source = _HandshakeSource()
        clean_registry[source.path] = source

        resp = client.get("/webhook/handshake?hub.mode=subscribe&hub.challenge=1158201444")

        assert resp.status_code == 200
        assert resp.text == "1158201444"
        assert resp.headers["content-type"].startswith("text/plain")
        # A handshake is not an event -- handle() must not run.
        assert source.handled == 0

    def test_declining_falls_through_to_handle(self, client, clean_registry):
        """Returning None keeps the pre-existing behaviour exactly."""
        source = _HandshakeSource()
        clean_registry[source.path] = source

        with patch("services.event_waiter.dispatch"):
            resp = client.get("/webhook/handshake")

        assert resp.status_code == 200
        assert resp.json() == {"status": "received", "path": "handshake"}
        assert source.handled == 1

    def test_source_without_override_is_unaffected(self, client, clean_registry):
        """Every existing source predates this hook and must behave as before."""
        source = _RecordingSource()
        clean_registry[source.path] = source

        with patch("services.event_waiter.dispatch"):
            resp = client.get("/webhook/recording")

        assert resp.status_code == 200
        assert resp.json() == {"status": "received", "path": "recording"}
        assert source.handled == 1

    def test_post_never_consults_handle_get(self, client, clean_registry):
        """The hook is GET-only; a POST carrying the query params still
        goes through handle()."""
        source = _HandshakeSource()
        clean_registry[source.path] = source

        with patch("services.event_waiter.dispatch"):
            resp = client.post("/webhook/handshake?hub.challenge=123", json={"a": 1})

        assert resp.status_code == 200
        assert resp.json() == {"status": "received", "path": "handshake"}
        assert source.handled == 1


class TestLegacyFallback:
    def test_unclaimed_path_still_reaches_the_generic_handler(self, client, clean_registry):
        """An unregistered path must keep firing webhookTrigger nodes."""
        clean_registry.clear()

        with patch("nodes.trigger.webhook_trigger._events.broadcast_webhook_received") as broadcast:
            resp = client.post("/webhook/unclaimed", json={"hello": "world"})

        assert resp.status_code == 200
        broadcast.assert_called_once()
        payload = broadcast.call_args[0][0]
        assert payload["path"] == "unclaimed"
        assert payload["json"] == {"hello": "world"}
