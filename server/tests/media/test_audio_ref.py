"""Contract for services.media — references, never bytes.

The invariants here are not stylistic. Audio bytes inside a node result
are copied at least six ways by the engine (see services/media/limits.py),
and Temporal's 2 MiB blob limit turns that into a three-retry failure with
a generic error message. ``AudioRef`` is the type that makes it impossible.
"""

from __future__ import annotations

import base64
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.media import (
    AudioRef,
    coerce_file_param,
    inspect_audio,
    read_media_bytes,
    resolve_media,
    write_audio,
)
from services.media.limits import TEMPORAL_PAYLOAD_WARN_BYTES


pytestmark = pytest.mark.unit


def _ctx(tmp_path: Path, *, node_id: str = "tts-node-1", workflow_id: str = "wf-1"):
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


class TestAudioRefCannotCarryBytes:
    """D1 is structural, not a convention someone must remember."""

    def test_has_no_bytes_or_base64_field(self):
        forbidden = {"data", "bytes", "audio", "base64", "audio_base64", "content"}
        assert forbidden.isdisjoint(AudioRef.model_fields)

    def test_rejects_an_injected_payload_field(self):
        with pytest.raises(ValidationError):
            AudioRef(path="audio/a.wav", filename="a.wav", audio_base64="AAAA")

    def test_serializes_far_below_the_temporal_warning_threshold(self):
        import orjson

        ref = AudioRef(
            path="audio/some-fairly-long-generated-name-1a2b3c.wav",
            workflow_id="019f99e2-dc99-7cf3-9019-3ad3e6260de1",
            filename="some-fairly-long-generated-name-1a2b3c.wav",
            mime_type="audio/wav",
            format="wav",
            size_bytes=12_000_000,
            duration_seconds=312.5,
            sample_rate=44100,
            channels=2,
            sha256="a" * 64,
            url="/api/workspace/019f99e2/files/audio/some-name.wav",
        )
        size = len(orjson.dumps(ref.model_dump(mode="json")))
        assert size < 1024
        # The whole point: thousands of refs still fit inside one payload.
        assert TEMPORAL_PAYLOAD_WARN_BYTES // size > 500


class TestWriteAudio:
    def test_round_trips_through_the_workspace(self, tmp_path):
        ctx = _ctx(tmp_path)
        payload = _wav_bytes()

        ref = write_audio(payload, ctx=ctx, stem="Hello World!", ext="wav")

        assert ref.path.startswith("audio/")
        assert not Path(ref.path).is_absolute()
        assert ref.size_bytes == len(payload)
        # Not an exact match: mimetypes consults the Windows registry, so
        # WAV resolves to audio/wav on some machines and audio/x-wav on others.
        assert ref.mime_type.startswith("audio/")
        assert read_media_bytes(ref, ctx=ctx)[1] == payload

    def test_populates_duration_from_the_container(self, tmp_path):
        ref = write_audio(_wav_bytes(seconds=0.5, rate=8000), ctx=_ctx(tmp_path), stem="clip", ext="wav")
        assert ref.duration_seconds == pytest.approx(0.5, abs=0.05)
        assert ref.sample_rate == 8000

    def test_never_collides_across_runs(self, tmp_path):
        ctx = _ctx(tmp_path)
        first = write_audio(_wav_bytes(), ctx=ctx, stem="same", ext="wav")
        second = write_audio(_wav_bytes(), ctx=ctx, stem="same", ext="wav")
        assert first.path != second.path

    def test_url_is_path_only(self, tmp_path):
        """The frontend prepends its own base for remote backends."""
        ref = write_audio(_wav_bytes(), ctx=_ctx(tmp_path), stem="x", ext="wav")
        assert ref.url and ref.url.startswith("/api/workspace/")
        assert "://" not in ref.url

    def test_refuses_empty_payload(self, tmp_path):
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            write_audio(b"", ctx=_ctx(tmp_path), stem="x", ext="wav")


class TestContainment:
    """Regression: the Sarvam STT node read the credential store."""

    @pytest.mark.parametrize(
        "attack",
        ["../../credentials.db", "..\\..\\credentials.db", "~/secrets", "C:/Windows/win.ini"],
    )
    def test_traversal_is_refused(self, tmp_path, attack):
        from services.plugin import NodeUserError

        (tmp_path.parent / "credentials.db").write_bytes(b"SECRET")
        with pytest.raises(NodeUserError):
            resolve_media(attack, ctx=_ctx(tmp_path))

    def test_absolute_path_outside_the_workspace_is_refused(self, tmp_path):
        from services.plugin import NodeUserError

        outside = tmp_path.parent / "elsewhere.wav"
        outside.write_bytes(b"nope")
        with pytest.raises(NodeUserError):
            resolve_media(str(outside), ctx=_ctx(tmp_path))

    def test_absolute_path_inside_the_workspace_still_works(self, tmp_path):
        """Back-compat for nodes that stored absolute paths pre-AudioRef."""
        ctx = _ctx(tmp_path)
        ref = write_audio(_wav_bytes(), ctx=ctx, stem="x", ext="wav")
        absolute = tmp_path / ref.path
        assert read_media_bytes(str(absolute), ctx=ctx)[1] == read_media_bytes(ref, ctx=ctx)[1]

    def test_a_ref_cannot_reach_another_workflows_workspace(self, tmp_path):
        from services.plugin import NodeUserError

        alpha, beta = tmp_path / "alpha", tmp_path / "beta"
        alpha.mkdir(), beta.mkdir()
        ref = write_audio(_wav_bytes(), ctx=_ctx(alpha, workflow_id="alpha"), stem="x", ext="wav")

        with pytest.raises(NodeUserError):
            read_media_bytes(ref, ctx=_ctx(beta, workflow_id="beta"))


class TestWorkspaceRootRefusesToGuess:
    """Regression: a workflow_id is not a workspace directory name.

    Directories are named by ``Workflow.slug``, which changes on rename,
    while an ``AudioRef`` carries the immutable ``workflow_id`` on purpose.
    Composing ``workspaces/<workflow_id>/`` produced a path that never
    exists. It stayed invisible because every caller happened to pass a
    ctx; the workspace HTTP route is the first that cannot.
    """

    def test_workflow_id_alone_is_refused(self):
        from services.media.workspace import workspace_root
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            workspace_root(workflow_id="019f99e2-dc99-7cf3-9019-3ad3e6260de1")

    def test_no_ctx_and_no_hint_is_refused(self):
        from services.media.workspace import workspace_root
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            workspace_root()

    def test_explicit_workspace_dir_wins(self, tmp_path):
        """How the HTTP route passes a directory it resolved via the DB."""
        from services.media.workspace import workspace_root

        assert workspace_root(workspace_dir=str(tmp_path)) == tmp_path

    def test_explicit_dir_lets_a_ref_resolve_without_a_ctx(self, tmp_path):
        ctx = _ctx(tmp_path)
        ref = write_audio(_wav_bytes(), ctx=ctx, stem="x", ext="wav")

        name, blob = read_media_bytes(ref, workspace_dir=str(tmp_path))
        assert blob == read_media_bytes(ref, ctx=ctx)[1]
        assert name.endswith(".wav")


class TestCoerceFileParam:
    def test_accepts_an_audio_ref(self, tmp_path):
        ctx = _ctx(tmp_path)
        payload = _wav_bytes()
        ref = write_audio(payload, ctx=ctx, stem="x", ext="wav")

        name, blob = coerce_file_param(ref.model_dump(mode="json"), ctx=ctx)
        assert blob == payload
        assert name.endswith(".wav")

    def test_accepts_the_legacy_base64_envelope(self, tmp_path, caplog):
        """Saved workflow rows still carry this shape and are not migrated."""
        payload = b"RIFFfake"
        value = {
            "type": "upload",
            "data": base64.b64encode(payload).decode(),
            "filename": "voice.mp3",
            "mimeType": "audio/mpeg",
        }
        name, blob = coerce_file_param(value, ctx=_ctx(tmp_path))
        assert (name, blob) == ("voice.mp3", payload)

    def test_accepts_a_relative_path(self, tmp_path):
        ctx = _ctx(tmp_path)
        payload = _wav_bytes()
        ref = write_audio(payload, ctx=ctx, stem="x", ext="wav")
        assert coerce_file_param(ref.path, ctx=ctx)[1] == payload

    def test_rejects_oversize_legacy_upload(self, tmp_path):
        from services.plugin import NodeUserError

        value = {"type": "upload", "data": base64.b64encode(b"x" * 4096).decode()}
        with pytest.raises(NodeUserError, match="limit"):
            coerce_file_param(value, ctx=_ctx(tmp_path), max_bytes=1024)

    def test_rejects_empty_and_unknown_shapes(self, tmp_path):
        from services.plugin import NodeUserError

        for value in ("", None, {"kind": "video"}, {"type": "upload", "data": ""}):
            with pytest.raises(NodeUserError):
                coerce_file_param(value, ctx=_ctx(tmp_path))


class TestFileRefBase:
    """FileRef is the base every kind shares; AudioRef narrows it.

    The base exists because a file manager lists arbitrary files, most of
    which are not media at all, and ``AudioRef`` forbids extras so it cannot
    stand in for them.
    """

    def test_audio_ref_is_a_file_ref(self):
        from services.media.refs import FileRef

        ref = AudioRef(path="audio/x.wav", filename="x.wav")
        assert isinstance(ref, FileRef)

    def test_file_ref_structurally_cannot_carry_bytes(self):
        """Same invariant as AudioRef: adding a payload field must fail."""
        from services.media.refs import FileRef

        for field in ("data", "content", "bytes", "audio_base64"):
            with pytest.raises(ValidationError):
                FileRef.model_validate(
                    {"path": "a.bin", "filename": "a.bin", field: "QUJD"}
                )

    def test_audio_ref_keeps_its_field_set_after_the_refactor(self):
        """Guards the inheritance change against silently dropping a field."""
        keys = set(AudioRef(path="audio/x.wav", filename="x.wav").model_dump())
        assert {
            "kind", "path", "workflow_id", "filename", "mime_type", "size_bytes",
            "sha256", "url", "format", "duration_seconds", "sample_rate", "channels",
        } <= keys

    def test_audio_payload_does_not_validate_as_the_plain_base(self):
        """Why coerce_file_param must pick the model instead of always
        validating as FileRef: extra='forbid' rejects the audio-only fields."""
        from services.media.refs import FileRef

        payload = AudioRef(path="audio/x.wav", filename="x.wav").model_dump()
        with pytest.raises(ValidationError):
            FileRef.model_validate(payload)


class TestCoerceFileParamAcceptsFileRefs:
    def test_accepts_a_plain_file_ref(self, tmp_path):
        from services.media.refs import FileRef

        (tmp_path / "chart.png").write_bytes(b"PNGDATA")
        ref = FileRef(path="chart.png", filename="chart.png", mime_type="image/png")

        name, blob = coerce_file_param(ref.model_dump(), ctx=_ctx(tmp_path))

        assert name == "chart.png"
        assert blob == b"PNGDATA"

    def test_still_accepts_an_audio_ref(self, tmp_path):
        (tmp_path / "audio").mkdir(exist_ok=True)
        (tmp_path / "audio" / "x.wav").write_bytes(b"RIFFDATA")
        ref = AudioRef(path="audio/x.wav", filename="x.wav")

        assert coerce_file_param(ref.model_dump(), ctx=_ctx(tmp_path))[1] == b"RIFFDATA"

    def test_malformed_ref_is_a_user_error_not_a_validation_traceback(self, tmp_path):
        """A known kind with a broken body still belongs on the NodeUserError
        path — one WARN line, no pydantic traceback in the operator log."""
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            coerce_file_param({"kind": "image"}, ctx=_ctx(tmp_path))


class TestResolveEntryWithin:
    """Mutations must name the entry, not what a symlink points at."""

    def test_refuses_the_root_and_non_entries(self, tmp_path):
        from nodes.filesystem._backend import resolve_entry_within

        for key in ("", "/", "..", "../../etc/passwd", "~/x", "C:/Windows/x"):
            with pytest.raises(ValueError):
                resolve_entry_within(tmp_path, key)

    def test_contains_nested_paths(self, tmp_path):
        from nodes.filesystem._backend import resolve_entry_within

        target = resolve_entry_within(tmp_path, "sub/dir/file.txt")
        assert target.relative_to(tmp_path) == Path("sub/dir/file.txt")

    def test_names_the_link_not_its_target(self, tmp_path):
        """resolve_within would return the target, so deleting it would
        destroy the pointed-at file instead of removing the link."""
        import os

        from nodes.filesystem._backend import resolve_entry_within, resolve_within

        (tmp_path / "real.txt").write_text("IMPORTANT")
        try:
            os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this platform")

        assert resolve_within(tmp_path, "link.txt").name == "real.txt"

        entry = resolve_entry_within(tmp_path, "link.txt")
        assert entry.name == "link.txt"

        os.unlink(entry)
        assert not (tmp_path / "link.txt").exists()
        assert (tmp_path / "real.txt").read_text() == "IMPORTANT"


class TestInspectNeverRaises:
    """D8: a parser miss degrades billing, it does not fail a workflow."""

    def test_unknown_container_returns_an_empty_probe(self, tmp_path):
        junk = tmp_path / "mystery.xyz"
        junk.write_bytes(b"\x00\x01\x02not audio at all")
        probe = inspect_audio(junk, declared_format="xyz")
        assert probe.duration_seconds is None

    def test_missing_file_returns_an_empty_probe(self, tmp_path):
        assert inspect_audio(tmp_path / "nope.wav").duration_seconds is None

    def test_raw_pcm_duration_is_computed_arithmetically(self, tmp_path):
        raw = tmp_path / "raw.pcm"
        raw.write_bytes(b"\x00\x00" * 16000)          # 16k frames, 16-bit mono
        probe = inspect_audio(raw, declared_format="linear16", pcm_sample_rate=16000)
        assert probe.duration_seconds == pytest.approx(1.0)
