import pytest
from pydantic import ValidationError

from app.ai.schema import LAYOUT_TYPES, Presentation, Slide, gemini_response_schema
from tests.sample_outline import SAMPLE, full_presentation, sample_presentation


def test_sample_is_valid():
    p = sample_presentation()
    assert len(p.slides) == 11
    assert {s.layout_type for s in full_presentation().slides} == set(LAYOUT_TYPES)


def test_process_with_one_item_degrades_to_bullets():
    s = Slide(slide_number=1, title="x", layout_type="process", items=[{"heading": "only", "text": "one"}])
    assert s.layout_type == "bullets"
    assert s.key_points == ["only: one"]


def test_two_column_without_both_sides_degrades():
    s = Slide(slide_number=1, title="x", layout_type="two_column", left_points=["a"], right_points=[])
    assert s.layout_type == "bullets" and s.key_points == ["a"]


def test_empty_slides_removed_and_renumbered():
    data = {
        "presentation_title": "T",
        "slides": [
            {"slide_number": 5, "title": "Title", "layout_type": "title"},
            {"slide_number": 9, "title": "Empty", "layout_type": "bullets"},
            {"slide_number": 7, "title": "Body", "layout_type": "bullets", "key_points": ["a"]},
        ],
    }
    p = Presentation.model_validate(data)
    assert [s.title for s in p.slides] == ["Title", "Body"]
    assert [s.slide_number for s in p.slides] == [1, 2]


def test_rejects_unknown_layout():
    with pytest.raises(ValidationError):
        Slide(slide_number=1, title="x", layout_type="hologram")


def test_rejects_too_few_slides():
    with pytest.raises(ValidationError):
        Presentation.model_validate({**SAMPLE, "slides": SAMPLE["slides"][:1]})


def test_gemini_schema_matches_model():
    schema = gemini_response_schema()
    slide_props = schema["properties"]["slides"]["items"]["properties"]
    assert set(slide_props) <= set(Slide.model_fields)
    assert slide_props["layout_type"]["enum"] == list(LAYOUT_TYPES)
