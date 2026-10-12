"""Presentation outline schema shared by every AI provider and the PPTX engine."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

LayoutType = Literal[
    "title",
    "section",
    "bullets",
    "image_left",
    "image_right",
    "two_column",
    "process",
    "timeline",
    "cards",
    "stats",
    "summary",
]
VisualType = Literal["video_frame", "diagram", "icon", "none"]

LAYOUT_TYPES: tuple[str, ...] = LayoutType.__args__  # type: ignore[attr-defined]
VISUAL_TYPES: tuple[str, ...] = VisualType.__args__  # type: ignore[attr-defined]


class Item(BaseModel):
    """A step, card, timeline event or statistic. For ``stats`` the heading is the value."""

    heading: str = Field(max_length=80)
    text: str = Field(default="", max_length=220)


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
        if self.layout_type in ("bullets", "image_left", "image_right", "summary") and not self.key_points:
            if self.items:
                self.key_points = [f"{i.heading}: {i.text}".strip(": ") for i in self.items]
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

    @model_validator(mode="after")
    def _renumber(self) -> "Presentation":
        self.slides = [s for s in self.slides if s.has_body()]
        if len(self.slides) < 2:
            raise ValueError("presentation must contain at least 2 non-empty slides")
        for idx, s in enumerate(self.slides, start=1):
            s.slide_number = idx
        return self


def gemini_response_schema() -> dict:
    """OpenAPI-subset schema accepted by Gemini's ``responseSchema``."""
    string = {"type": "STRING"}
    str_list = {"type": "ARRAY", "items": string}
    item = {
        "type": "OBJECT",
        "properties": {"heading": string, "text": string},
        "required": ["heading"],
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
        },
        "required": ["slide_number", "title", "layout_type", "speaker_notes", "visual_type"],
        "propertyOrdering": [
            "slide_number", "title", "subtitle", "layout_type", "key_points", "items",
            "left_title", "left_points", "right_title", "right_points",
            "speaker_notes", "visual_type", "visual_description", "source_timestamps",
        ],
    }
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
