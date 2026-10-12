"""Offline, non-LLM fallback provider.

Used when no AI key is configured (and in tests). It splits the transcript into
time-based chapters and extracts short sentences. Output is clearly weaker than
an LLM outline, so the UI shows a warning when this provider is used.
"""

from __future__ import annotations

import re

from .base import OutlineProvider, OutlineRequest, recommend_slide_count
from .schema import Item, Presentation, Slide

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
LABELS = {
    "vi": {"overview": "Nội dung chính", "summary": "Tóm tắt", "part": "Phần", "src": "Tóm tắt từ video"},
    "en": {"overview": "Overview", "summary": "Key takeaways", "part": "Part", "src": "Summary of the video"},
}


def _clock(sec: float) -> str:
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def _shorten(text: str, words: int = 14) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


class OfflineProvider(OutlineProvider):
    name = "offline"
    is_llm = False

    def generate(self, req: OutlineRequest) -> Presentation:
        t = req.transcript
        lang = req.language if req.language in LABELS else ("vi" if t.language == "vi" else "en")
        L = LABELS[lang]
        total = req.slide_count or recommend_slide_count(t.duration, len(t.text.split()))
        n_content = max(1, total - 3)  # title, overview, summary

        segs = t.segments
        chunk = max(1, len(segs) // n_content)
        chapters = [segs[i : i + chunk] for i in range(0, len(segs), chunk)][:n_content]

        title = req.title_hint or _shorten(segs[0].text, 8)
        slides: list[Slide] = [
            Slide(slide_number=1, title=title, subtitle=L["src"], layout_type="title",
                  speaker_notes=_shorten(t.text, 60))
        ]
        chapter_titles = []
        content: list[Slide] = []
        frames = list(req.frame_timestamps)
        for idx, ch in enumerate(chapters):
            text = " ".join(s.text for s in ch)
            sentences = [s for s in _SENT_SPLIT.split(text) if len(s.split()) >= 4] or [text]
            points = [_shorten(s) for s in sentences[:4]]
            ch_title = f"{L['part']} {idx + 1}: {_shorten(ch[0].text, 5)}"
            chapter_titles.append(ch_title)
            start, end = ch[0].start, ch[-1].end
            frame = next((f for f in frames if start <= f <= end), None)
            if frame is not None:
                layout, visual, ts = ("image_right" if idx % 2 else "image_left"), "video_frame", [frame]
            else:
                layout, visual, ts = "bullets", "none", [start]
            content.append(
                Slide(slide_number=idx + 2, title=ch_title, layout_type=layout, key_points=points,
                      speaker_notes=text[:2500], visual_type=visual, source_timestamps=ts)
            )

        slides.append(
            Slide(slide_number=2, title=L["overview"], layout_type="cards" if 2 <= len(chapter_titles) <= 6 else "bullets",
                  items=[Item(heading=c.split(": ", 1)[-1], text=f"{_clock(ch[0].start)} – {_clock(ch[-1].end)}")
                         for c, ch in zip(chapter_titles, chapters)]
                  if 2 <= len(chapter_titles) <= 6 else [],
                  key_points=chapter_titles[:6], visual_type="diagram")
        )
        slides.extend(content)
        slides.append(
            Slide(slide_number=len(slides) + 1, title=L["summary"], layout_type="summary",
                  key_points=[s.key_points[0] for s in content if s.key_points][:5])
        )
        return Presentation(presentation_title=title, subtitle=L["src"], language=lang, slides=slides)
