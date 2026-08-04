"""Contract for ``write_media`` — the kind-agnostic sibling of ``write_audio``.

``write_audio`` produces an ``AudioRef``, which *asserts* the container was
probed. Everything that is not audio needs the same containment, atomicity
and naming without that claim, because ``kind="audio"`` on an unprobed file
means a fabricated duration, and a fabricated duration silently mis-bills
per-second providers downstream.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.media import (
    AudioRef,
    FileRef,
    MEDIA_SUBDIR,
    read_media_bytes,
    resolve_media,
    write_audio,
    write_media,
)


pytestmark = pytest.mark.unit


def _ctx(tmp_path: Path, *, node_id: str = "wa-node-1", workflow_id: str = "wf-1"):
    return SimpleNamespace(
        node_id=node_id,
        workflow_id=workflow_id,
        workspace_dir=str(tmp_path),
        raw={"workspace_dir": str(tmp_path)},
    )


def _wav_bytes(seconds: float = 0.25, rate: int = 8000) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


class TestWriteMedia:
    def test_round_trips_through_the_workspace(self, tmp_path):
        ctx = _ctx(tmp_path)
        ref = write_media(b"%PDF-1.7 fake", ctx=ctx, stem="invoice", ext="pdf", kind="document")

        assert isinstance(ref, FileRef)
        assert ref.kind == "document"
        assert ref.path.startswith(f"{MEDIA_SUBDIR}/")
        assert not ref.path.startswith("/")
        name, blob = read_media_bytes(ref, ctx=ctx)
        assert blob == b"%PDF-1.7 fake"
        assert name == ref.filename

    def test_defaults_to_the_honest_kind(self, tmp_path):
        """``file`` asserts nothing beyond existence, so it is the default."""
        ref = write_media(b"data", ctx=_ctx(tmp_path), stem="x", ext="bin")
        assert ref.kind == "file"

    @pytest.mark.parametrize("kind", ["file", "image", "video", "document"])
    def test_accepts_every_non_audio_kind(self, tmp_path, kind):
        ref = write_media(b"data", ctx=_ctx(tmp_path), stem="x", ext="bin", kind=kind)
        assert ref.kind == kind

    def test_refuses_to_claim_audio(self, tmp_path):
        """The whole point of the split.

        ``kind="audio"`` means the duration/rate fields came from a real
        probe. Letting a caller assert it here would reintroduce exactly the
        mis-billing ``AudioRef`` exists to prevent, so it is refused rather
        than silently downgraded.
        """
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError, match="write_audio"):
            write_media(_wav_bytes(), ctx=_ctx(tmp_path), stem="clip", ext="wav", kind="audio")

    def test_carries_no_probe_fields(self, tmp_path):
        """A FileRef has no duration to fabricate -- structurally."""
        ref = write_media(_wav_bytes(), ctx=_ctx(tmp_path), stem="clip", ext="wav")
        assert not hasattr(ref, "duration_seconds")
        assert "duration_seconds" not in ref.model_dump()

    def test_never_collides_across_runs(self, tmp_path):
        ctx = _ctx(tmp_path)
        first = write_media(b"a", ctx=ctx, stem="same", ext="bin")
        second = write_media(b"b", ctx=ctx, stem="same", ext="bin")
        assert first.path != second.path
        assert read_media_bytes(first, ctx=ctx)[1] == b"a"
        assert read_media_bytes(second, ctx=ctx)[1] == b"b"

    def test_url_is_path_only(self, tmp_path):
        ref = write_media(b"data", ctx=_ctx(tmp_path), stem="x", ext="png", kind="image")
        assert ref.url == f"/api/workspace/wf-1/files/{ref.path}"
        assert "://" not in ref.url

    def test_mime_is_guessed_but_overridable(self, tmp_path):
        guessed = write_media(b"data", ctx=_ctx(tmp_path), stem="x", ext="png", kind="image")
        assert guessed.mime_type == "image/png"

        declared = write_media(
            b"data", ctx=_ctx(tmp_path), stem="x", ext="bin", mime_type="application/vnd.custom"
        )
        assert declared.mime_type == "application/vnd.custom"

    def test_unknown_extension_falls_back_to_octet_stream(self, tmp_path):
        ref = write_media(b"data", ctx=_ctx(tmp_path), stem="x", ext="zzzz")
        assert ref.mime_type == "application/octet-stream"

    def test_refuses_empty_payload(self, tmp_path):
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            write_media(b"", ctx=_ctx(tmp_path), stem="x", ext="bin")

    def test_sha256_matches_payload(self, tmp_path):
        import hashlib

        payload = b"some bytes"
        ref = write_media(payload, ctx=_ctx(tmp_path), stem="x", ext="bin")
        assert ref.sha256 == hashlib.sha256(payload).hexdigest()
        assert ref.size_bytes == len(payload)


class TestContainmentIsShared:
    """write_media must inherit the same containment as write_audio."""

    def test_a_ref_cannot_reach_another_workflows_workspace(self, tmp_path):
        alpha, beta = tmp_path / "alpha", tmp_path / "beta"
        alpha.mkdir()
        beta.mkdir()

        ref = write_media(b"data", ctx=_ctx(alpha, workflow_id="alpha"), stem="x", ext="bin")
        with pytest.raises(Exception):
            read_media_bytes(ref, ctx=_ctx(beta, workflow_id="beta"))

    def test_traversing_stem_cannot_escape(self, tmp_path):
        """The stem is slugified, so separators never survive into the path."""
        ref = write_media(b"data", ctx=_ctx(tmp_path), stem="../../etc/passwd", ext="bin")
        assert ".." not in ref.path
        assert ref.path.startswith(f"{MEDIA_SUBDIR}/")
        resolved = resolve_media(ref, ctx=_ctx(tmp_path))
        assert tmp_path in resolved.parents


class TestWriteAudioUnchanged:
    """The refactor must not alter write_audio's observable behaviour."""

    def test_still_returns_an_audio_ref_with_a_measured_duration(self, tmp_path):
        ref = write_audio(_wav_bytes(seconds=0.5, rate=8000), ctx=_ctx(tmp_path), stem="clip", ext="wav")
        assert isinstance(ref, AudioRef)
        assert ref.kind == "audio"
        assert ref.format == "wav"
        assert ref.duration_seconds == pytest.approx(0.5, abs=0.05)
        assert ref.sample_rate == 8000

    def test_still_defaults_to_the_audio_subdir(self, tmp_path):
        ref = write_audio(_wav_bytes(), ctx=_ctx(tmp_path), stem="clip", ext="wav")
        assert ref.path.startswith("audio/")

    def test_still_refuses_empty_payload(self, tmp_path):
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError, match="empty audio"):
            write_audio(b"", ctx=_ctx(tmp_path), stem="x", ext="wav")
