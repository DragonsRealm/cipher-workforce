"""Image-block contracts: never-bytes codec rule, llm_media parsing,
capability-gated hydration, and the Anthropic tool_result wire shape."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PILImage

from services.llm.media import (
    LLM_MEDIA_MAX_PER_RESULT,
    hydrate_image_blocks,
    image_blocks_from_tool_result,
    provider_supports_vision,
)
from services.llm.protocol import (
    ContentBlock,
    Message,
    message_from_wire,
    message_to_wire,
)


def _ref(path: str = "images/chart.png", mime: str = "image/png") -> dict:
    return {
        "kind": "image",
        "path": path,
        "workflow_id": "wf-media",
        "filename": Path(path).name,
        "mime_type": mime,
        "size_bytes": 1234,
    }


def _ref_block(**overrides) -> ContentBlock:
    source = {"kind": "file_ref", "ref": _ref(), "detail": "auto"}
    source.update(overrides)
    return ContentBlock(type="image", source=source)


class TestWireCodec:
    def test_ref_source_round_trips(self):
        message = Message(role="tool", content="{}", tool_call_id="t1")
        message.blocks.append(_ref_block())
        wire = message_to_wire(message)
        restored = message_from_wire(wire)
        images = [b for b in restored.blocks if b.type == "image"]
        assert images[0].source["kind"] == "file_ref"
        assert images[0].source["ref"]["path"] == "images/chart.png"

    def test_bytes_source_refuses_serialization(self):
        message = Message(role="tool", content="{}", tool_call_id="t1")
        message.blocks.append(
            ContentBlock(
                type="image",
                source={"kind": "bytes", "media_type": "image/png", "data_b64": "AAAA"},
            )
        )
        with pytest.raises(ValueError, match="durable state"):
            message_to_wire(message)

    def test_legacy_wire_without_source_decodes(self):
        wire = message_to_wire(Message(role="user", content="hi"))
        for block in wire["blocks"]:
            block.pop("source", None)
        assert message_from_wire(wire).content == "hi"


class TestLlmMediaContract:
    def test_valid_entry_becomes_ref_block(self):
        blocks = image_blocks_from_tool_result(
            {"llm_media": [{"ref": _ref(), "detail": "high"}]}
        )
        assert len(blocks) == 1
        assert blocks[0].source["detail"] == "high"

    def test_invalid_entries_skipped(self):
        result = {
            "llm_media": [
                {"ref": _ref(mime="application/pdf")},  # mime not allowed
                {"ref": {**_ref(), "workflow_id": ""}},  # scope missing
                {"no_ref": True},
                "not-a-dict",
            ]
        }
        assert image_blocks_from_tool_result(result) == []

    def test_cap_enforced(self):
        entries = [{"ref": _ref(path=f"i/{n}.png")} for n in range(20)]
        blocks = image_blocks_from_tool_result({"llm_media": entries})
        assert len(blocks) == LLM_MEDIA_MAX_PER_RESULT

    def test_non_dict_results_ignored(self):
        assert image_blocks_from_tool_result("text") == []
        assert image_blocks_from_tool_result({"result": 1}) == []


class TestHydration:
    async def test_identity_when_no_images(self):
        messages = [Message(role="user", content="hi")]
        hydrated = await hydrate_image_blocks(
            messages, provider="anthropic", model="claude-opus-5"
        )
        assert hydrated[0] is messages[0]

    async def test_incapable_provider_degrades_to_text(self):
        message = Message(role="tool", content="{}", tool_call_id="t1")
        message.blocks.append(_ref_block())
        hydrated = await hydrate_image_blocks(
            [message], provider="deepseek", model="deepseek-chat"
        )
        texts = [b.text for b in hydrated[0].blocks if b.type == "text"]
        assert any("cannot view images" in t for t in texts)
        assert all(b.type != "image" for b in hydrated[0].blocks)
        # Original untouched — refs remain the durable form.
        assert any(b.type == "image" for b in message.blocks)

    async def test_capable_provider_gets_bytes(self, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        (workspace / "images").mkdir(parents=True)
        PILImage.new("RGB", (64, 64)).save(workspace / "images" / "chart.png")

        async def fake_root(workflow_id, database, **kwargs):
            return workspace

        monkeypatch.setattr(
            "services.workspace_locator.resolve_workspace_root", fake_root
        )
        monkeypatch.setattr(
            "services.plugin.deps.get_database", lambda: object()
        )
        message = Message(role="tool", content="{}", tool_call_id="t1")
        message.blocks.append(_ref_block())
        hydrated = await hydrate_image_blocks(
            [message], provider="anthropic", model="claude-opus-5"
        )
        images = [b for b in hydrated[0].blocks if b.type == "image"]
        assert images[0].source["kind"] == "bytes"
        assert images[0].source["media_type"] in ("image/jpeg", "image/png")
        assert images[0].source["data_b64"]
        # The hydrated copy must never serialize.
        with pytest.raises(ValueError):
            message_to_wire(hydrated[0])
        # And the original still does.
        assert message_to_wire(message)

    async def test_missing_file_degrades_to_placeholder(self, monkeypatch):
        async def fake_root(workflow_id, database, **kwargs):
            return Path("Z:/nonexistent")

        monkeypatch.setattr(
            "services.workspace_locator.resolve_workspace_root", fake_root
        )
        monkeypatch.setattr(
            "services.plugin.deps.get_database", lambda: object()
        )
        message = Message(role="tool", content="{}", tool_call_id="t1")
        message.blocks.append(_ref_block())
        hydrated = await hydrate_image_blocks(
            [message], provider="anthropic", model="claude-opus-5"
        )
        texts = [b.text for b in hydrated[0].blocks if b.type == "text"]
        assert any("unavailable" in t for t in texts)


class TestCapabilityGate:
    def test_anthropic_enabled(self):
        # The only provider whose request encoder renders image blocks today.
        assert provider_supports_vision("anthropic") is True

    def test_encoderless_providers_disabled(self):
        # openai/gemini stay off until their encoders render image blocks —
        # enabled-without-encoder would hydrate bytes that get dropped.
        for provider in ("openai", "gemini", "deepseek", "groq", "nope"):
            assert provider_supports_vision(provider) is False


class TestAnthropicToolResultShape:
    def test_hydrated_image_rides_tool_result(self):
        from services.llm.providers.anthropic import AnthropicProvider

        message = Message(role="tool", content='{"ok":1}', tool_call_id="t1")
        message.blocks.append(
            ContentBlock(
                type="image",
                source={
                    "kind": "bytes",
                    "media_type": "image/jpeg",
                    "data_b64": "QUJD",
                },
            )
        )
        content = AnthropicProvider._tool_result_content(message)
        assert content[0] == {"type": "text", "text": '{"ok":1}'}
        assert content[1] == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "QUJD",
            },
        }

    def test_plain_tool_result_stays_string(self):
        from services.llm.providers.anthropic import AnthropicProvider

        message = Message(role="tool", content='{"ok":1}', tool_call_id="t1")
        assert AnthropicProvider._tool_result_content(message) == '{"ok":1}'
