"""Illustration planner: structured slide content -> validated image requests.

The existing LLM writes a short English ``image_prompt`` for the slides that
benefit from a picture (see ``ai/prompt.py``). The planner only selects, caps,
validates and decorates those prompts with the shared deck style; it never puts
slide text, numbers or titles into the picture.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from .. import config
from ..ai.schema import Presentation, Slide
from .base import ImageGenerationError, ImageRequest
from .styles import BASE_NEGATIVE, get_visual_style
from .validation import check_params, check_prompt, size_for

# Text-structured layouts keep their editable shapes; pictures only go where the layout has room.
ELIGIBLE_LAYOUTS = ("title", "image_left", "image_right", "bullets", "concept")
DENSITIES = ("standard", "high")

# Used when the outline has no LLM image prompts (offline provider): one neutral title artwork.
FALLBACK_TITLE_SUBJECT = "abstract conceptual artwork of flowing layered shapes and soft light suggesting ideas and learning"


@dataclass
class IllustrationPlan:
    requests: list[ImageRequest] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human-readable reasons, no transcript content


def deck_seed(presentation_id: str, slide_id: str, variant: int = 0) -> int:
    digest = hashlib.sha256(f"{presentation_id}:{slide_id}:{variant}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


def image_cap(slide_count: int, density: str, max_images: int) -> int:
    limit = min(max_images, config.settings.image_max_per_presentation)
    if density == "high":
        return max(0, limit)
    return max(0, min(limit, max(2, math.ceil(slide_count / 3))))


def _eligible(s: Slide) -> bool:
    if s.layout_type not in ELIGIBLE_LAYOUTS:
        return False
    if s.layout_type == "bullets" and not (1 <= len(s.key_points) <= 5):
        return False
    return True


def plan_illustrations(pres: Presentation, presentation_id: str, style: str, density: str = "standard",
                       max_images: int | None = None, variants: dict[str, int] | None = None) -> IllustrationPlan:
    """Choose the slides to illustrate and build one request per slide."""
    s = config.settings
    variants = variants or {}
    cap = image_cap(len(pres.slides), density if density in DENSITIES else "standard",
                    s.image_max_per_presentation if max_images is None else max_images)
    plan = IllustrationPlan()
    if cap == 0:
        return plan

    with_prompt = [sl for sl in pres.slides if sl.image_prompt and _eligible(sl)]
    # Title first, then slides the LLM explicitly marked as illustrations, then the rest; deck order within.
    def priority(sl: Slide) -> tuple[int, int]:
        rank = 0 if sl.layout_type == "title" else (1 if sl.visual_type == "illustration" else 2)
        return rank, sl.slide_number

    chosen: list[tuple[Slide, str, str]] = [(sl, sl.image_prompt, "llm") for sl in sorted(with_prompt, key=priority)]
    if not chosen and pres.slides and pres.slides[0].layout_type == "title":
        chosen = [(pres.slides[0], FALLBACK_TITLE_SUBJECT, "fallback")]
    if len(chosen) > cap:
        plan.skipped.append(f"{len(chosen) - cap} slide vượt giới hạn {cap} ảnh/bài")
        chosen = chosen[:cap]

    vs = get_visual_style(style)
    width, height = size_for("16:9", (s.image_width, s.image_height))
    for sl, subject, source in sorted(chosen, key=lambda c: c[0].slide_number):
        try:
            prompt = check_prompt(f"{subject}, {vs.suffix()}")
            req = ImageRequest(
                presentation_id=presentation_id,
                slide_id=sl.slide_id,
                slide_title=sl.title[:120],
                visual_description=(sl.visual_description or "")[:300],
                prompt=prompt,
                negative_prompt=BASE_NEGATIVE,
                style=vs.key,
                aspect_ratio="16:9",
                width=width,
                height=height,
                seed=deck_seed(presentation_id, sl.slide_id, variants.get(sl.slide_id, 0)),
                steps=s.image_steps,
                guidance=s.image_guidance,
                output_format="jpeg",
                prompt_source=source,
            )
            check_params(req.width, req.height, req.steps, req.guidance, req.seed, req.output_format)
        except ImageGenerationError as exc:
            plan.skipped.append(f"{sl.slide_id}: {exc}")
            continue
        plan.requests.append(req)
    return plan


def apply_illustrations(pres: Presentation, slide_ids: set[str]) -> None:
    """Switch illustrated slides to image layouts (alternating sides) before rendering."""
    side = "image_right"
    for sl in pres.slides:
        if sl.slide_id not in slide_ids:
            continue
        sl.visual_type = "illustration"
        if sl.layout_type == "bullets":
            sl.layout_type = side
        if sl.layout_type in ("image_left", "image_right"):
            side = "image_left" if sl.layout_type == "image_right" else "image_right"
