"""Contract for the workspace file routes.

Mounted on a bare FastAPI app rather than importing ``main`` — the app's
lifespan starts the container, the database and the Temporal runtime, none
of which this router needs. The router is the unit under test.

The security assertions here are not defensive padding. ``shell``,
``fileDownloader`` and ``fileModify`` can all write arbitrary files into a
workspace, so this route serves attacker-influenced content from the app
origin. Inline HTML would be stored XSS with session-cookie access.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from routers import workspace as workspace_router

pytestmark = pytest.mark.unit

SLUG = "My_Workflow_1"
WORKFLOW_ID = "019f99e2dc997cf390193ad3e6260de1"


def _wav(seconds: float = 0.25, rate: int = 8000) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspaces root containing one workflow directory, keyed by SLUG."""
    root = tmp_path / "workspaces"
    (root / SLUG / "audio").mkdir(parents=True)
    monkeypatch.setattr(workspace_router, "workspaces_dir", lambda: root)
    return root / SLUG


@pytest.fixture
def client(workspace):
    app = FastAPI()
    app.include_router(workspace_router.router)

    database = AsyncMock()
    database.get_workflow.return_value = SimpleNamespace(slug=SLUG)
    app.dependency_overrides[workspace_router._db] = lambda: database

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestServeFile:
    async def test_serves_audio_inline(self, client, workspace):
        payload = _wav()
        (workspace / "audio" / "clip.wav").write_bytes(payload)

        async with client as http:
            response = await http.get(
                f"/api/workspace/{WORKFLOW_ID}/files/audio/clip.wav"
            )

        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"].startswith("audio/")
        assert response.headers["content-disposition"].startswith("inline")
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_url_carries_the_id_while_the_directory_uses_the_slug(
        self, client, workspace
    ):
        """The whole point of the id/slug split: refs survive a rename."""
        (workspace / "audio" / "clip.wav").write_bytes(_wav())

        async with client as http:
            response = await http.get(
                f"/api/workspace/{WORKFLOW_ID}/files/audio/clip.wav"
            )

        assert response.status_code == 200
        assert WORKFLOW_ID not in str(workspace)
        assert workspace.name == SLUG

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("page.html", "attachment"),
            ("vector.svg", "attachment"),
            ("clip.wav", "inline"),
            ("cover.png", "inline"),
            ("notes.txt", "attachment"),
        ],
    )
    async def test_only_safe_media_renders_inline(
        self, client, workspace, name, expected
    ):
        """Script-bearing types must download, never render on our origin."""
        (workspace / name).write_bytes(b"<html><script>alert(1)</script></html>")

        async with client as http:
            response = await http.get(f"/api/workspace/{WORKFLOW_ID}/files/{name}")

        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith(expected)

    @pytest.mark.parametrize(
        "attack",
        [
            "../../../etc/passwd",
            "..%2f..%2fsecret.txt",
            "....//secret.txt",
        ],
    )
    async def test_traversal_is_refused_as_404(self, client, workspace, attack):
        (workspace.parent.parent / "secret.txt").write_bytes(b"SECRET")

        async with client as http:
            response = await http.get(f"/api/workspace/{WORKFLOW_ID}/files/{attack}")

        assert response.status_code == 404
        assert b"SECRET" not in response.content

    async def test_missing_file_is_404_not_403(self, client):
        """A distinct status would confirm what exists outside the workspace."""
        async with client as http:
            response = await http.get(
                f"/api/workspace/{WORKFLOW_ID}/files/audio/nope.wav"
            )

        assert response.status_code == 404

    async def test_range_requests_work_without_router_code(self, client, workspace):
        """Starlette's FileResponse already implements 206; seeking depends on it."""
        payload = _wav()
        (workspace / "audio" / "clip.wav").write_bytes(payload)

        async with client as http:
            response = await http.get(
                f"/api/workspace/{WORKFLOW_ID}/files/audio/clip.wav",
                headers={"Range": "bytes=0-99"},
            )

        assert response.status_code == 206
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-range"].startswith("bytes 0-99/")
        assert response.content == payload[:100]


class TestUpload:
    async def test_stores_the_file_and_returns_a_ref(self, client, workspace):
        payload = _wav()

        async with client as http:
            response = await http.post(
                f"/api/workspace/{WORKFLOW_ID}/uploads",
                files={"file": ("My Clip.wav", payload, "audio/wav")},
            )

        assert response.status_code == 200
        ref = response.json()
        assert ref["kind"] == "audio"
        assert ref["path"].startswith("uploads/")
        assert ref["size_bytes"] == len(payload)
        assert ref["workflow_id"] == WORKFLOW_ID
        assert (workspace / ref["path"]).read_bytes() == payload

    async def test_returned_ref_is_immediately_servable(self, client, workspace):
        async with client as http:
            ref = (
                await http.post(
                    f"/api/workspace/{WORKFLOW_ID}/uploads",
                    files={"file": ("clip.wav", _wav(), "audio/wav")},
                )
            ).json()

            # The ref's own url must round-trip through the GET route.
            response = await http.get(ref["url"])

        assert response.status_code == 200

    async def test_over_cap_upload_is_413(self, client, monkeypatch):
        monkeypatch.setattr(workspace_router, "MEDIA_MAX_UPLOAD_BYTES", 1024)

        async with client as http:
            response = await http.post(
                f"/api/workspace/{WORKFLOW_ID}/uploads",
                files={"file": ("big.wav", b"x" * 4096, "audio/wav")},
            )

        assert response.status_code == 413

    async def test_empty_upload_is_400(self, client):
        async with client as http:
            response = await http.post(
                f"/api/workspace/{WORKFLOW_ID}/uploads",
                files={"file": ("empty.wav", b"", "audio/wav")},
            )

        assert response.status_code == 400

    async def test_filename_is_sanitized_not_trusted(self, client, workspace):
        """A traversal filename must not escape the uploads directory."""
        async with client as http:
            response = await http.post(
                f"/api/workspace/{WORKFLOW_ID}/uploads",
                files={"file": ("../../evil.wav", _wav(), "audio/wav")},
            )

        assert response.status_code == 200
        stored = Path(response.json()["path"])
        assert stored.parts[0] == "uploads"
        assert ".." not in stored.parts
