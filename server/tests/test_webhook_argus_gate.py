"""Argus gate: unregistered webhook paths must be rejected with 404.

Proves the exploit scenario reported in the Phase 4 security review:
  POST /webhook/unregistered/path (no signature) must return 404
  and must NOT call broadcast_webhook_received.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from routers.webhook import router


@pytest.fixture()
def app():
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


class TestArgusGate:
    """Unregistered paths must never reach broadcast_webhook_received."""

    def test_unregistered_path_returns_404(self, client):
        """POST /webhook/unregistered/path with no signature -> 404."""
        response = client.post(
            "/webhook/dcs/incident",
            json={"payload": "evil"},
        )
        assert response.status_code == 404
        assert response.json() == {"error": "webhook_path_not_found"}

    def test_unregistered_path_does_not_broadcast(self, client):
        """broadcast_webhook_received must NOT be called for unregistered paths."""
        with patch(
            "nodes.trigger.webhook_trigger._events.broadcast_webhook_received",
            new_callable=AsyncMock,
        ) as mock_broadcast:
            client.post(
                "/webhook/unregistered/path",
                json={"key": "value"},
            )
            mock_broadcast.assert_not_called()

    def test_unregistered_path_no_auth_header_returns_404(self, client):
        """Even with a signature header, an unregistered path returns 404."""
        response = client.post(
            "/webhook/some/unknown/path",
            json={"data": "x"},
            headers={"x-webhook-signature": "sha256=abc123"},
        )
        assert response.status_code == 404

    def test_registered_path_still_works(self, app, client):
        """Registered WebhookSource paths are unaffected by the gate."""
        from services.events import WEBHOOK_SOURCES

        mock_source = MagicMock()
        mock_source.skip_signature_check = True
        mock_source.handle_get = AsyncMock(return_value=None)
        mock_source.handle = AsyncMock()

        original = WEBHOOK_SOURCES.copy()
        try:
            WEBHOOK_SOURCES["test/registered"] = mock_source
            response = client.post(
                "/webhook/test/registered",
                json={"ok": True},
            )
            assert response.status_code == 200
            mock_source.handle.assert_called_once()
        finally:
            WEBHOOK_SOURCES.clear()
            WEBHOOK_SOURCES.update(original)
