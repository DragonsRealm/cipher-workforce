"""Vision Analyze — the vision-delegate AI tool.

Gives EVERY agent — including ones running text-only models — real image
understanding: the tool loads a workspace image at the provider boundary
(never carrying bytes in results), fits it to a visual-token budget, sends
it to a vision-capable model, and returns text.

This is deliberately the speech-node shape, not a ChatUnifier call: the
unifier's message protocol is text-only today, and this node must work
regardless of the host agent's provider. When native image blocks land in
the provider layer, this tool remains the fallback rung for text-only host
models and the specialist path (OCR-style extraction).

Token cost/billing note: like the LLM-backed translate providers, usage is
deliberately NOT recorded via the pricing service — this path bills
provider tokens that the LLM pricing layer has no per-call meter for here.
"""

from __future__ import annotations

import base64
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.plugin import (
    NodeContext,
    NodeUserError,
    Operation,
    TaskQueue,
    ToolNode,
)

VisionProvider = Literal["openai", "anthropic", "gemini"]
VisionOperation = Literal["describe", "extract_text"]
VisionBudget = Literal["small", "normal", "large"]

_MAX_OUTPUT_TOKENS = 1024

_PROMPTS = {
    "describe": "Describe this image in detail.",
    "extract_text": (
        "Extract all text visible in this image. Return only the text, "
        "preserving reading order and line breaks."
    ),
}


class VisionAnalyzeParams(BaseModel):
    """Persisted operator configuration; never exposed as model arguments."""

    provider: VisionProvider = Field(
        default="openai",
        description="Which vision-capable provider answers the delegate call.",
    )
    # NOT named `model`: with a sibling `provider` field the parameter panel
    # would overwrite it with the chat-model list (reserved-name magic in
    # ParameterRenderer.tsx).
    vision_model: str = Field(
        default="",
        description=(
            "Vision model id. Empty uses the provider's default model from "
            "llm_defaults.json."
        ),
    )

    model_config = ConfigDict(extra="ignore")


class VisionAnalyzeToolInput(BaseModel):
    """One locked schema visible to the LLM."""

    operation: VisionOperation = Field(
        description="describe the image, or extract_text (OCR-style)."
    )
    image: str = Field(
        min_length=1,
        max_length=4_096,
        description=(
            "Workspace-relative path to the image (e.g. imports/chart.png). "
            "Use the data tool to discover files."
        ),
    )
    question: Optional[str] = Field(
        default=None,
        max_length=4_000,
        description="describe: an optional specific question about the image.",
    )
    budget: VisionBudget = Field(
        default="normal",
        description=(
            "Visual-token budget: small (~256 tokens), normal (~1024), "
            "large (~2048 — fine detail)."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _strip(self) -> "VisionAnalyzeToolInput":
        if not self.image.strip():
            raise ValueError("image path required")
        return self


class VisionAnalyzeOutput(BaseModel):
    operation: str
    provider: str
    vision_model: str
    text: str
    image: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


async def _api_key(ctx: NodeContext, provider: str) -> str:
    """Resolve the stored key through the provider's credential (the speech
    idiom) so a missing key surfaces as the annotated credential envelope."""
    async with ctx.connection(provider) as conn:
        secrets = await conn.credentials()
    api_key = str(secrets.get("api_key") or "")
    if not api_key:
        raise NodeUserError(
            f"No API key stored for '{provider}'. Add one in the "
            "Credentials modal."
        )
    return api_key


def _default_model(provider: str) -> str:
    from services.llm.config import get_provider_config

    config = get_provider_config(provider)
    default = getattr(config, "default_model", "") if config else ""
    if not default:
        raise NodeUserError(
            f"No default model configured for provider '{provider}'; set "
            "vision_model on the node"
        )
    return default


class VisionAnalyzeNode(ToolNode):
    type = "visionAnalyze"
    display_name = "Vision Analyze"
    subtitle = "See Images via a Vision Model"
    group = ("tool",)
    description = (
        "Delegate image understanding to a vision-capable model: describe "
        "an image or extract its text. Works with any host agent, "
        "including text-only models"
    )
    component_kind = "tool"
    tool_name = "vision"
    tool_description = (
        "See an image by delegating to a vision model. describe returns a "
        "detailed description (pass question for something specific); "
        "extract_text returns the text visible in the image. image is a "
        "workspace-relative path; budget controls detail vs cost."
    )
    handles = (
        {
            "name": "output-tool",
            "kind": "output",
            "position": "top",
            "label": "Vision",
            "role": "tools",
        },
    )
    ui_hints = {
        "isToolPanel": True,
        "hideInputSection": True,
        "hideOutputSection": True,
        "hideRunButton": True,
    }
    annotations = {
        "destructive": False,
        "readonly": True,
        "open_world": True,  # sends image content to an external provider
    }
    task_queue = TaskQueue.AI_HEAVY

    Params = VisionAnalyzeParams
    ToolInput = VisionAnalyzeToolInput
    Output = VisionAnalyzeOutput
    tool_schema_locked = True
    server_controlled_fields = frozenset({"provider", "vision_model"})

    @staticmethod
    def _config(ctx: NodeContext, params: Any) -> VisionAnalyzeParams:
        config = ctx.raw.get("_tool_config")
        if isinstance(config, VisionAnalyzeParams):
            return config
        if isinstance(params, VisionAnalyzeParams):
            return params
        return VisionAnalyzeParams()

    @Operation("vision")
    async def vision(
        self,
        ctx: NodeContext,
        params: VisionAnalyzeToolInput | VisionAnalyzeParams,
    ) -> VisionAnalyzeOutput:
        if isinstance(params, VisionAnalyzeParams):
            raise NodeUserError(
                "The vision tool needs an image argument; connect it to an "
                "agent's tools and let the agent call it"
            )
        args = params
        config = self._config(ctx, params)
        provider = config.provider
        model = config.vision_model.strip() or _default_model(provider)

        # Bytes exist only inside this call — loaded through the contained
        # media reader, resized to budget, sent, and dropped.
        from services.media import read_media_bytes
        from services.media.image_fit import fit_image_bytes
        from services.media.limits import MEDIA_MAX_READ_BYTES

        try:
            filename, blob = read_media_bytes(
                args.image, ctx=ctx, max_bytes=MEDIA_MAX_READ_BYTES
            )
        except ValueError as exc:
            raise NodeUserError(str(exc)) from exc
        try:
            fitted, mime, (width, height) = fit_image_bytes(
                blob, budget=args.budget
            )
        except Exception as exc:
            raise NodeUserError(
                f"'{filename}' is not a readable image: {exc}"
            ) from exc

        prompt = (
            args.question.strip()
            if args.operation == "describe" and args.question
            else _PROMPTS[args.operation]
        )
        api_key = await _api_key(ctx, provider)
        dispatch = {
            "openai": self._ask_openai,
            "anthropic": self._ask_anthropic,
            "gemini": self._ask_gemini,
        }
        text = await dispatch[provider](
            api_key=api_key,
            model=model,
            prompt=prompt,
            image_bytes=fitted,
            mime=mime,
        )
        return VisionAnalyzeOutput(
            operation=args.operation,
            provider=provider,
            vision_model=model,
            text=text,
            image={"width": width, "height": height, "source": args.image},
        )

    # ------------------------------------------------ provider requests

    @staticmethod
    async def _ask_openai(
        *, api_key: str, model: str, prompt: str, image_bytes: bytes, mime: str
    ) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key)
        data_url = (
            f"data:{mime};base64,"
            f"{base64.b64encode(image_bytes).decode('ascii')}"
        )
        response = await client.chat.completions.create(
            model=model,
            max_completion_tokens=_MAX_OUTPUT_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            # detail set explicitly — omitting it has caused
                            # order-of-magnitude token regressions elsewhere.
                            "image_url": {"url": data_url, "detail": "auto"},
                        },
                    ],
                }
            ],
        )
        return str(response.choices[0].message.content or "")

    @staticmethod
    async def _ask_anthropic(
        *, api_key: str, model: str, prompt: str, image_bytes: bytes, mime: str
    ) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": base64.b64encode(image_bytes).decode(
                                    "ascii"
                                ),
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )

    @staticmethod
    async def _ask_gemini(
        *, api_key: str, model: str, prompt: str, image_bytes: bytes, mime: str
    ) -> str:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ],
        )
        return str(getattr(response, "text", "") or "")


__all__ = [
    "VisionAnalyzeNode",
    "VisionAnalyzeOutput",
    "VisionAnalyzeParams",
    "VisionAnalyzeToolInput",
]
