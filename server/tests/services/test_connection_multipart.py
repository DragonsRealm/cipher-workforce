"""Contract for ``Connection.request(files=...)`` multipart support.

``Connection`` is the authed HTTP facade every REST plugin uses. It
originally accepted only ``headers`` / ``params`` / ``json`` / ``data``,
which meant any plugin needing a file upload had to bypass it and hand-roll
credential resolution — losing the auth-retry and the structured
missing-credential envelope. ``files`` closes that gap generically; the
Sarvam speech-to-text node is the first consumer.

The replay assertion is the load-bearing one: the auth-retry path rebuilds
the request kwargs from scratch, so ``files`` must be threaded into BOTH
branches or a 401 would silently retry with an empty body.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from services.plugin.credential import ApiKeyCredential
from services.plugin.connection import Connection


class _StubCredential(ApiKeyCredential):
    id = "multipart_stub"
    display_name = "Multipart Stub"
    category = "Test"
    key_name = "X-Stub-Key"
    key_location = "header"

    _resolve_calls = 0

    @classmethod
    async def resolve(cls, *, user_id: str = "owner"):
        cls._resolve_calls += 1
        return {"api_key": f"key-{cls._resolve_calls}"}


URL = "https://upload.example.com/v1/transcribe"


@pytest.fixture(autouse=True)
def _reset_stub():
    _StubCredential._resolve_calls = 0
    yield


@respx.mock
async def test_files_reach_httpx_as_multipart():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    async with Connection(_StubCredential) as conn:
        response = await conn.post(
            URL,
            files={"file": ("clip.wav", b"RIFFDATA", "audio/wav")},
            data={"model": "saaras:v3"},
        )

    assert response.status_code == 200
    sent = respx.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"RIFFDATA" in sent.content
    assert b'name="model"' in sent.content
    # Auth still injected by the credential class, not the caller.
    assert sent.headers["X-Stub-Key"] == "key-1"


@respx.mock
async def test_auth_retry_replays_the_file_payload():
    """A 401 must re-send the same parts, not an empty body."""
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with Connection(_StubCredential) as conn:
        response = await conn.post(
            URL,
            files={"file": ("clip.wav", b"RIFFDATA", "audio/wav")},
            data={"model": "saaras:v3"},
        )

    assert response.status_code == 200
    assert len(respx.calls) == 2

    retry = respx.calls[1].request
    assert b"RIFFDATA" in retry.content
    assert b'name="model"' in retry.content
    # Credentials were re-resolved, so the retry carries the fresh key.
    assert retry.headers["X-Stub-Key"] == "key-2"


@respx.mock
async def test_absent_files_is_unchanged_json_behaviour():
    """The new kwarg must not perturb the existing JSON path."""
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    async with Connection(_StubCredential) as conn:
        await conn.post(URL, json={"hello": "world"})

    sent = respx.calls.last.request
    assert sent.headers["content-type"] == "application/json"
    assert b'"hello"' in sent.content
