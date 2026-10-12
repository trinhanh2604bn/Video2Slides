"""Conservative text measurement used to pick font sizes that never overflow.

We cannot rely on PowerPoint's autofit (it only recalculates when a user edits
the slide), so we estimate wrapped height with an average glyph width and shrink
the font until the text fits its box. Estimates are deliberately pessimistic.
"""

from __future__ import annotations

import math

# Average glyph advance as a fraction of the font size (em). Vietnamese text in
# Calibri/Cambria/Segoe averages ~0.48-0.52 em; 0.55 leaves a safety margin.
CHAR_EM = 0.55
BOLD_EM = 0.60
LINE_SPACING = 1.22
PARA_GAP_EM = 0.45
INSET_IN = 0.1


def wrapped_lines(text: str, width_in: float, size_pt: float, bold: bool = False) -> int:
    usable_pt = max((width_in - 2 * INSET_IN) * 72, 1)
    char_pt = size_pt * (BOLD_EM if bold else CHAR_EM)
    per_line = max(int(usable_pt / char_pt), 1)
    lines, current = 1, 0
    for word in text.split():
        n = len(word)
        needed = n if current == 0 else current + 1 + n
        if needed <= per_line:
            current = needed
            continue
        if current > 0:
            lines += 1
        # A word longer than a line breaks across several lines.
        extra = (n - 1) // per_line
        lines += extra
        current = n - extra * per_line
    return lines


def text_height_in(paragraphs: list[str], width_in: float, size_pt: float, bold: bool = False,
                   indent_in: float = 0.0) -> float:
    if not paragraphs:
        return 0.0
    total_lines = sum(wrapped_lines(p, width_in - indent_in, size_pt, bold) for p in paragraphs)
    height_pt = total_lines * size_pt * LINE_SPACING + (len(paragraphs) - 1) * size_pt * PARA_GAP_EM
    return height_pt / 72 + 2 * 0.05


def fit_size(paragraphs: list[str], width_in: float, height_in: float, max_pt: float, min_pt: float,
             bold: bool = False, indent_in: float = 0.0) -> tuple[float, bool]:
    """Largest size in [min_pt, max_pt] that fits. Returns (size, fits)."""
    size = max_pt
    while size >= min_pt:
        if text_height_in(paragraphs, width_in, size, bold, indent_in) <= height_in:
            return size, True
        size -= 1
    return min_pt, False


def fit_or_trim(paragraphs: list[str], width_in: float, height_in: float, max_pt: float, min_pt: float,
                bold: bool = False, indent_in: float = 0.0) -> tuple[list[str], float]:
    """Fit text; if it cannot fit at ``min_pt``, drop trailing paragraphs, then shorten the last one."""
    paras = [p for p in paragraphs if p]
    while paras:
        size, ok = fit_size(paras, width_in, height_in, max_pt, min_pt, bold, indent_in)
        if ok:
            return paras, size
        if len(paras) > 1:
            paras = paras[:-1]
            continue
        words = paras[0].split()
        if len(words) <= 3:
            return paras, min_pt
        paras = [" ".join(words[: math.ceil(len(words) * 0.75)]) + "…"]
    return paras, min_pt
