"""Contracts for the vision-delegate tool: budget fitting math, locked
schema, and the exact outgoing request per provider (the speech-test idiom
— assert the request, not the parsed result)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from nodes.vision.vision_analyze import (
    VisionAnalyzeNode,
    VisionAnalyzeParams,
    VisionAnalyzeToolInput,
)
from services.media.image_fit import (
    IMAGE_BUDGET_TOKENS,
    budget_to_pixels,
    fit_image_bytes,
    smart_resize,
)
from services.plugin import NodeContext, NodeUserError


class TestSmartResize:
    def test_aspect_ratio_preserved_and_snapped(self):
        height, width = smart_resize(
            2160, 3840, min_pixels=256 * 28 * 28, max_pixels=1024 * 28 * 28
        )
        assert height % 28 == 0 and width % 28 == 0
        assert height * width <= 1024 * 28 * 28 * 1.05
        original_ratio = 3840 / 2160
        assert abs((width / height) - original_ratio) / original_ratio < 0.1

    def test_tiny_image_scales_up(self):
        height, width = smart_resize(
            10, 10, min_pixels=256 * 28 * 28, max_pixels=1024 * 28 * 28
        )
        assert height * width >= 256 * 28 * 28 * 0.9

    def test_in_window_image_only_snaps(self):
        height, width = smart_resize(
            560, 560, min_pixels=28 * 28, max_pixels=2048 * 28 * 28
        )
        assert (height, width) == (560, 560)

    def test_invalid_dims_raise(self):
        with pytest.raises(ValueError):
            smart_resize(0, 100, min_pixels=1, max_pixels=100)


class TestFitImageBytes:
    @staticmethod
    def _png(size, mode="RGB") -> bytes:
        buffer = io.BytesIO()
        Image.new(mode, size).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_opaque_becomes_bounded_jpeg(self):
        fitted, mime, (width, height) = fit_image_bytes(
            self._png((4000, 3000)), budget="normal"
        )
        assert mime == "image/jpeg"
        assert width % 28 == 0 and height % 28 == 0
        assert width * height <= budget_to_pixels("normal") * 1.05
        assert len(fitted) < 2 * 1024 * 1024

    def test_alpha_stays_png(self):
        _, mime, _ = fit_image_bytes(self._png((100, 100), "RGBA"))
        assert mime == "image/png"

    def test_budgets_are_ordered(self):
        assert (
            IMAGE_BUDGET_TOKENS["small"]
            < IMAGE_BUDGET_TOKENS["normal"]
            < IMAGE_BUDGET_TOKENS["large"]
        )


class TestSpecInvariants:
    def test_locked_split_schema(self):
        assert VisionAnalyzeNode.tool_schema_locked is True
        assert VisionAnalyzeNode.tool_name == "vision"
        assert VisionAnalyzeNode.Params is VisionAnalyzeParams
        assert VisionAnalyzeNode.ToolInput is VisionAnalyzeToolInput
        schema = VisionAnalyzeNode.as_tool_schema()
        assert schema["name"] == "vision"
        properties = schema["parameters"]["properties"]
        assert "provider" not in properties
        assert "vision_model" not in properties
        assert set(properties["operation"]["enum"]) == {
            "describe",
            "extract_text",
        }

    def test_tool_input_validation(self):
        with pytest.raises(ValidationError):
            VisionAnalyzeToolInput(operation="describe", image="")
        with pytest.raises(ValidationError):
            VisionAnalyzeToolInput(operation="describe", image="a.png", extra=1)

    def test_no_model_field_name(self):
        # Reserved sibling-name magic in ParameterRenderer wipes a field
        # literally named `model` next to `provider`.
        assert "model" not in VisionAnalyzeParams.model_fields
        assert "vision_model" in VisionAnalyzeParams.model_fields


def _ctx(tmp_path: Path) -> NodeContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 480), color=(200, 40, 40)).save(
        workspace / "chart.png"
    )
    return NodeContext(
        node_id="vision-1",
        node_type="visionAnalyze",
        workflow_id="wf-vision",
        workspace_dir=str(workspace),
    )


@pytest.fixture
def no_api_key_lookup(monkeypatch):
    async def fake_key(ctx, provider):
        return "test-key"

    monkeypatch.setattr("nodes.vision.vision_analyze._api_key", fake_key)


class TestProviderRequestShapes:
    async def test_openai_request_shape(
        self, tmp_path, monkeypatch, no_api_key_lookup
    ):
        captured = {}

        class FakeCompletions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="a red chart")
                        )
                    ]
                )

        class FakeClient:
            def __init__(self, api_key):
                captured["api_key"] = api_key
                self.chat = SimpleNamespace(completions=FakeCompletions())

        monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)
        node = VisionAnalyzeNode()
        ctx = _ctx(tmp_path)
        ctx.raw["_tool_config"] = VisionAnalyzeParams(provider="openai")
        result = await node.vision(
            ctx,
            VisionAnalyzeToolInput(operation="describe", image="chart.png"),
        )
        assert result.text == "a red chart"
        assert captured["api_key"] == "test-key"
        content = captured["messages"][0]["content"]
        assert content[0] == {
            "type": "text",
            "text": "Describe this image in detail.",
        }
        image_part = content[1]
        assert image_part["type"] == "image_url"
        assert image_part["image_url"]["detail"] == "auto"
        assert image_part["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        assert captured["max_completion_tokens"] == 1024
        # The result carries text and metadata only — never image bytes.
        serialized = json.dumps(result.model_dump(mode="json"))
        assert "base64" not in serialized

    async def test_anthropic_request_shape(
        self, tmp_path, monkeypatch, no_api_key_lookup
    ):
        captured = {}

        class FakeMessages:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="chart text")]
                )

        class FakeClient:
            def __init__(self, api_key):
                captured["api_key"] = api_key
                self.messages = FakeMessages()

        monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
        node = VisionAnalyzeNode()
        ctx = _ctx(tmp_path)
        ctx.raw["_tool_config"] = VisionAnalyzeParams(
            provider="anthropic", vision_model="claude-sonnet-5"
        )
        result = await node.vision(
            ctx,
            VisionAnalyzeToolInput(
                operation="extract_text", image="chart.png"
            ),
        )
        assert result.text == "chart text"
        assert result.vision_model == "claude-sonnet-5"
        content = captured["messages"][0]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["source"]["type"] == "base64"
        assert content[0]["source"]["media_type"] == "image/jpeg"
        assert "Extract all text" in content[1]["text"]

    async def test_gemini_request_shape(
        self, tmp_path, monkeypatch, no_api_key_lookup
    ):
        captured = {}

        class FakeModels:
            async def generate_content(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(text="gemini sees a chart")

        class FakeClient:
            def __init__(self, api_key):
                captured["api_key"] = api_key
                self.aio = SimpleNamespace(models=FakeModels())

        monkeypatch.setattr("google.genai.Client", FakeClient)
        node = VisionAnalyzeNode()
        ctx = _ctx(tmp_path)
        ctx.raw["_tool_config"] = VisionAnalyzeParams(
            provider="gemini", vision_model="gemini-3.6-flash"
        )
        result = await node.vision(
            ctx,
            VisionAnalyzeToolInput(
                operation="describe",
                image="chart.png",
                question="What color is it?",
            ),
        )
        assert result.text == "gemini sees a chart"
        assert captured["model"] == "gemini-3.6-flash"
        # Custom question wins over the canned prompt.
        assert captured["contents"][1] == "What color is it?"


class TestSafety:
    async def test_traversal_rejected(self, tmp_path, no_api_key_lookup):
        node = VisionAnalyzeNode()
        ctx = _ctx(tmp_path)
        ctx.raw["_tool_config"] = VisionAnalyzeParams(provider="openai")
        with pytest.raises(NodeUserError):
            await node.vision(
                ctx,
                VisionAnalyzeToolInput(
                    operation="describe", image="../../credentials.db"
                ),
            )

    async def test_non_image_rejected(self, tmp_path, no_api_key_lookup):
        node = VisionAnalyzeNode()
        ctx = _ctx(tmp_path)
        (Path(ctx.workspace_dir) / "notes.txt").write_text("hello")
        ctx.raw["_tool_config"] = VisionAnalyzeParams(provider="openai")
        with pytest.raises(NodeUserError, match="not a readable image"):
            await node.vision(
                ctx,
                VisionAnalyzeToolInput(
                    operation="describe", image="notes.txt"
                ),
            )

    async def test_direct_run_is_refused(self, tmp_path):
        node = VisionAnalyzeNode()
        ctx = _ctx(tmp_path)
        with pytest.raises(NodeUserError, match="needs an image"):
            await node.vision(ctx, VisionAnalyzeParams())
