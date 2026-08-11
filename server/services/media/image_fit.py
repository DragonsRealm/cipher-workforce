"""Budget-based image fitting for vision-model input.

The budget abstraction (after QwenLM/Qwen-MM-Plugins): callers pick
``"small" | "normal" | "large"`` defined in *visual tokens*, converted to a
pixel area via ``tokens x patch^2``. The agent gets a cost dial it can
reason about, and per-provider retargeting is a parameter, not a rewrite.

``smart_resize`` is the canonical NaViT-style rule (verbatim behavior from
the Qwen-VL preprocessing): one aspect-preserving scale into the
[min_pixels, max_pixels] area window, then snap both dimensions to patch
multiples so no partial patches are billed. Both a floor and a ceiling are
enforced — a tiny thumbnail is scaled UP so its budget isn't wasted.

Bytes produced here are transient request material only. They must never
enter node results, Temporal payloads, or the context journal (the
never-bytes rule in docs-internal/media_transport.md).
"""

from __future__ import annotations

import io
import math
from typing import Tuple

IMAGE_BUDGET_TOKENS = {"small": 256, "normal": 1024, "large": 2048}
DEFAULT_BUDGET = "normal"
# 28 px is the Anthropic patch edge; close enough to every current
# provider's tiling that a single default serves the delegate path.
# Callers with provider-exact needs pass their own factor.
DEFAULT_PATCH = 28
_MIN_TOKENS = min(IMAGE_BUDGET_TOKENS.values())
_JPEG_QUALITY = 90


def budget_to_pixels(budget: str, *, patch: int = DEFAULT_PATCH) -> int:
    tokens = IMAGE_BUDGET_TOKENS.get(budget, IMAGE_BUDGET_TOKENS[DEFAULT_BUDGET])
    return tokens * patch * patch


def smart_resize(
    height: int,
    width: int,
    *,
    min_pixels: int,
    max_pixels: int,
    factor: int = DEFAULT_PATCH,
) -> Tuple[int, int]:
    """Aspect-preserving (height, width) inside the pixel-area window,
    snapped to ``factor`` multiples."""
    if height <= 0 or width <= 0:
        raise ValueError("image dimensions must be positive")
    area = height * width
    scale = 1.0
    if area < min_pixels:
        scale = math.sqrt(min_pixels / area)
    elif area > max_pixels:
        scale = math.sqrt(max_pixels / area)
    resized_height = max(factor, round(height * scale / factor) * factor)
    resized_width = max(factor, round(width * scale / factor) * factor)
    return resized_height, resized_width


def fit_image_bytes(
    data: bytes,
    *,
    budget: str = DEFAULT_BUDGET,
    patch: int = DEFAULT_PATCH,
    max_pixels_cap: int | None = None,
) -> Tuple[bytes, str, Tuple[int, int]]:
    """Fit encoded image bytes to a token budget.

    Returns ``(encoded_bytes, mime_type, (width, height))``. JPEG q90 for
    opaque images (tool results accumulate; PNG at full budget is what
    blows request caps), PNG kept when alpha must survive.
    """
    from PIL import Image

    max_pixels = budget_to_pixels(budget, patch=patch)
    if max_pixels_cap is not None:
        max_pixels = min(max_pixels, max_pixels_cap)
    min_pixels = min(_MIN_TOKENS * patch * patch, max_pixels)

    with Image.open(io.BytesIO(data)) as image:
        target_h, target_w = smart_resize(
            image.height,
            image.width,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
            factor=patch,
        )
        has_alpha = image.mode in ("RGBA", "LA", "PA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if (target_w, target_h) != (image.width, image.height):
            resample = Image.Resampling.LANCZOS
            image = image.resize((target_w, target_h), resample)
        buffer = io.BytesIO()
        if has_alpha:
            image.convert("RGBA").save(buffer, format="PNG", optimize=True)
            mime = "image/png"
        else:
            image.convert("RGB").save(
                buffer, format="JPEG", quality=_JPEG_QUALITY
            )
            mime = "image/jpeg"
        return buffer.getvalue(), mime, (target_w, target_h)


__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_PATCH",
    "IMAGE_BUDGET_TOKENS",
    "budget_to_pixels",
    "fit_image_bytes",
    "smart_resize",
]
