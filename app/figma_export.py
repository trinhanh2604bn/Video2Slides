"""Figma-ready SVG of every slide (Figma Design cannot import PDF; it imports SVG as editable layers).

Pipeline: the finished .pptx -> vector PDF (``preview.render_pdf``: PowerPoint or LibreOffice) -> one SVG per
page with PyMuPDF, text kept as ``<text>`` (one editable text layer per line, real font family, size and
colour), shapes as vector paths, pictures embedded. Dragging the SVG files into Figma gives one frame per slide;
pasting one slide's SVG markup (Ctrl+V) does the same. Rendered on demand, never part of the main pipeline, and
cached until the deck changes.

PyMuPDF is AGPL-licensed; the owner accepted that for this app (2026-10-14).
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from . import preview

DIR_NAME = "figma"
ZIP_NAME = "figma-slides.zip"
_lock = threading.Lock()

# MuPDF positions every glyph (x="50.4 71.9 81.3 ..."). Figma reads only a single x per tspan, so keep the
# first one and let the font lay out the line, which matches the PDF because it is the same font.
_X_LIST = re.compile(r'(<tspan\b[^>]*?\bx=")([-\d.eE]+)(?:\s+[-\d.eE]+)+(")')


class FigmaExportError(Exception):
    """User-safe failure."""


def svg_dir(job_dir: Path) -> Path:
    return job_dir / DIR_NAME


def clean_svg(svg: str) -> str:
    return _X_LIST.sub(r"\1\2\3", svg)


def _cached(job_dir: Path, pptx: Path) -> list[Path]:
    d = svg_dir(job_dir)
    files = sorted(d.glob("slide-*.svg")) if d.is_dir() else []
    if not files or min(f.stat().st_mtime for f in files) < pptx.stat().st_mtime:
        return []
    return files


def render_svgs(job_dir: Path, pptx: Path) -> list[Path]:
    """One SVG per slide (``figma/slide-01.svg`` ...). Raises FigmaExportError."""
    try:
        import pymupdf
    except ImportError as exc:
        raise FigmaExportError("Máy chủ chưa cài PyMuPDF nên chưa xuất được SVG cho Figma.") from exc
    with _lock:
        if files := _cached(job_dir, pptx):
            return files
        try:
            pdf = preview.render_pdf(job_dir, pptx)
        except preview.PreviewError as exc:
            raise FigmaExportError(str(exc)) from exc
        tmp = Path(tempfile.mkdtemp(prefix="figma-", dir=job_dir))
        try:
            with pymupdf.open(pdf) as doc:
                for i, page in enumerate(doc, start=1):
                    svg = clean_svg(page.get_svg_image(text_as_path=False))
                    (tmp / f"slide-{i:02d}.svg").write_text(svg, encoding="utf-8")
            if not any(tmp.glob("slide-*.svg")):
                raise FigmaExportError("Không tạo được SVG nào.")
            (job_dir / ZIP_NAME).unlink(missing_ok=True)
            shutil.rmtree(svg_dir(job_dir), ignore_errors=True)
            tmp.replace(svg_dir(job_dir))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return sorted(svg_dir(job_dir).glob("slide-*.svg"))


COLUMNS = 4   # slides per row when the whole deck is pasted at once
GAP = 80      # px between slides, like a Figma slide grid

_SVG_OPEN = re.compile(r"<svg\b[^>]*>")
_REFS = re.compile(r'(\bid="|url\(#|href="#)([^")]+)')


def combined_svg(files: list[Path]) -> str:
    """Every slide in one SVG, laid out in a grid with one group per slide (named Slide-01, Slide-02 ... in
    Figma's layer panel), so a single Ctrl+V brings the whole deck into Figma. Each slide's ids are prefixed
    because MuPDF numbers clip paths from 1 on every page."""
    w, h = 960.0, 540.0
    parts = []
    for i, f in enumerate(files):
        svg = f.read_text(encoding="utf-8")
        head = _SVG_OPEN.search(svg)
        if head and (m := re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', head.group(0))):
            w, h = float(m.group(1)), float(m.group(2))
        body = svg[head.end():svg.rfind("</svg>")] if head else svg
        prefix = f"s{i + 1:02d}-"
        body = _REFS.sub(lambda m: m.group(1) + prefix + m.group(2), body)
        x, y = (i % COLUMNS) * (w + GAP), (i // COLUMNS) * (h + GAP)
        # Clip to the slide: shapes bleeding off the edge were hidden by each file's own viewBox.
        parts.append(f'<g id="Slide-{i + 1:02d}" transform="translate({x:g} {y:g})">'
                     f'<defs><clipPath id="{prefix}slide"><rect width="{w:g}" height="{h:g}"/></clipPath></defs>'
                     f'<g clip-path="url(#{prefix}slide)">{body}</g></g>')
    cols = min(COLUMNS, len(files)) or 1
    rows = -(-len(files) // COLUMNS) or 1
    tw, th = cols * w + (cols - 1) * GAP, rows * h + (rows - 1) * GAP
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" '
            f'width="{tw:g}" height="{th:g}" viewBox="0 0 {tw:g} {th:g}">\n' + "\n".join(parts) + "\n</svg>\n")


def render_zip(job_dir: Path, pptx: Path) -> Path:
    """All slide SVGs in one ZIP (cached next to them)."""
    files = render_svgs(job_dir, pptx)
    out = job_dir / ZIP_NAME
    with _lock:
        if out.is_file() and out.stat().st_mtime >= max(f.stat().st_mtime for f in files):
            return out
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.name)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(out)
        return out
