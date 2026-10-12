"""Turn a validated ``Presentation`` outline into a designed, editable .pptx file."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from pptx import Presentation as PptxPresentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from ..ai.schema import Presentation, Slide
from . import draw
from .textfit import fit_or_trim, text_height_in
from .themes import Theme, get_theme

W, H = 13.333, 7.5
M = 0.6               # outer margin
CT, CB = 1.8, 6.85    # content top / bottom
CH = CB - CT

LABELS = {
    "vi": {"frame": "Khung hình tại", "sources": "Thời điểm trong video", "ai_image": "Ảnh minh họa tạo bởi AI",
           "definition": "ĐỊNH NGHĨA", "insight": "ĐIỂM CỐT LÕI"},
    "en": {"frame": "Frame at", "sources": "Video timestamps", "ai_image": "AI-generated illustration",
           "definition": "DEFINITION", "insight": "KEY INSIGHT"},
}


@dataclass
class FrameRef:
    path: Path
    timestamp: float


@dataclass
class BuildContext:
    theme: Theme
    deck_title: str
    language: str
    frames: list[FrameRef]
    total: int
    used_frames: set[Path] = field(default_factory=set)
    illustrations: dict[str, Path] = field(default_factory=dict)  # slide_id -> AI image
    warnings: list[str] = field(default_factory=list)
    section_no: int = 0

    @property
    def labels(self) -> dict[str, str]:
        return LABELS.get(self.language, LABELS["en"])


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- frames

def pick_frame(ctx: BuildContext, s: Slide, max_distance: float | None = None) -> FrameRef | None:
    """Unused frame nearest to the slide's source timestamps."""
    candidates = [f for f in ctx.frames if f.path not in ctx.used_frames]
    if not candidates:
        return None
    if s.source_timestamps:
        def dist(f: FrameRef) -> float:
            return min(abs(f.timestamp - t) for t in s.source_timestamps)
        best = min(candidates, key=dist)
        if max_distance is not None and dist(best) > max_distance:
            return None
    elif max_distance is not None:
        return None
    else:
        best = candidates[0]
    ctx.used_frames.add(best.path)
    return best


def plan_layouts(pres: Presentation, frames: list[FrameRef], illustrated: set[str] | frozenset = frozenset()) -> None:
    """Upgrade plain bullet slides to image layouts when a matching frame exists,
    so the deck alternates visual rhythm instead of repeating one layout."""
    if not frames:
        return
    reserved = sum(1 for s in pres.slides
                   if s.layout_type in ("image_left", "image_right") and s.slide_id not in illustrated)
    spare = len(frames) - reserved - 1  # keep one for the title slide
    side = "image_right"
    prev = None
    for s in pres.slides:
        if spare <= 0:
            break
        if s.layout_type == "bullets" and s.source_timestamps and 1 <= len(s.key_points) <= 5 and prev == "bullets":
            if any(min(abs(f.timestamp - t) for t in s.source_timestamps) <= 45 for f in frames):
                s.layout_type = side
                s.visual_type = "video_frame"
                side = "image_left" if side == "image_right" else "image_right"
                spare -= 1
        prev = s.layout_type


# --------------------------------------------------------------------------- chrome

def _title_color(t: Theme):
    return t.rgb("text") if t.dark else t.rgb("primary")


def _fit(ctx: BuildContext, slide_no: int, paras: list[str], w: float, h: float, max_pt: float,
         min_pt: float, bold: bool = False, indent: float = 0.0) -> tuple[list[str], float]:
    fitted, size = fit_or_trim(paras, w, h, max_pt, min_pt, bold, indent)
    if fitted != [p for p in paras if p]:
        ctx.warnings.append(f"Slide {slide_no}: text shortened to fit")
    return fitted, size


def _take(ctx: BuildContext, s: Slide, seq: list, limit: int) -> list:
    """Cap list length for a layout, recording a warning when content is dropped."""
    if len(seq) > limit:
        ctx.warnings.append(f"Slide {s.slide_number}: text shortened to fit ({len(seq) - limit} items dropped)")
    return seq[:limit]


def chrome(ctx: BuildContext, sl, s: Slide, title: str | None = None) -> None:
    """Background, title, accent and footer shared by content slides."""
    t = ctx.theme
    draw.set_background(sl, t.rgb("background"))
    if t.key == "professional":
        draw.shape(sl, 0, 0, W, 0.09, t.rgb("primary"))
    elif t.key == "creative":
        draw.shape(sl, W - 2.6, -1.6, 4.2, 4.2, t.rgb("accent2"), MSO_SHAPE.OVAL, alpha=0.18)
        draw.shape(sl, W - 1.2, 0.9, 1.4, 1.4, t.rgb("accent"), MSO_SHAPE.OVAL, alpha=0.22)

    title_w = W - 2 * M - (1.6 if t.key == "creative" else 0)
    paras, size = _fit(ctx, s.slide_number, [title or s.title], title_w, 1.0, 32, 22, bold=True)
    draw.text(sl, M, 0.45, title_w, 1.0, paras, size, _title_color(t), t.title_font, bold=True,
              anchor=MSO_ANCHOR.BOTTOM)
    if t.key == "academic":
        draw.shape(sl, M + 0.1, 1.52, W - 2 * M - 0.2, 0.015, t.rgb("accent"))
        draw.shape(sl, M + 0.1, 1.49, 1.1, 0.07, t.rgb("primary"))
    else:
        draw.shape(sl, M + 0.1, 1.5, 0.9, 0.07, t.rgb("accent"),
                   MSO_SHAPE.ROUNDED_RECTANGLE if t.rounded else MSO_SHAPE.RECTANGLE, radius=0.5)

    # Footer
    if t.key == "academic":
        draw.shape(sl, M + 0.1, 7.0, W - 2 * M - 0.2, 0.01, t.rgb("accent"))
    draw.text(sl, M, 7.03, 8, 0.35, ctx.deck_title[:90], 10, t.rgb("muted"), t.body_font)
    draw.text(sl, W - M - 1.5, 7.03, 1.5, 0.35, f"{s.slide_number} / {ctx.total}", 10, t.rgb("muted"),
              t.body_font, align=PP_ALIGN.RIGHT)


def _card(ctx: BuildContext, sl, x, y, w, h, fill_name: str = "surface", line: bool = False):
    t = ctx.theme
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if t.rounded else MSO_SHAPE.RECTANGLE
    radius = min(0.12, 0.25 / max(min(w, h), 0.5)) if t.rounded else None
    return draw.shape(sl, x, y, w, h, t.rgb(fill_name), kind, radius=radius,
                      line=t.rgb("accent") if line else None, line_pt=0.75)


def _badge(ctx: BuildContext, sl, x, y, d, label: str, color_name: str = "accent"):
    t = ctx.theme
    circ = draw.shape(sl, x, y, d, d, t.rgb(color_name), MSO_SHAPE.OVAL)
    draw.text(sl, 0, 0, 0, 0, label, max(11, d * 30), t.rgb("on_primary"), t.body_font, bold=True,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, target=circ)
    circ.text_frame.margin_left = circ.text_frame.margin_right = 0
    return circ


def _bullets(ctx: BuildContext, sl, s: Slide, points: list[str], x, y, w, h, max_pt=24, min_pt=14,
             color_name="text", anchor=MSO_ANCHOR.TOP):
    t = ctx.theme
    paras, size = _fit(ctx, s.slide_number, points, w, h, max_pt, min_pt, indent=0.32)
    draw.text(sl, x, y, w, h, paras, size, t.rgb(color_name), t.body_font, anchor=anchor,
              bullet_color=t.rgb("accent"))


# --------------------------------------------------------------------------- layouts

def layout_title(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    draw.set_background(sl, t.rgb("primary"))
    illus = ctx.illustrations.get(s.slide_id)
    frame = FrameRef(illus, 0.0) if illus else pick_frame(ctx, s)
    if frame:
        # Show the whole frame / illustration (no heavy crop) as a framed 16:9 picture beside the text.
        img_w = 5.9
        img_h = img_w * 9 / 16
        ix, iy = W - M - img_w, (H - img_h) / 2
        draw.shape(sl, W - 3.4, -1.0, 4.6, 4.6, t.rgb("accent2"), MSO_SHAPE.OVAL, alpha=0.2)
        draw.shape(sl, ix + 0.22, iy + 0.22, img_w, img_h, t.rgb("accent"))
        draw.picture_cover(sl, frame.path, ix, iy, img_w, img_h)
        text_w = ix - M - 0.7
    else:
        draw.shape(sl, W - 4.8, -1.2, 6.0, 6.0, t.rgb("accent"), MSO_SHAPE.OVAL, alpha=0.25)
        draw.shape(sl, W - 3.0, 3.9, 4.2, 4.2, t.rgb("accent2"), MSO_SHAPE.OVAL, alpha=0.3)
        text_w = W - 2 * M - 3.5

    x = M + 0.2
    title = s.title or ctx.deck_title
    paras, size = _fit(ctx, s.slide_number, [title], text_w, 2.9, 44, 26, bold=True)
    draw.text(sl, x, 1.2, text_w, 2.9, paras, size, t.rgb("on_primary"), t.title_font, bold=True,
              anchor=MSO_ANCHOR.BOTTOM)
    draw.shape(sl, x + 0.1, 4.3, 1.3, 0.08, t.rgb("accent"))
    subtitle = s.subtitle or (s.key_points[0] if s.key_points else "")
    if subtitle:
        paras, size = _fit(ctx, s.slide_number, [subtitle], text_w, 1.6, 22, 14)
        draw.text(sl, x, 4.55, text_w, 1.6, paras, size, t.rgb("on_primary"), t.body_font)


def layout_section(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    ctx.section_no += 1
    draw.set_background(sl, t.rgb("primary"))
    draw.shape(sl, W - 5.0, 1.0, 6.5, 6.5, t.rgb("accent2"), MSO_SHAPE.OVAL, alpha=0.18)
    draw.text(sl, M + 0.2, 1.3, 4, 1.4, f"{ctx.section_no:02d}", 72, t.rgb("accent"), t.title_font, bold=True,
              anchor=MSO_ANCHOR.BOTTOM)
    paras, size = _fit(ctx, s.slide_number, [s.title], 9.5, 1.9, 42, 26, bold=True)
    draw.text(sl, M + 0.2, 2.85, 9.5, 1.9, paras, size, t.rgb("on_primary"), t.title_font, bold=True)
    sub = s.subtitle or " · ".join(s.key_points[:3])
    if sub:
        paras, size = _fit(ctx, s.slide_number, [sub], 9.5, 1.3, 20, 13)
        draw.text(sl, M + 0.2, 4.9, 9.5, 1.3, paras, size, t.rgb("on_primary"), t.body_font)


def layout_bullets(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    points = _take(ctx, s, s.key_points, 6)
    if 2 <= len(points) <= 4:
        # Numbered rows read better than a bare list for a few key points.
        gap = 0.22
        row_h = min(1.15, (CH - gap * (len(points) - 1)) / len(points))
        y = CT + (CH - (row_h * len(points) + gap * (len(points) - 1))) / 2
        for i, p in enumerate(points):
            _card(ctx, sl, M + 0.1, y, W - 2 * M - 0.2, row_h)
            draw.shape(sl, M + 0.1, y, 0.09, row_h, t.rgb("accent" if i % 2 == 0 else "accent2"))
            d = min(0.6, row_h - 0.3)
            _badge(ctx, sl, M + 0.45, y + (row_h - d) / 2, d, str(i + 1), "accent" if i % 2 == 0 else "accent2")
            tw = W - 2 * M - 1.5
            paras, size = _fit(ctx, s.slide_number, [p], tw, row_h - 0.1, 22, 13)
            draw.text(sl, M + 1.3, y + 0.05, tw, row_h - 0.1, paras, size, t.rgb("text"), t.body_font,
                      anchor=MSO_ANCHOR.MIDDLE)
            y += row_h + gap
    else:
        _bullets(ctx, sl, s, points, M + 0.1, CT + 0.1, W - 2 * M - 0.2, CH - 0.2, max_pt=24, min_pt=14)


def layout_image(ctx: BuildContext, sl, s: Slide, image_side: str) -> None:
    t = ctx.theme
    illus = ctx.illustrations.get(s.slide_id)
    frame = FrameRef(illus, 0.0) if illus else pick_frame(ctx, s)
    if frame is None:
        return layout_bullets(ctx, sl, s)
    chrome(ctx, sl, s)
    img_w = 6.3
    ratio = draw.image_ratio(frame.path)
    img_h = min(img_w / ratio, CH - 0.6)
    img_w = img_h * ratio if img_h == CH - 0.6 else img_w
    iy = CT + (CH - 0.45 - img_h) / 2
    if image_side == "left":
        ix, tx = M + 0.1, M + 0.1 + img_w + 0.5
    else:
        ix, tx = W - M - 0.1 - img_w, M + 0.1
    tw = W - 2 * M - 0.2 - img_w - 0.5

    draw.shape(sl, ix + 0.18, iy + 0.18, img_w, img_h, t.rgb("accent"), alpha=0.35)
    pic = draw.picture_cover(sl, frame.path, ix, iy, img_w, img_h)
    pic.line.color.rgb = t.rgb("surface")
    pic.line.width = Pt(1)
    caption = ctx.labels["ai_image"] if illus else f"{ctx.labels['frame']} {_fmt_ts(frame.timestamp)}"
    draw.text(sl, ix, iy + img_h + 0.22, img_w, 0.35, caption, 11, t.rgb("muted"), t.body_font, italic=True)
    _bullets(ctx, sl, s, _take(ctx, s, s.key_points, 5), tx, CT + 0.1, tw, CH - 0.3, max_pt=22, min_pt=13,
             anchor=MSO_ANCHOR.MIDDLE)


def layout_two_column(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    gap = 0.5
    cw = (W - 2 * M - 0.2 - gap) / 2
    need = max(text_height_in(p[:6], cw - 0.5, 22, indent_in=0.32) for p in (s.left_points, s.right_points))
    card_h = min(CH - 0.1, max(3.2, 1.5 + need))
    top = CT + (CH - card_h) / 2
    for i, (head, pts, color) in enumerate(
        [(s.left_title, s.left_points, "primary"), (s.right_title, s.right_points, "accent")]
    ):
        x = M + 0.1 + i * (cw + gap)
        _card(ctx, sl, x, top, cw, card_h)
        band = draw.shape(sl, x, top, cw, 0.8, t.rgb(color),
                          MSO_SHAPE.ROUNDED_RECTANGLE if t.rounded else MSO_SHAPE.RECTANGLE,
                          radius=0.2 if t.rounded else None)
        paras, size = _fit(ctx, s.slide_number, [head or "—"], cw - 0.3, 0.7, 22, 14, bold=True)
        draw.text(sl, 0, 0, 0, 0, paras, size, t.rgb("on_primary"), t.title_font, bold=True,
                  anchor=MSO_ANCHOR.MIDDLE, target=band)
        _bullets(ctx, sl, s, _take(ctx, s, pts, 6), x + 0.25, top + 1.05, cw - 0.5, card_h - 1.25, max_pt=22, min_pt=12)


def layout_process(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    items = s.items[:6]
    n = len(items)
    gap = 0.45
    bw = (W - 2 * M - 0.2 - gap * (n - 1)) / n
    d = 0.62
    need = max(text_height_in([it.text], bw - 0.2, 17) if it.text else 0 for it in items)
    proc_h = min(CB - (CT + 0.1 + d + 0.3), max(2.6, 1.6 + need))
    cy = CT + (CH - (d + 0.3 + proc_h)) / 2
    # Connector line behind the step badges.
    line = sl.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(M + 0.1 + bw / 2), Inches(cy + d / 2),
                                   Inches(W - M - 0.1 - bw / 2), Inches(cy + d / 2))
    line.line.color.rgb = t.rgb("muted")
    line.line.width = Pt(1.5)
    for i, it in enumerate(items):
        x = M + 0.1 + i * (bw + gap)
        color = "accent" if i % 2 == 0 else "accent2"
        _badge(ctx, sl, x + (bw - d) / 2, cy, d, str(i + 1), color)
        card_y = cy + d + 0.3
        card_h = proc_h
        _card(ctx, sl, x, card_y, bw, card_h)
        draw.shape(sl, x, card_y, bw, 0.08, t.rgb(color))
        hp, hs = _fit(ctx, s.slide_number, [it.heading], bw - 0.2, 1.1, 20, 12, bold=True)
        draw.text(sl, x + 0.1, card_y + 0.2, bw - 0.2, 1.1, hp, hs, _title_color(t), t.title_font, bold=True)
        if it.text:
            tp, ts = _fit(ctx, s.slide_number, [it.text], bw - 0.2, card_h - 1.5, 18, 11)
            draw.text(sl, x + 0.1, card_y + 1.35, bw - 0.2, card_h - 1.5, tp, ts, t.rgb("text"), t.body_font)
        if i < n - 1:
            draw.shape(sl, x + bw + 0.08, card_y + card_h / 2 - 0.18, gap - 0.16, 0.36, t.rgb(color),
                       MSO_SHAPE.RIGHT_ARROW)


def layout_timeline(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    items = s.items[:6]
    n = len(items)
    slot = (W - 2 * M - 0.2) / n
    y_line = CT + CH * 0.45
    draw.shape(sl, M + 0.1, y_line - 0.03, W - 2 * M - 0.2, 0.06, t.rgb("muted"), alpha=0.6)
    for i, it in enumerate(items):
        cx = M + 0.1 + (i + 0.5) * slot
        color = "accent" if i % 2 == 0 else "accent2"
        draw.shape(sl, cx - 0.2, y_line - 0.2, 0.4, 0.4, t.rgb(color), MSO_SHAPE.OVAL)
        draw.shape(sl, cx - 0.08, y_line - 0.08, 0.16, 0.16, t.rgb("background"), MSO_SHAPE.OVAL)
        hp, hs = _fit(ctx, s.slide_number, [it.heading], slot - 0.2, y_line - CT - 0.45, 24, 12, bold=True)
        draw.text(sl, cx - slot / 2 + 0.1, CT + 0.15, slot - 0.2, y_line - CT - 0.45, hp, hs, t.rgb(color), t.title_font,
                  bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
        if it.text:
            top = y_line + 0.45
            tp, ts = _fit(ctx, s.slide_number, [it.text], slot - 0.2, CB - top, 18, 11)
            draw.text(sl, cx - slot / 2 + 0.1, top, slot - 0.2, CB - top, tp, ts, t.rgb("text"), t.body_font,
                      align=PP_ALIGN.CENTER)


def _grid(n: int) -> tuple[int, int]:
    cols = n if n <= 3 else (2 if n == 4 else 3)
    return cols, math.ceil(n / cols)


def layout_cards(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    items = s.items[:6]
    cols, rows = _grid(len(items))
    gap = 0.35
    cw = (W - 2 * M - 0.2 - gap * (cols - 1)) / cols
    max_ch = (CH - gap * (rows - 1)) / rows
    # Shrink cards to their content so short texts don't leave empty boxes.
    need = max(text_height_in([it.text], cw - 0.5, 17) if it.text else 0 for it in items)
    ch = min(max_ch, max(1.9, 1.45 + need))
    top = CT + (CH - (ch * rows + gap * (rows - 1))) / 2
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        x = M + 0.1 + c * (cw + gap)
        y = top + r * (ch + gap)
        color = "accent" if (r + c) % 2 == 0 else "accent2"
        _card(ctx, sl, x, y, cw, ch)
        draw.shape(sl, x, y, cw, 0.08, t.rgb(color))
        d = 0.55
        _badge(ctx, sl, x + 0.25, y + 0.3, d, str(i + 1), color)
        hp, hs = _fit(ctx, s.slide_number, [it.heading], cw - 1.15, 0.85, 20, 12, bold=True)
        draw.text(sl, x + 0.95, y + 0.25, cw - 1.15, 0.85, hp, hs, _title_color(t), t.title_font, bold=True,
                  anchor=MSO_ANCHOR.MIDDLE)
        if it.text:
            body_h = ch - 1.35
            tp, ts = _fit(ctx, s.slide_number, [it.text], cw - 0.5, body_h, 18, 11)
            draw.text(sl, x + 0.25, y + 1.2, cw - 0.5, body_h, tp, ts, t.rgb("text"), t.body_font)


def layout_stats(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    items = s.items[:6]
    cols, rows = _grid(len(items)) if len(items) != 4 else (4, 1)
    gap = 0.35
    cw = (W - 2 * M - 0.2 - gap * (cols - 1)) / cols
    ch = min(4.2, (CH - gap * (rows - 1)) / rows)
    top = CT + (CH - (ch * rows + gap * (rows - 1))) / 2
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        x = M + 0.1 + c * (cw + gap)
        y = top + r * (ch + gap)
        color = "accent" if i % 2 == 0 else "accent2"
        _card(ctx, sl, x, y, cw, ch)
        vh = ch * 0.45
        vp, vs = _fit(ctx, s.slide_number, [it.heading], cw - 0.3, vh, 54, 24, bold=True)
        draw.text(sl, x + 0.15, y + 0.2, cw - 0.3, vh, vp, vs, t.rgb(color), t.title_font, bold=True,
                  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
        draw.shape(sl, x + cw / 2 - 0.4, y + 0.3 + vh, 0.8, 0.05, t.rgb(color))
        if it.text:
            lh = ch - vh - 0.7
            lp, ls = _fit(ctx, s.slide_number, [it.text], cw - 0.4, lh, 18, 11)
            draw.text(sl, x + 0.2, y + vh + 0.5, cw - 0.4, lh, lp, ls, t.rgb("text"), t.body_font,
                      align=PP_ALIGN.CENTER)


def layout_summary(ctx: BuildContext, sl, s: Slide) -> None:
    t = ctx.theme
    chrome(ctx, sl, s)
    points = _take(ctx, s, s.key_points, 6)
    two_cols = len(points) > 3
    cols = 2 if two_cols else 1
    rows = math.ceil(len(points) / cols)
    gap = 0.3
    cw = (W - 2 * M - 0.2 - gap * (cols - 1)) / cols
    rh = min(1.3, (CH - gap * (rows - 1)) / rows)
    top = CT + (CH - (rh * rows + gap * (rows - 1))) / 2
    for i, p in enumerate(points):
        c, r = (i // rows, i % rows) if two_cols else (0, i)
        x = M + 0.1 + c * (cw + gap)
        y = top + r * (rh + gap)
        _card(ctx, sl, x, y, cw, rh)
        d = min(0.55, rh - 0.3)
        _badge(ctx, sl, x + 0.25, y + (rh - d) / 2, d, "✓", "accent2")
        tw = cw - 1.1
        pp, ps = _fit(ctx, s.slide_number, [p], tw, rh - 0.1, 20, 12)
        draw.text(sl, x + 0.95, y + 0.05, tw, rh - 0.1, pp, ps, t.rgb("text"), t.body_font,
                  anchor=MSO_ANCHOR.MIDDLE)


def layout_agenda(ctx: BuildContext, sl, s: Slide) -> None:
    """Overview: a colour panel with the title on the left, numbered parts on the right."""
    t = ctx.theme
    draw.set_background(sl, t.rgb("background"))
    panel_w = 4.3
    draw.shape(sl, 0, 0, panel_w, H, t.rgb("primary"))
    draw.shape(sl, -1.2, H - 2.0, 3.0, 3.0, t.rgb("accent2"), MSO_SHAPE.OVAL, alpha=0.22)  # stays inside the panel
    paras, size = _fit(ctx, s.slide_number, [s.title], panel_w - 1.0, 2.6, 40, 24, bold=True)
    draw.text(sl, M, 1.0, panel_w - 1.0, 2.6, paras, size, t.rgb("on_primary"), t.title_font, bold=True,
              anchor=MSO_ANCHOR.BOTTOM)
    draw.shape(sl, M + 0.1, 3.8, 1.1, 0.08, t.rgb("accent"))
    if s.subtitle:
        paras, size = _fit(ctx, s.slide_number, [s.subtitle], panel_w - 1.0, 1.8, 18, 12)
        draw.text(sl, M, 4.05, panel_w - 1.0, 1.8, paras, size, t.rgb("on_primary"), t.body_font)

    points = _take(ctx, s, s.key_points, 7)
    x0, x1 = panel_w + 0.8, W - M - 0.1
    top, bottom = 0.8, H - 0.8
    row_h = min(1.05, (bottom - top) / len(points))
    y = top + ((bottom - top) - row_h * len(points)) / 2
    num_w = 1.0
    for i, p in enumerate(points):
        color = "accent" if i % 2 == 0 else "accent2"
        draw.text(sl, x0, y, num_w, row_h, f"{i + 1:02d}", min(30, row_h * 34), t.rgb(color), t.title_font,
                  bold=True, anchor=MSO_ANCHOR.MIDDLE)
        tw = x1 - x0 - num_w
        pp, ps = _fit(ctx, s.slide_number, [p], tw, row_h - 0.1, 22, 13)
        draw.text(sl, x0 + num_w, y + 0.05, tw, row_h - 0.1, pp, ps, t.rgb("text"), t.body_font,
                  anchor=MSO_ANCHOR.MIDDLE)
        if i < len(points) - 1:
            draw.shape(sl, x0, y + row_h - 0.005, x1 - x0, 0.01, t.rgb("muted"), alpha=0.35)
        y += row_h


def layout_concept(ctx: BuildContext, sl, s: Slide) -> None:
    """A term explained: definition card + its characteristics, with the AI illustration when present."""
    t = ctx.theme
    chrome(ctx, sl, s)
    illus = ctx.illustrations.get(s.slide_id)
    parts = [(it.heading, it.text) for it in s.items[:4]] or [(p, "") for p in s.key_points[:4]]
    if illus:
        img_w = 5.6
        img_h = img_w * 9 / 16
        ix, iy = W - M - 0.1 - img_w, CT + (CH - 0.45 - img_h) / 2
        draw.shape(sl, ix + 0.18, iy + 0.18, img_w, img_h, t.rgb("accent"), alpha=0.35)
        draw.picture_cover(sl, illus, ix, iy, img_w, img_h)
        draw.text(sl, ix, iy + img_h + 0.22, img_w, 0.35, ctx.labels["ai_image"], 11, t.rgb("muted"), t.body_font,
                  italic=True)
        col_w = ix - M - 0.1 - 0.5
        def_h = 2.1 if parts else CH - 0.2
        list_box = (M + 0.1, CT + def_h + 0.3, col_w, CH - def_h - 0.4)
    else:
        col_w = 5.4
        def_h = CH - 0.2
        list_box = (M + 0.1 + col_w + 0.5, CT + 0.1, W - 2 * M - 0.7 - col_w, CH - 0.2)

    card = _card(ctx, sl, M + 0.1, CT + 0.1, col_w, def_h, fill_name="primary")
    card.name = "Definition"
    draw.text(sl, M + 0.4, CT + 0.3, col_w - 0.6, 0.4, ctx.labels["definition"], 12, t.rgb("accent"),
              t.body_font, bold=True)
    dp, ds = _fit(ctx, s.slide_number, [s.subtitle], col_w - 0.6, def_h - 0.9, 22 if illus else 28, 14)
    draw.text(sl, M + 0.4, CT + 0.75, col_w - 0.6, def_h - 0.9, dp, ds, t.rgb("on_primary"), t.title_font,
              anchor=MSO_ANCHOR.TOP if illus else MSO_ANCHOR.MIDDLE)
    if not parts:
        return
    lx, ly, lw, lh = list_box
    gap = 0.2
    row_h = min(1.35, (lh - gap * (len(parts) - 1)) / len(parts))
    y = ly + (lh - (row_h * len(parts) + gap * (len(parts) - 1))) / 2
    for i, (head, body) in enumerate(parts):
        color = "accent" if i % 2 == 0 else "accent2"
        draw.shape(sl, lx, y, 0.08, row_h, t.rgb(color))
        tw = lw - 0.3
        if body:
            hp, hs = _fit(ctx, s.slide_number, [head], tw, row_h * 0.42, 20, 12, bold=True)
            draw.text(sl, lx + 0.25, y, tw, row_h * 0.42, hp, hs, _title_color(t), t.title_font, bold=True,
                      anchor=MSO_ANCHOR.BOTTOM)
            bp, bs = _fit(ctx, s.slide_number, [body], tw, row_h * 0.58, 17, 11)
            draw.text(sl, lx + 0.25, y + row_h * 0.42, tw, row_h * 0.58, bp, bs, t.rgb("text"), t.body_font)
        else:
            hp, hs = _fit(ctx, s.slide_number, [head], tw, row_h, 20, 12)
            draw.text(sl, lx + 0.25, y, tw, row_h, hp, hs, t.rgb("text"), t.body_font, anchor=MSO_ANCHOR.MIDDLE)
        y += row_h + gap


def layout_key_insight(ctx: BuildContext, sl, s: Slide) -> None:
    """One emphasised message in large type, with up to three supporting points."""
    t = ctx.theme
    chrome(ctx, sl, s)
    support = _take(ctx, s, s.key_points, 3)
    box_h = CH - 0.2 if not support else 2.75
    x, y, w = M + 0.1, CT + 0.1, W - 2 * M - 0.2
    _card(ctx, sl, x, y, w, box_h)
    draw.shape(sl, x, y, 0.12, box_h, t.rgb("accent"))
    draw.text(sl, x + 0.45, y + 0.05, 1.2, 1.2, "“", 60, t.rgb("accent"), "Georgia", bold=True)
    draw.text(sl, x + 1.5, y + 0.25, 4, 0.4, ctx.labels["insight"], 12, t.rgb("accent"), t.body_font, bold=True)
    mp, ms = _fit(ctx, s.slide_number, [s.subtitle], w - 2.0, box_h - 0.95, 34, 18, bold=True)
    draw.text(sl, x + 1.5, y + 0.7, w - 2.0, box_h - 0.95, mp, ms, _title_color(t), t.title_font, bold=True,
              anchor=MSO_ANCHOR.MIDDLE)
    if not support:
        return
    gap = 0.35
    n = len(support)
    cw = (w - gap * (n - 1)) / n
    top = y + box_h + 0.35
    ch = CB - top
    for i, p in enumerate(support):
        cx = x + i * (cw + gap)
        color = "accent" if i % 2 == 0 else "accent2"
        draw.shape(sl, cx, top, cw, 0.06, t.rgb(color))
        pp, ps = _fit(ctx, s.slide_number, [p], cw - 0.2, ch - 0.2, 20, 12)
        draw.text(sl, cx, top + 0.2, cw - 0.2, ch - 0.2, pp, ps, t.rgb("text"), t.body_font)


def layout_conclusion(ctx: BuildContext, sl, s: Slide) -> None:
    """Closing slide: the final message on the brand colour, with up to three closing statements."""
    t = ctx.theme
    draw.set_background(sl, t.rgb("primary"))
    draw.shape(sl, -1.5, H - 3.2, 4.5, 4.5, t.rgb("accent2"), MSO_SHAPE.OVAL, alpha=0.2)
    draw.shape(sl, W - 3.6, -1.4, 5.0, 5.0, t.rgb("accent"), MSO_SHAPE.OVAL, alpha=0.18)
    points = _take(ctx, s, s.key_points, 3)
    text_w = 5.6 if points else W - 2 * M - 2.5
    x = M + 0.2
    paras, size = _fit(ctx, s.slide_number, [s.title], text_w, 2.6, 44, 26, bold=True)
    draw.text(sl, x, 1.3, text_w, 2.6, paras, size, t.rgb("on_primary"), t.title_font, bold=True,
              anchor=MSO_ANCHOR.BOTTOM)
    draw.shape(sl, x + 0.1, 4.1, 1.3, 0.08, t.rgb("accent"))
    if s.subtitle:
        paras, size = _fit(ctx, s.slide_number, [s.subtitle], text_w, 1.8, 20, 13)
        draw.text(sl, x, 4.35, text_w, 1.8, paras, size, t.rgb("on_primary"), t.body_font)
    if not points:
        return
    px = x + text_w + 0.6
    pw = W - M - px
    gap = 0.3
    rh = min(1.5, (H - 2.4 - gap * (len(points) - 1)) / len(points))
    y = (H - (rh * len(points) + gap * (len(points) - 1))) / 2
    for i, p in enumerate(points):
        panel = draw.shape(sl, px, y, pw, rh, t.rgb("on_primary"),
                           MSO_SHAPE.ROUNDED_RECTANGLE if t.rounded else MSO_SHAPE.RECTANGLE,
                           radius=0.12 if t.rounded else None, alpha=0.1)
        panel.name = "Closing point"
        d = min(0.55, rh - 0.4)
        _badge(ctx, sl, px + 0.3, y + (rh - d) / 2, d, "✓", "accent")
        pp, ps = _fit(ctx, s.slide_number, [p], pw - 1.2, rh - 0.15, 20, 12)
        draw.text(sl, px + 1.05, y + 0.075, pw - 1.2, rh - 0.15, pp, ps, t.rgb("on_primary"), t.body_font,
                  anchor=MSO_ANCHOR.MIDDLE)
        y += rh + gap


LAYOUTS = {
    "title": layout_title,
    "agenda": layout_agenda,
    "concept": layout_concept,
    "key_insight": layout_key_insight,
    "conclusion": layout_conclusion,
    "section": layout_section,
    "bullets": layout_bullets,
    "image_left": lambda c, sl, s: layout_image(c, sl, s, "left"),
    "image_right": lambda c, sl, s: layout_image(c, sl, s, "right"),
    "two_column": layout_two_column,
    "process": layout_process,
    "timeline": layout_timeline,
    "cards": layout_cards,
    "stats": layout_stats,
    "summary": layout_summary,
}


# --------------------------------------------------------------------------- entry point

def apply_theme_definition(prs, theme: Theme) -> None:
    """Write the deck's colours and fonts into the slide master's theme.

    Slides are drawn with explicit formatting, but slides, text boxes and shapes the user
    adds later in PowerPoint inherit from the theme, so they match the deck's style.
    """
    part = prs.slide_master.part.part_related_by(RT.THEME)
    root = etree.fromstring(part.blob)
    elements = root.find(qn("a:themeElements"))
    scheme = elements.find(qn("a:clrScheme"))
    scheme.set("name", f"Video2Slides {theme.label}")
    colors = {"dk1": theme.text, "lt1": theme.background, "dk2": theme.primary, "lt2": theme.surface,
              "accent1": theme.accent, "accent2": theme.accent2, "accent3": theme.primary,
              "accent4": theme.muted, "accent5": theme.accent, "accent6": theme.accent2,
              "hlink": theme.accent, "folHlink": theme.accent2}
    for name, hex_value in colors.items():
        slot = scheme.find(qn(f"a:{name}"))
        for child in list(slot):
            slot.remove(child)
        etree.SubElement(slot, qn("a:srgbClr")).set("val", hex_value)
    fonts = elements.find(qn("a:fontScheme"))
    fonts.set("name", f"Video2Slides {theme.label}")
    fonts.find(qn("a:majorFont")).find(qn("a:latin")).set("typeface", theme.title_font)
    fonts.find(qn("a:minorFont")).find(qn("a:latin")).set("typeface", theme.body_font)
    part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def build_pptx(pres: Presentation, out_path: Path, style: str = "professional",
               frames: list[FrameRef] | None = None, illustrations: dict[str, Path] | None = None) -> list[str]:
    """Render the deck to ``out_path``. Returns a list of non-fatal layout warnings.

    ``illustrations`` maps slide ids to image files from any image provider; they take
    precedence over video frames on title and image slides.
    """
    frames = sorted(frames or [], key=lambda f: f.timestamp)
    illustrations = {k: v for k, v in (illustrations or {}).items() if v and Path(v).is_file()}
    theme = get_theme(style)
    plan_layouts(pres, frames, set(illustrations))
    ctx = BuildContext(theme=theme, deck_title=pres.presentation_title, language=pres.language,
                       frames=frames, total=len(pres.slides), illustrations=illustrations)

    prs = PptxPresentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    apply_theme_definition(prs, theme)
    blank = prs.slide_layouts[6]
    prs.core_properties.title = pres.presentation_title
    prs.core_properties.subject = pres.subtitle or pres.presentation_title

    for s in pres.slides:
        sl = prs.slides.add_slide(blank)
        LAYOUTS.get(s.layout_type, layout_bullets)(ctx, sl, s)
        notes = s.speaker_notes.strip()
        if s.source_timestamps:
            stamps = ", ".join(_fmt_ts(x) for x in sorted(set(s.source_timestamps))[:6])
            notes = f"{notes}\n\n{ctx.labels['sources']}: {stamps}".strip()
        if notes:
            sl.notes_slide.notes_text_frame.text = notes

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return ctx.warnings
