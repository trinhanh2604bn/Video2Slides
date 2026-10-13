"""Presentation outline schema shared by every AI provider and the PPTX engine."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

LayoutType = Literal[
    "title",
    "agenda",
    "section",
    "bullets",
    "concept",
    "key_insight",
    "image_left",
    "image_right",
    "two_column",
    "process",
    "timeline",
    "cards",
    "stats",
    "summary",
    "conclusion",
]
VisualType = Literal["video_frame", "illustration", "diagram", "icon", "none"]

LAYOUT_TYPES: tuple[str, ...] = LayoutType.__args__  # type: ignore[attr-defined]
VISUAL_TYPES: tuple[str, ...] = VisualType.__args__  # type: ignore[attr-defined]


class Item(BaseModel):
    """A step, card, timeline event or statistic. For ``stats`` the heading is the value."""

    heading: str = Field(max_length=80)
    text: str = Field(default="", max_length=220)


def _clean(text: str, limit: int) -> str:
    """Normalise whitespace but keep paragraphs (blank-line separated); trim instead of rejecting."""
    paras = [" ".join(line.split()) for line in (text or "").splitlines()]
    return "\n\n".join(p for p in paras if p)[:limit].rstrip()


class TimeRange(BaseModel):
    """A span of the source video (seconds) that supports a slide."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def _order(self) -> "TimeRange":
        if self.end < self.start:
            self.start, self.end = self.end, self.start
        return self


class ReadingNotes(BaseModel):
    """Explanation shown beside the slide in Reading Mode (and written into the speaker notes)."""

    summary: str = ""
    detailed_explanation: str = ""
    examples: list[str] = Field(default_factory=list)
    key_takeaway: str = ""

    @field_validator("summary", "key_takeaway")
    @classmethod
    def _short(cls, v: str) -> str:
        return _clean(v, 600)

    @field_validator("detailed_explanation")
    @classmethod
    def _long(cls, v: str) -> str:
        return _clean(v, 4000)

    @field_validator("examples")
    @classmethod
    def _examples(cls, v: list[str]) -> list[str]:
        return [" ".join(e.split())[:300] for e in v if e and e.strip()][:6]

    def is_empty(self) -> bool:
        return not (self.summary or self.detailed_explanation or self.examples or self.key_takeaway)


class Slide(BaseModel):
    slide_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    layout_type: LayoutType = "bullets"
    key_points: list[str] = Field(default_factory=list, max_length=8)
    items: list[Item] = Field(default_factory=list, max_length=6)
    left_title: str = Field(default="", max_length=60)
    left_points: list[str] = Field(default_factory=list, max_length=6)
    right_title: str = Field(default="", max_length=60)
    right_points: list[str] = Field(default_factory=list, max_length=6)
    speaker_notes: str = Field(default="", max_length=3000)
    visual_type: VisualType = "none"
    visual_description: str = Field(default="", max_length=300)
    source_timestamps: list[float] = Field(default_factory=list, max_length=10)
    # English scene description for the AI illustration model (empty = no illustration wanted).
    image_prompt: str = ""
    # Reading experience (all optional so outlines generated before this feature still load).
    brief_explanation: str = ""   # 1-2 sentences shown beside the slide preview in Reading Mode
    reading_notes: ReadingNotes = Field(default_factory=ReadingNotes)
    source_ranges: list[TimeRange] = Field(default_factory=list, max_length=10)

    @property
    def slide_id(self) -> str:
        """Stable id used to match illustrations to slides (outlines are renumbered once, then cached)."""
        return f"slide-{self.slide_number:02d}"

    @field_validator("image_prompt")
    @classmethod
    def _trim_image_prompt(cls, v: str) -> str:
        return " ".join((v or "").split())[:400]

    @field_validator("brief_explanation")
    @classmethod
    def _trim_brief(cls, v: str) -> str:
        return _clean(v, 400)

    @field_validator("source_ranges", mode="before")
    @classmethod
    def _cap_ranges(cls, v):
        return (v or [])[:10]

    @field_validator("key_points", "left_points", "right_points")
    @classmethod
    def _strip_points(cls, v: list[str]) -> list[str]:
        return [p.strip()[:240] for p in v if p and p.strip()]

    @model_validator(mode="after")
    def _check_content(self) -> "Slide":
        lt = self.layout_type
        if lt in ("process", "timeline", "cards", "stats") and len(self.items) < 2:
            # Degrade gracefully instead of rejecting the whole outline.
            if self.items or self.key_points:
                self.key_points = self.key_points or [f"{i.heading}: {i.text}".strip(": ") for i in self.items]
                self.items = []
                self.layout_type = "bullets"
        if lt == "two_column" and not (self.left_points and self.right_points):
            self.key_points = self.key_points or self.left_points + self.right_points
            self.layout_type = "bullets"
        text_layouts = ("bullets", "image_left", "image_right", "summary", "agenda", "conclusion")
        if self.layout_type in text_layouts and not self.key_points and self.items:
            self.key_points = [f"{i.heading}: {i.text}".strip(": ") for i in self.items]
        if self.layout_type == "agenda" and len(self.key_points) < 2:
            self.layout_type = "bullets"
        if self.layout_type == "key_insight" and not self.subtitle:
            if self.key_points:
                # The highlighted statement is the subtitle; the remaining points support it.
                self.subtitle, self.key_points = self.key_points[0], self.key_points[1:]
            else:
                self.layout_type = "bullets"
        if self.layout_type == "concept" and not self.subtitle:
            if self.key_points:
                self.subtitle, self.key_points = self.key_points[0], self.key_points[1:]
            else:
                self.layout_type = "cards" if len(self.items) >= 2 else "bullets"
        return self

    def has_body(self) -> bool:
        return bool(
            self.key_points or self.items or self.left_points or self.right_points or self.subtitle
            or self.layout_type in ("title", "section")
        )


class Presentation(BaseModel):
    presentation_title: str = Field(min_length=1, max_length=150)
    subtitle: str = Field(default="", max_length=200)
    language: str = Field(default="vi", max_length=10)
    slides: list[Slide] = Field(min_length=2, max_length=30)

    @field_validator("language", mode="before")
    @classmethod
    def _language_code(cls, v) -> str:
        """Models sometimes answer "Vietnamese" / "vi-VN"; labels and notes need the bare code."""
        low = str(v or "").strip().lower()
        if low.startswith(("vi", "tiếng việt", "tieng viet")):
            return "vi"
        if low.startswith(("en", "english")):
            return "en"
        return low[:10] or "vi"

    @model_validator(mode="after")
    def _renumber(self) -> "Presentation":
        self.slides = [s for s in self.slides if s.has_body()]
        if len(self.slides) < 2:
            raise ValueError("presentation must contain at least 2 non-empty slides")
        for idx, s in enumerate(self.slides, start=1):
            s.slide_number = idx
        return self


def gemini_response_schema(include_image_prompt: bool = False) -> dict:
    """OpenAPI-subset schema accepted by Gemini's ``responseSchema``.

    ``include_image_prompt`` adds the per-slide ``image_prompt`` field (only when
    AI illustrations are requested, so the default outline request is unchanged).
    """
    string = {"type": "STRING"}
    str_list = {"type": "ARRAY", "items": string}
    item = {
        "type": "OBJECT",
        "properties": {"heading": string, "text": string},
        "required": ["heading"],
    }
    time_range = {
        "type": "OBJECT",
        "properties": {"start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}},
        "required": ["start", "end"],
    }
    reading = {
        "type": "OBJECT",
        "properties": {"summary": string, "detailed_explanation": string, "examples": str_list,
                       "key_takeaway": string},
        "required": ["summary", "detailed_explanation"],
        "propertyOrdering": ["summary", "detailed_explanation", "examples", "key_takeaway"],
    }
    slide = {
        "type": "OBJECT",
        "properties": {
            "slide_number": {"type": "INTEGER"},
            "title": string,
            "subtitle": string,
            "layout_type": {"type": "STRING", "enum": list(LAYOUT_TYPES)},
            "key_points": str_list,
            "items": {"type": "ARRAY", "items": item},
            "left_title": string,
            "left_points": str_list,
            "right_title": string,
            "right_points": str_list,
            "speaker_notes": string,
            "visual_type": {"type": "STRING", "enum": list(VISUAL_TYPES)},
            "visual_description": string,
            "source_timestamps": {"type": "ARRAY", "items": {"type": "NUMBER"}},
            "source_ranges": {"type": "ARRAY", "items": time_range},
            "brief_explanation": string,
            "reading_notes": reading,
        },
        "required": ["slide_number", "title", "layout_type", "speaker_notes", "visual_type",
                     "brief_explanation", "reading_notes"],
        "propertyOrdering": [
            "slide_number", "title", "subtitle", "layout_type", "key_points", "items",
            "left_title", "left_points", "right_title", "right_points",
            "speaker_notes", "visual_type", "visual_description", "source_timestamps", "source_ranges",
            "brief_explanation", "reading_notes",
        ],
    }
    if include_image_prompt:
        slide["properties"]["image_prompt"] = string
        slide["propertyOrdering"].append("image_prompt")
    return {
        "type": "OBJECT",
        "properties": {
            "presentation_title": string,
            "subtitle": string,
            "language": string,
            "slides": {"type": "ARRAY", "items": slide},
        },
        "required": ["presentation_title", "language", "slides"],
        "propertyOrdering": ["presentation_title", "subtitle", "language", "slides"],
    }
