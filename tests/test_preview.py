"""Slide previews rendered by the local presentation engine (PowerPoint COM / LibreOffice)."""

import os

import pytest
from PIL import Image

from app import preview
from app.pptx_engine import build_pptx
from tests.sample_outline import sample_presentation

pytestmark = pytest.mark.skipif(os.environ.get("PREVIEW_ENGINE") == "off" or not preview.engine(),
                                reason="no PowerPoint/LibreOffice on this machine")


def test_render_every_slide_and_reuse_cache(tmp_path):
    pptx = tmp_path / "presentation.pptx"
    pres = sample_presentation()
    build_pptx(pres, pptx, "professional", [])
    files = preview.render_previews(tmp_path, pptx)
    assert len(files) == len(pres.slides)
    with Image.open(files[0]) as im:
        assert im.width == preview.PREVIEW_WIDTH and abs(im.width / im.height - 16 / 9) < 0.01
    mtimes = [f.stat().st_mtime for f in files]
    assert [f.stat().st_mtime for f in preview.render_previews(tmp_path, pptx)] == mtimes  # cached


def test_rebuilt_deck_invalidates_previews(tmp_path):
    pptx = tmp_path / "presentation.pptx"
    build_pptx(sample_presentation(), pptx, "professional", [])
    preview.render_previews(tmp_path, pptx)
    os.utime(pptx, (pptx.stat().st_atime, pptx.stat().st_mtime + 60))
    assert preview.cached_previews(tmp_path, pptx) == []
