"""Static checks that keep the single-page frontend wired up after visual changes.

app.js looks every element up by id; a redesign of index.html must never drop one.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML_IDS = set(re.findall(r'\bid="([^"]+)"', HTML))
# Created at runtime by app.js itself, not present in the markup.
RUNTIME_IDS = {"toast"}


def test_html_ids_are_unique():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_every_id_used_by_app_js_exists():
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', JS))
    assert used, "no element lookups found in app.js"
    missing = sorted(used - HTML_IDS - RUNTIME_IDS)
    assert not missing, f"index.html lost elements used by app.js: {missing}"


def test_aria_references_resolve():
    refs = re.findall(r'aria-(?:controls|labelledby|describedby)="([^"]+)"', HTML)
    missing = sorted({r for r in refs if r not in HTML_IDS})
    assert not missing, missing


def test_label_for_targets_exist():
    missing = sorted({f for f in re.findall(r'\bfor="([^"]+)"', HTML) if f not in HTML_IDS})
    assert not missing, missing


@pytest.mark.parametrize("name", ["layoutMode"])
def test_radio_groups_present(name):
    assert f'name="{name}"' in HTML


@pytest.mark.parametrize("control", [
    'id="fileInput" type="file" accept="video/mp4,.mp4"',
    'id="urlInput" type="url"',
    'id="rightsCheck"',
    'id="titleInput" maxlength="150"',
    'id="languageSelect"',
    'id="slideCountSelect"',
    'id="googleCheck"',
    'id="aiImagesCheck"',
    'id="imageDensity"',
    'value="standard"',
    'value="high"',
    'type="submit" id="submitBtn" disabled',
])
def test_form_controls_preserved(control):
    assert control in HTML


def test_stylesheet_and_script_linked():
    assert 'href="styles.css"' in HTML
    assert 'src="app.js"' in HTML


def test_theme_applied_before_stylesheet():
    """The saved theme must be set in <head> before CSS loads, or dark-mode users see a light flash."""
    head = HTML.split("</head>", 1)[0]
    assert head.index('localStorage.getItem("theme")') < head.index('href="styles.css"')
    for option in ("system", "light", "dark"):
        assert f'data-theme-option="{option}"' in HTML
    assert ':root[data-theme="dark"]' in (STATIC / "styles.css").read_text(encoding="utf-8")


def test_reading_notes_sit_below_the_slide_not_beside_it():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    rule = re.search(r"\.reader \{([^}]*)\}", css).group(1)
    assert "grid-template-columns: minmax(0, 1fr);" in rule
    html_reader = HTML[HTML.index('id="readingView"'):HTML.index('id="libraryView"')]
    assert html_reader.index('id="readerStage"') < html_reader.index('id="readerNotes"')


def test_writing_slides_stage_is_shown():
    assert '"writing_slides"' in JS
