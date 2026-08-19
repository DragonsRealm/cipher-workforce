"""Screenshot persistence helpers shared by the browser plugins.

Tolerance is the contract: unrecognized payload shapes pass through
(``(None, None)``), and persistence failures never raise into the browser
operation. The harness path helper is containment-checked — printed process
output is not a licence to read arbitrary files.
"""

from __future__ import annotations

import base64

from nodes.browser._screenshots import (
    persist_screenshot_file,
    persist_screenshot_from_payload,
)
from services.plugin import NodeContext


def _ctx(tmp_path, workflow_id="wf-shot") -> NodeContext:
    return NodeContext(
        node_id="browser-1",
        node_type="browser",
        workflow_id=workflow_id,
        workspace_dir=str(tmp_path),
    )


def _fake_png(size: int = 1024) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"x" * size


def test_base64_payload_is_persisted_and_bulky_key_named(tmp_path):
    payload = {"success": True, "base64": base64.b64encode(_fake_png()).decode()}

    ref, consumed = persist_screenshot_from_payload(payload, _ctx(tmp_path))

    assert consumed == "base64"
    assert ref is not None
    assert ref["kind"] == "image"
    assert ref["mime_type"] == "image/png"
    assert ref["path"].startswith("media/screenshot-")
    stored = tmp_path / ref["path"]
    assert stored.is_file()
    assert stored.read_bytes() == _fake_png()


def test_jpeg_format_is_respected(tmp_path):
    payload = {"base64": base64.b64encode(_fake_png()).decode()}
    ref, _ = persist_screenshot_from_payload(payload, _ctx(tmp_path), fmt="jpeg")
    assert ref is not None
    assert ref["mime_type"] == "image/jpeg"
    assert ref["path"].endswith(".jpg")


def test_unrecognized_shapes_pass_through(tmp_path):
    ctx = _ctx(tmp_path)
    assert persist_screenshot_from_payload("not a dict", ctx) == (None, None)
    assert persist_screenshot_from_payload({"output": "plain text"}, ctx) == (None, None)
    # Short strings and invalid base64 are skipped, not errors.
    assert persist_screenshot_from_payload({"base64": "AAAA"}, ctx) == (None, None)
    assert persist_screenshot_from_payload(
        {"base64": "!" * 400}, ctx
    ) == (None, None)


def test_tiny_decoded_payload_is_refused(tmp_path):
    payload = {"base64": base64.b64encode(b"tiny").decode().ljust(300, "=")}
    assert persist_screenshot_from_payload(payload, _ctx(tmp_path)) == (None, None)


def test_missing_workspace_never_raises(tmp_path):
    ctx = NodeContext(node_id="n", node_type="browser", workflow_id="wf")
    payload = {"base64": base64.b64encode(_fake_png()).decode()}
    assert persist_screenshot_from_payload(payload, ctx) == (None, None)


def test_saved_file_path_form(tmp_path):
    source = tmp_path / "cache"
    source.mkdir()
    shot = source / "shot.png"
    shot.write_bytes(_fake_png())
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ref, consumed = persist_screenshot_from_payload(
        {"path": str(shot)}, _ctx(workspace)
    )
    assert consumed is None
    assert ref is not None
    assert (workspace / ref["path"]).is_file()


def test_harness_file_is_contained_under_runtime_dir(tmp_path):
    runtime = tmp_path / "daemons" / "browser-harness"
    (runtime / "tmp").mkdir(parents=True)
    shot = runtime / "tmp" / "shot-1.png"
    shot.write_bytes(_fake_png())
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ref = persist_screenshot_file(str(shot), _ctx(workspace), contained_under=runtime)
    assert ref is not None
    assert ref["kind"] == "image"

    # Outside the harness dir: refused, never read.
    outside = tmp_path / "credentials.db"
    outside.write_bytes(_fake_png())
    assert (
        persist_screenshot_file(str(outside), _ctx(workspace), contained_under=runtime)
        is None
    )
    # Non-image suffixes are refused even when contained.
    text = runtime / "tmp" / "notes.txt"
    text.write_text("hi")
    assert (
        persist_screenshot_file(str(text), _ctx(workspace), contained_under=runtime)
        is None
    )
