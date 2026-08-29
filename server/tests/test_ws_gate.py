"""The WebSocket handshake gate must fail closed (CSWSH -> RCE regression).

Both sockets dispatch ``execute_node`` with a caller-supplied ``node_type``.
Before this gate, ``/ws/status`` skipped every check under
``VITE_AUTH_ENABLED=false`` and ``/ws/internal`` accepted unconditionally, so a
web page the operator merely visited could drive node execution as the owner.

Every case below asserts a REFUSAL except the two that present real proof —
the point of the test is that absence of evidence is refused, not tolerated.
"""

from __future__ import annotations

from services.authz.ws_gate import (
    INTERNAL_TOKEN_HEADER,
    STATUS_TOKEN_HEADER,
    authorize_internal_ws,
    authorize_status_ws,
    internal_ws_token,
)

PORT = 5678


class _Socket:
    def __init__(self, **headers: str) -> None:
        self.headers = headers


class TestStatusSocket:
    def test_same_origin_browser_is_admitted(self):
        assert authorize_status_ws(
            _Socket(host=f"127.0.0.1:{PORT}", origin=f"http://127.0.0.1:{PORT}"), PORT
        )

    def test_cross_origin_is_refused(self):
        assert not authorize_status_ws(
            _Socket(host=f"127.0.0.1:{PORT}", origin="http://evil.example"), PORT
        )

    def test_missing_origin_and_token_is_refused(self):
        # The exact CSWSH shape a bare client presents. Must not degrade open.
        assert not authorize_status_ws(_Socket(host=f"127.0.0.1:{PORT}"), PORT)

    def test_rebound_host_is_refused(self):
        assert not authorize_status_ws(
            _Socket(host=f"attacker.example:{PORT}", origin=f"http://127.0.0.1:{PORT}"), PORT
        )

    def test_origin_on_another_port_is_refused(self):
        assert not authorize_status_ws(
            _Socket(host=f"127.0.0.1:{PORT}", origin="http://127.0.0.1:9999"), PORT
        )

    def test_service_client_may_present_the_token(self):
        token = internal_ws_token()
        assert token
        assert authorize_status_ws(
            _Socket(**{"host": f"127.0.0.1:{PORT}", STATUS_TOKEN_HEADER: token}), PORT
        )


class TestInternalSocket:
    def test_worker_with_token_is_admitted(self):
        token = internal_ws_token()
        assert token
        assert authorize_internal_ws(
            _Socket(**{"host": f"127.0.0.1:{PORT}", INTERNAL_TOKEN_HEADER: token}), PORT
        )

    def test_no_token_is_refused(self):
        assert not authorize_internal_ws(_Socket(host=f"127.0.0.1:{PORT}"), PORT)

    def test_wrong_token_is_refused(self):
        assert not authorize_internal_ws(
            _Socket(**{"host": f"127.0.0.1:{PORT}", INTERNAL_TOKEN_HEADER: "x" * 43}), PORT
        )

    def test_browser_is_refused_even_holding_the_token(self):
        # A browser always sends Origin; the activity worker never does.
        token = internal_ws_token()
        assert not authorize_internal_ws(
            _Socket(
                **{
                    "host": f"127.0.0.1:{PORT}",
                    "origin": "http://evil.example",
                    INTERNAL_TOKEN_HEADER: token or "",
                }
            ),
            PORT,
        )

    def test_rebound_host_is_refused_even_with_token(self):
        token = internal_ws_token()
        assert not authorize_internal_ws(
            _Socket(**{"host": f"evil.example:{PORT}", INTERNAL_TOKEN_HEADER: token or ""}), PORT
        )
