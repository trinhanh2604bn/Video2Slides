"""Grounding guard: no invented boilerplate slides or contact details (outline repair, slide repair, final net).

MOCKED models (tests/fake_ollama.py); the patterns come from a real qwen3:4b run on an 89-minute test video that
ended with invented "Acknowledgements" and "Contact Information" slides.
"""

import copy
import json

from app.ai import grounding
from app.ai.base import OutlineRequest
from app.ai.ollama import OllamaProvider
from app.ai.ollama_client import OllamaClient
from app.ai.schema import Presentation
from app.transcribe import Segment, Transcript
from tests.fake_ollama import FakeOllama
from tests.sample_outline import SAMPLE

SRC = "In 1914-1918 the railway network expanded. The lecture explains signalling and timetables."


def test_filler_titles_are_detected_only_when_the_source_lacks_them():
    assert grounding.filler_category("Contact Information", SRC) == "contact"
    assert grounding.filler_category("Acknowledgements", SRC) == "acknowledgements"
    assert grounding.filler_category("Q&A", SRC) == "questions"
    assert grounding.filler_category("Tài liệu tham khảo", "bài giảng về đường sắt") == "references"
    assert grounding.filler_category("Railway Signalling", SRC) is None
    assert grounding.filler_category("Thank you", SRC + " Thank you for listening.") is None  # actually said


def test_contact_details_must_come_from_the_source():
    assert grounding.invented_contacts("Years 1914-1918 and 2,500 km of track", SRC) == []
    found = grounding.invented_contacts("Write to info@rail.org or call +84 28 1234 5678 / 0912 345 678", SRC)
    assert "info@rail.org" in found and "+84 28 1234 5678" in found and "0912 345 678" in found
    assert grounding.invented_contacts("see www.rail.org", SRC + " visit www.rail.org") == []


def test_final_net_drops_boilerplate_and_invented_contacts_then_renumbers():
    data = copy.deepcopy(SAMPLE)
    n = len(data["slides"])
    data["slides"].append({"slide_number": n + 1, "title": "Contact Information", "layout_type": "summary",
                           "key_points": ["Email: lecturer@uni.edu"]})
    data["slides"][1]["key_points"] = list(data["slides"][1].get("key_points") or ["Point"]) + ["Visit www.fake.com"]
    pres = Presentation.model_validate(data)
    notices = grounding.sanitize(pres, " ".join(json.dumps(s) for s in SAMPLE["slides"]))
    assert len(pres.slides) == n and [s.slide_number for s in pres.slides] == list(range(1, n + 1))
    assert all("fake.com" not in p for s in pres.slides for p in s.key_points)
    assert any("Contact Information" in x for x in notices) and any("đường dẫn" in x for x in notices)


def _transcript():
    segs = [Segment(i * 5.0, i * 5.0 + 4.5, f"Câu số {i} về quản lý thời gian cho sinh viên.") for i in range(12)]
    return Transcript(segs, "vi", 60)


def _provider(fake):
    return OllamaProvider(client=OllamaClient(base_url="http://ollama.test", transport=fake.transport()))


def test_outline_with_invented_closing_slide_is_repaired():
    fake = FakeOllama()

    def outline(body, idx):
        data = fake.answer_outline(body)
        if idx == 0:
            data["slides"][-1].update(title="Contact Information", layout_type="summary", source_ranges=[])
        return json.dumps(data), "stop"

    fake.overrides["outline"] = outline
    p = _provider(fake)
    pres = p.generate(OutlineRequest(transcript=_transcript(), language="vi", slide_count=6))
    assert all(s.title != "Contact Information" for s in pres.slides)
    assert p.stats.repairs >= 1
    repair_prompt = [c for c in fake.chats if "presentation_title" in json.dumps(c["format"])][1]["messages"][-1]
    assert "boilerplate" in repair_prompt["content"]


def test_persistent_boilerplate_is_dropped_on_the_last_attempt():
    fake = FakeOllama()

    def outline(body, idx):
        data = fake.answer_outline(body)
        data["slides"].append({"title": "Acknowledgements", "layout_type": "summary", "purpose": "x",
                               "source_ranges": []})
        return json.dumps(data), "stop"

    fake.overrides["outline"] = outline
    p = _provider(fake)
    pres = p.generate(OutlineRequest(transcript=_transcript(), language="vi", slide_count=6))
    assert all(s.title != "Acknowledgements" for s in pres.slides)
    assert any("không có trong nguồn" in w for w in p.stats.warnings)


def test_invented_contact_in_slide_text_is_repaired():
    fake = FakeOllama()

    def slides(body, idx):
        data = fake.answer_slides(body)
        if idx == 0:
            data["slides"][0]["key_points"].append("Liên hệ: hotro@truong.edu.vn")
        return json.dumps(data), "stop"

    fake.overrides["slides"] = slides
    p = _provider(fake)
    pres = p.generate(OutlineRequest(transcript=_transcript(), language="vi", slide_count=6))
    assert not any("hotro@" in k for s in pres.slides for k in s.key_points)
    assert p.stats.repairs >= 1
