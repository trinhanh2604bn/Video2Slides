"""Validation of generation parameters, prompts and returned images.

The same limits are enforced again inside the Modal worker; checking here too
means a bad request never reaches (and never bills) the GPU.
"""

from __future__ import annotations

import io
import re

from PIL import Image, ImageStat, UnidentifiedImageError

from .base import ImageGenerationError

MIN_SIDE, MAX_SIDE = 512, 1536
MAX_PIXELS = 1_100_000          # ~1 MP: SDXL native size, bounds GPU time and memory
MIN_STEPS, MAX_STEPS = 10, 40
MIN_GUIDANCE, MAX_GUIDANCE = 1.0, 12.0
MAX_PROMPT_CHARS = 900
MAX_IMAGE_BYTES = 12 * 1024 * 1024
OUTPUT_FORMATS = ("jpeg", "png")

# SDXL-friendly sizes (multiples of 64, ~1 MP) per aspect ratio.
ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1344, 768),
    "3:2": (1216, 832),
    "4:3": (1152, 896),
    "1:1": (1024, 1024),
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Prompts are written by the LLM from user transcripts; refuse clearly unsafe subjects.
_BLOCKED = re.compile(
    r"\b(nude|nudity|naked|nsfw|porn\w*|sexual|explicit|gore|beheading|child abuse)\b", re.IGNORECASE
)


def clean_prompt(text: str) -> str:
    text = _CONTROL.sub(" ", text or "")
    return " ".join(text.split())


def check_prompt(prompt: str) -> str:
    prompt = clean_prompt(prompt)
    if not prompt:
        raise ImageGenerationError("Prompt ảnh trống.", "invalid_prompt")
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS].rsplit(" ", 1)[0]
    if _BLOCKED.search(prompt):
        raise ImageGenerationError("Prompt ảnh chứa nội dung không được phép.", "blocked_prompt")
    return prompt


def check_params(width: int, height: int, steps: int, guidance: float, seed: int, output_format: str) -> None:
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise ImageGenerationError(f"Kích thước ảnh {width}x{height} nằm ngoài giới hạn.", "invalid_params")
    if width % 64 or height % 64:
        raise ImageGenerationError("Kích thước ảnh phải là bội số của 64.", "invalid_params")
    if width * height > MAX_PIXELS:
        raise ImageGenerationError(f"Ảnh {width}x{height} vượt quá {MAX_PIXELS} điểm ảnh.", "invalid_params")
    if not (MIN_STEPS <= steps <= MAX_STEPS):
        raise ImageGenerationError(f"Số bước suy luận phải trong khoảng {MIN_STEPS}-{MAX_STEPS}.", "invalid_params")
    if not (MIN_GUIDANCE <= guidance <= MAX_GUIDANCE):
        raise ImageGenerationError("Giá trị guidance không hợp lệ.", "invalid_params")
    if not (0 <= seed < 2**31):
        raise ImageGenerationError("Seed không hợp lệ.", "invalid_params")
    if output_format not in OUTPUT_FORMATS:
        raise ImageGenerationError("Định dạng ảnh không hỗ trợ.", "invalid_params")


def size_for(aspect_ratio: str, default: tuple[int, int]) -> tuple[int, int]:
    return ASPECT_SIZES.get(aspect_ratio, default)


def sanitize_image(data: bytes, expect_w: int | None = None, expect_h: int | None = None) -> tuple[bytes, int, int]:
    """Decode untrusted image bytes safely and re-encode them as a clean JPEG.

    Rejects oversized payloads, decompression bombs, non-image data, unexpected
    sizes and blank (single-colour / black) outputs. Re-encoding drops metadata.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageGenerationError("Ảnh trả về rỗng hoặc quá lớn.", "invalid_image", retryable=True)
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ("JPEG", "PNG", "WEBP"):
                raise ImageGenerationError("Định dạng ảnh trả về không hợp lệ.", "invalid_image", retryable=True)
            w, h = probe.size
            if w * h > MAX_PIXELS * 1.05:
                raise ImageGenerationError("Ảnh trả về quá lớn.", "invalid_image")
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            rgb = im.convert("RGB")
    except ImageGenerationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageGenerationError(f"Không đọc được ảnh trả về ({exc.__class__.__name__}).", "invalid_image",
                                   retryable=True) from None

    if expect_w and expect_h and (w, h) != (expect_w, expect_h):
        raise ImageGenerationError(f"Ảnh trả về có kích thước {w}x{h}, cần {expect_w}x{expect_h}.", "invalid_image")
    stddev = ImageStat.Stat(rgb.convert("L")).stddev[0]
    if stddev < 3.0:
        # SDXL's fp16 VAE can yield black frames; a flat image is never a valid illustration.
        raise ImageGenerationError("Ảnh trả về bị trống (một màu).", "blank_image", retryable=True)

    out = io.BytesIO()
    rgb.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue(), w, h
