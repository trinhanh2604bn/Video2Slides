"""Shared visual style specifications, one per presentation theme.

Every image in a deck is generated with the same style block (palette, art
direction, lighting, composition, rendering) so the illustrations look like one
consistent set and match the PPTX theme colours in ``pptx_engine/themes.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisualStyle:
    key: str
    palette: str
    art_direction: str
    lighting: str
    composition: str
    rendering: str

    def suffix(self) -> str:
        return ", ".join([self.art_direction, self.palette, self.lighting, self.composition, self.rendering])


# Composition always leaves calm negative space and keeps the subject centred, because
# the PPTX engine crops images "cover"-style to fit the layout box.
VISUAL_STYLES: dict[str, VisualStyle] = {
    "professional": VisualStyle(
        key="professional",
        palette="navy blue, bright blue and teal accents on clean white and light grey",
        art_direction="clean modern corporate illustration, soft minimal 3D render",
        lighting="soft diffused studio lighting, gentle shadows",
        composition="single clear focal subject centered, uncluttered background, generous negative space",
        rendering="crisp details, smooth gradients, high resolution, professional quality",
    ),
    "academic": VisualStyle(
        key="academic",
        palette="muted burgundy, warm gold and deep teal on ivory paper tones",
        art_direction="refined editorial illustration, classic scholarly atmosphere",
        lighting="warm natural window light, calm and balanced",
        composition="balanced classical composition, subject centered, plain background, ample negative space",
        rendering="fine detail, subtle paper texture, high resolution, elegant",
    ),
    "creative": VisualStyle(
        key="creative",
        palette="deep navy and charcoal background with glowing pink and violet accents",
        art_direction="vibrant contemporary digital art, bold stylised shapes",
        lighting="dramatic rim lighting, soft neon glow",
        composition="dynamic composition with subject near center, dark open space around it",
        rendering="rich colour, smooth shading, high resolution, striking",
    ),
}

# Raster images must never contain typography; slide text stays editable in PowerPoint.
BASE_NEGATIVE = (
    "text, letters, words, typography, caption, title, watermark, signature, logo, brand, label, "
    "numbers, chart, graph, table, diagram, user interface, screenshot, frame, border, collage, "
    "blurry, low quality, jpeg artifacts, deformed, distorted, disfigured, extra limbs, bad anatomy, "
    "nsfw, nudity, gore, violence"
)


def get_visual_style(key: str | None) -> VisualStyle:
    return VISUAL_STYLES.get((key or "professional").lower(), VISUAL_STYLES["professional"])
