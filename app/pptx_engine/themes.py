"""Reusable visual themes. Fonts were chosen for full Vietnamese glyph coverage."""

from __future__ import annotations

from dataclasses import dataclass

from pptx.dml.color import RGBColor


def _rgb(hex_str: str) -> RGBColor:
    return RGBColor.from_string(hex_str.lstrip("#"))


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    background: str     # slide background
    surface: str        # cards / panels
    primary: str        # title slides, headers
    accent: str         # highlights, markers
    accent2: str        # secondary highlight
    text: str           # body text on background
    muted: str          # captions, footers
    on_primary: str     # text drawn on primary color
    title_font: str
    body_font: str
    dark: bool = False
    rounded: bool = True

    def rgb(self, name: str) -> RGBColor:
        return _rgb(getattr(self, name))


THEMES: dict[str, Theme] = {
    "professional": Theme(
        key="professional",
        label="Professional",
        background="FFFFFF",
        surface="F1F4F9",
        primary="1E3A5F",
        accent="2F80ED",
        accent2="14B8A6",
        text="1F2937",
        muted="6B7280",
        on_primary="FFFFFF",
        title_font="Calibri",
        body_font="Calibri",
    ),
    "academic": Theme(
        key="academic",
        label="Academic",
        background="FBFAF6",
        surface="F1ECE1",
        primary="6B1D2A",
        accent="B08D57",
        accent2="3F5E5A",
        text="2B2B2B",
        muted="6F6A60",
        on_primary="FBFAF6",
        title_font="Cambria",
        body_font="Calibri",
        rounded=False,
    ),
    "creative": Theme(
        key="creative",
        label="Creative",
        background="111827",
        surface="1F2937",
        primary="312E81",
        accent="F472B6",
        accent2="A78BFA",
        text="F9FAFB",
        muted="CBD5E1",
        on_primary="FFFFFF",
        title_font="Segoe UI Semibold",
        body_font="Segoe UI",
        dark=True,
    ),
}

DEFAULT_THEME = "professional"


def get_theme(key: str | None) -> Theme:
    return THEMES.get((key or DEFAULT_THEME).lower(), THEMES[DEFAULT_THEME])
