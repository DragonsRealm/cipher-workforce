"""Tool-result images: refs in durable state, bytes only at the provider boundary.

A tool opts in by returning ``llm_media: [{"ref": <FileRef kind=image>,
"detail": "auto"}]``. The agent loop attaches ref-only image ContentBlocks to
the tool message (durable, ~450 B each). ``hydrate_image_blocks`` runs once
per LLM step on throwaway copies, loading bytes through the contained media
reader and fitting them to a budget; provider encoders then emit the
official wire shapes. Originals are never mutated, so nothing downstream of
the journal ever sees bytes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, List, Sequence

from core.logging import get_logger
from services.llm.config import LLM_DEFAULTS
from services.llm.protocol import ContentBlock, Message

logger = get_logger(__name__)

LLM_MEDIA_KEY = "llm_media"
LLM_MEDIA_MAX_PER_RESULT = 8
IMAGE_MIME_ALLOWLIST = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# detail -> visual-token budget for services.media.image_fit.
_DETAIL_BUDGETS = {"low": "small", "auto": "normal", "high": "large"}


def provider_supports_vision(provider: str, model: str = "") -> bool:
    """Capability gate. Unknown means False: a rejected image block would be
    journaled and resent on every turn, so never emit one speculatively."""
    del model  # per-model gating arrives with registry modality data
    vision = (
        LLM_DEFAULTS.get("providers", {}).get(provider, {}).get("vision", {})
    )
    return bool(vision.get("enabled"))


def image_blocks_from_tool_result(result: Any) -> List[ContentBlock]:
    """Parse a tool result's ``llm_media`` opt-in into ref-only image blocks.

    Invalid entries are skipped with a warning, never raised — the tool
    already did its (possibly paid) work.
    """
    if not isinstance(result, dict):
        return []
    entries = result.get(LLM_MEDIA_KEY)
    if not isinstance(entries, list):
        return []
    blocks: List[ContentBlock] = []
    for entry in entries[:LLM_MEDIA_MAX_PER_RESULT]:
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if not isinstance(ref, dict):
            logger.warning("llm_media entry without a ref; skipped")
            continue
        if ref.get("kind") not in ("image", "file"):
            logger.warning(
                "llm_media ref kind unsupported", kind=ref.get("kind")
            )
            continue
        if ref.get("mime_type") not in IMAGE_MIME_ALLOWLIST:
            logger.warning(
                "llm_media mime unsupported", mime=ref.get("mime_type")
            )
            continue
        if not ref.get("workflow_id") or not ref.get("path"):
            logger.warning("llm_media ref missing workflow_id/path; skipped")
            continue
        detail = str(entry.get("detail") or "auto")
        blocks.append(
            ContentBlock(
                type="image",
                source={
                    "kind": "file_ref",
                    "ref": dict(ref),
                    "detail": detail if detail in _DETAIL_BUDGETS else "auto",
                },
            )
        )
    return blocks


def _placeholder(ref: dict, reason: str) -> ContentBlock:
    filename = ref.get("filename") or ref.get("path") or "image"
    return ContentBlock(
        type="text", text=f"[Image attached: {filename} — {reason}]"
    )


async def hydrate_image_blocks(
    messages: Sequence[Message], *, provider: str, model: str
) -> List[Message]:
    """Return messages with image blocks hydrated for one provider call.

    Identity (same objects, zero cost) when nothing carries an image ref.
    Hydration works on copies; per-image failures degrade to a text
    placeholder rather than failing an otherwise-valid turn.
    """
    if not any(_ref_blocks(message) for message in messages):
        return list(messages)

    capable = provider_supports_vision(provider, model)
    hydrated: List[Message] = []
    for message in messages:
        if not _ref_blocks(message):
            hydrated.append(message)
            continue
        blocks: List[ContentBlock] = []
        for block in message.blocks:
            if not _is_ref_block(block):
                blocks.append(block)
                continue
            source = block.source or {}
            ref = dict(source.get("ref") or {})
            if not capable:
                blocks.append(
                    _placeholder(
                        ref,
                        f"the current model ({model}) cannot view images; "
                        "metadata only",
                    )
                )
                continue
            try:
                data_b64, mime = await _hydrate_one(
                    ref, str(source.get("detail") or "auto")
                )
                blocks.append(
                    ContentBlock(
                        type="image",
                        source={
                            "kind": "bytes",
                            "media_type": mime,
                            "data_b64": data_b64,
                            "detail": source.get("detail") or "auto",
                        },
                    )
                )
            except Exception as exc:
                logger.warning(
                    "image hydration failed", path=ref.get("path"), error=str(exc)
                )
                blocks.append(_placeholder(ref, f"unavailable: {exc}"))
        hydrated.append(replace(message, blocks=blocks))
    return hydrated


def _is_ref_block(block: ContentBlock) -> bool:
    return (
        block.type == "image"
        and isinstance(block.source, dict)
        and block.source.get("kind") == "file_ref"
    )


def _ref_blocks(message: Message) -> List[ContentBlock]:
    return [block for block in message.blocks or [] if _is_ref_block(block)]


async def _hydrate_one(ref: dict, detail: str) -> tuple[str, str]:
    """Resolve a FileRef to fitted base64 + mime, contained end to end."""
    import asyncio
    import base64

    from nodes.filesystem._backend import resolve_within
    from services.media.image_fit import fit_image_bytes
    from services.media.limits import MEDIA_MAX_READ_BYTES
    from services.plugin.deps import get_database
    from services.workspace_locator import resolve_workspace_root

    root = await resolve_workspace_root(
        str(ref.get("workflow_id") or ""), get_database()
    )
    path = resolve_within(root, str(ref.get("path") or ""))
    if not path.is_file():
        raise FileNotFoundError(f"{ref.get('path')} not found in workspace")
    if path.stat().st_size > MEDIA_MAX_READ_BYTES:
        raise ValueError("image exceeds the read size cap")
    budget = _DETAIL_BUDGETS.get(detail, "normal")

    def fit() -> tuple[str, str]:
        fitted, mime, _ = fit_image_bytes(path.read_bytes(), budget=budget)
        return base64.b64encode(fitted).decode("ascii"), mime

    return await asyncio.to_thread(fit)


__all__ = [
    "IMAGE_MIME_ALLOWLIST",
    "LLM_MEDIA_KEY",
    "LLM_MEDIA_MAX_PER_RESULT",
    "hydrate_image_blocks",
    "image_blocks_from_tool_result",
    "provider_supports_vision",
]
