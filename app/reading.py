"""Per-slide reading content shared by the PPTX notes, Google Slides notes and Reading Mode.

The outline (``outline.json``) is the source of truth. Outlines produced before the
reading feature (or by the offline provider) may lack ``reading_notes``; for those the
explanation is derived from the transcript spans that support the slide, so every slide
still has a matching explanation.
"""

from __future__ import annotations

import re

from .ai.schema import Presentation, Slide, TimeRange
from .citations import Matcher, point_sources
from .transcribe import Transcript

LABELS = {
    "vi": {"details": "Giải thích chi tiết", "examples": "Ví dụ", "takeaway": "Ý chính cần nhớ",
           "sources": "Thời điểm trong video"},
    "en": {"details": "Detailed explanation", "examples": "Examples", "takeaway": "Key takeaway",
           "sources": "Video timestamps"},
}
NEARBY_SEC = 20.0
EXCERPT_CHARS = 1500
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def labels(language: str) -> dict[str, str]:
    return LABELS.get(language, LABELS["en"])


def fmt_clock(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def slide_ranges(slide: Slide) -> list[TimeRange]:
    """Source spans of a slide: explicit ranges, else a window around each timestamp."""
    if slide.source_ranges:
        return sorted(slide.source_ranges, key=lambda r: r.start)
    return [TimeRange(start=max(0.0, t - NEARBY_SEC / 2), end=t + NEARBY_SEC)
            for t in sorted(set(slide.source_timestamps))]


def transcript_excerpt(transcript: Transcript | None, slide: Slide, limit: int = EXCERPT_CHARS) -> str:
    """Transcript text spoken during the slide's source spans (empty when unknown)."""
    if transcript is None:
        return ""
    ranges = slide_ranges(slide)
    if not ranges:
        return ""
    parts = [seg.text.strip() for seg in transcript.segments
             if any(seg.end >= r.start and seg.start <= r.end for r in ranges) and seg.text.strip()]
    text = " ".join(parts)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _first_sentences(text: str, max_words: int) -> str:
    out: list[str] = []
    for sent in _SENT_SPLIT.split(text.strip()):
        if out and len(" ".join(out + [sent]).split()) > max_words:
            break
        out.append(sent)
    words = " ".join(out).split()
    return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")


def ensure_reading_notes(pres: Presentation, transcript: Transcript | None) -> int:
    """Fill missing reading content from the transcript. Returns how many slides were filled.

    Only empty fields are touched, so LLM-written explanations are never overwritten.
    """
    filled = 0
    for s in pres.slides:
        rn = s.reading_notes
        if not rn.is_empty() and s.brief_explanation:
            continue
        excerpt = transcript_excerpt(transcript, s)
        # Last resort (e.g. an overview slide without source spans): the slide's own points.
        source = excerpt or s.speaker_notes or " ".join(
            p if p.endswith((".", "!", "?", "…")) else f"{p}." for p in _main_points(s))
        if not source:
            continue
        if not rn.detailed_explanation:
            rn.detailed_explanation = source
        if not rn.summary:
            rn.summary = _first_sentences(s.speaker_notes or source, 30)
        if not s.brief_explanation:
            s.brief_explanation = _first_sentences(s.speaker_notes or source, 30)
        filled += 1
    return filled


def speaker_notes_text(slide: Slide, language: str) -> str:
    """Full narration for the slide's speaker notes (same text in PPTX and Google Slides)."""
    L = labels(language)
    rn = slide.reading_notes
    blocks: list[str] = []
    lead = slide.speaker_notes.strip() or rn.summary
    if lead:
        blocks.append(lead)
    if rn.detailed_explanation and rn.detailed_explanation.strip() != lead:
        blocks.append(f"{L['details']}:\n{rn.detailed_explanation}")
    if rn.examples:
        blocks.append(f"{L['examples']}:\n" + "\n".join(f"• {e}" for e in rn.examples))
    if rn.key_takeaway:
        blocks.append(f"{L['takeaway']}: {rn.key_takeaway}")
    spans = _span_labels(slide)
    if spans:
        blocks.append(f"{L['sources']}: {', '.join(spans)}")
    return "\n\n".join(blocks).strip()


def _span_labels(slide: Slide) -> list[str]:
    if slide.source_ranges:
        return [f"{fmt_clock(r.start)}–{fmt_clock(r.end)}" for r in sorted(slide.source_ranges, key=lambda r: r.start)][:6]
    return [fmt_clock(x) for x in sorted(set(slide.source_timestamps))[:6]]


def slide_payload(slide: Slide, transcript: Transcript | None, matcher: Matcher | None = None) -> dict:
    """JSON for one slide in Reading Mode (``matcher``: reuse one transcript index for a whole deck)."""
    rn = slide.reading_notes
    ranges = slide_ranges(slide)
    points = _main_points(slide)
    if matcher is None:
        matcher = Matcher(transcript)
    return {
        "slide_id": slide.slide_id,
        "slide_number": slide.slide_number,
        "title": slide.title,
        "layout_type": slide.layout_type,
        "main_points": points,
        # Per-point citations by word matching (no LLM); start is None when no passage matches well.
        "point_sources": point_sources(points, [(r.start, r.end) for r in ranges], matcher),
        "brief_explanation": slide.brief_explanation,
        "reading_notes": {
            "summary": rn.summary,
            "detailed_explanation": rn.detailed_explanation,
            "examples": rn.examples,
            "key_takeaway": rn.key_takeaway,
        },
        "speaker_notes": slide.speaker_notes,
        "source_timestamps": [{"start": round(r.start, 1), "end": round(r.end, 1)} for r in ranges],
        "transcript_excerpt": transcript_excerpt(transcript, slide),
    }


def _main_points(slide: Slide) -> list[str]:
    if slide.key_points:
        return list(slide.key_points)
    if slide.items:
        return [f"{i.heading}: {i.text}".strip(": ") for i in slide.items]
    pts = [f"{slide.left_title}: {p}" if slide.left_title else p for p in slide.left_points]
    pts += [f"{slide.right_title}: {p}" if slide.right_title else p for p in slide.right_points]
    return pts or ([slide.subtitle] if slide.subtitle else [])
