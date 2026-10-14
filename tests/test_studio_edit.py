"""Point citations, Studio outputs, slide edits and in-app video playback: all outside the job pipeline."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, slide_edit, studio
from app.ai import assist
from app.ai.ollama import OllamaProvider
from app.ai.ollama_client import OllamaClient
from app.citations import Matcher
from app.pptx_engine import build_pptx, validate_pptx
from app.reading import slide_payload
from app.transcribe import Segment, Transcript
from tests.fake_ollama import FakeOllama
from tests.sample_outline import sample_presentation


def _transcript() -> Transcript:
    lines = ["Hôm nay chúng ta nói về học máy.", "Máy tính học quy luật từ dữ liệu thay vì lập trình từng quy tắc.",
             "Dữ liệu có nhãn dùng cho học có giám sát.", "Ví dụ lọc thư rác trong hộp thư.",
             "Quy trình gồm thu thập dữ liệu, làm sạch, huấn luyện và đánh giá.",
             "Nhận diện chữ viết tay dùng ảnh chữ số làm đầu vào."]
    return Transcript([Segment(i * 40.0, i * 40.0 + 40, t) for i, t in enumerate(lines)], "vi", 240.0)


def _studio_answers(body: dict, idx: int):
    props = body["format"]["properties"]
    if "questions" in props:
        data = {"questions": [{"question": f"Câu hỏi {i}?", "options": ["A", "B", "C", "D"], "answer_index": i % 4,
                               "explanation": "Vì slide nói vậy.", "slides": [2, 99]} for i in range(6)]}
    elif "cards" in props:
        data = {"cards": [{"front": f"Thuật ngữ {i}", "back": "Định nghĩa.", "slides": [4]} for i in range(8)]}
    elif "key_points" in props and "overview" in props:
        data = {"overview": "Tổng quan.", "key_points": [{"text": f"Ý {i}", "slides": [2]} for i in range(5)],
                "conclusion": "Kết luận."}
    elif "items" in props and "question" in props["items"]["items"]["properties"]:
        data = {"items": [{"question": f"Hỏi {i}?", "answer": "Đáp.", "slides": [5]} for i in range(6)]}
    else:
        return json.dumps(FakeOllama().answer_slides(body), ensure_ascii=False), "stop"
    return json.dumps(data, ensure_ascii=False), "stop"


@pytest.fixture
def fake(monkeypatch):
    f = FakeOllama()
    f.overrides["slides"] = _studio_answers
    monkeypatch.setattr(assist, "side_provider", lambda: OllamaProvider(
        client=OllamaClient(base_url="http://ollama.test", transport=f.transport())))
    return f


@pytest.fixture
def done_job():
    """A finished job folder (outline, transcript, built deck) like the pipeline leaves it."""
    job_id, d = jobs.new_job({"style": "professional", "layout_mode": "presentation"}, "bai.mp4")
    pres = sample_presentation()
    for s, t in zip(pres.slides, (None, 40.0, None, 40.0, 80.0, 160.0, None, None, None, 200.0, None)):
        if t is not None:
            s.source_timestamps = [t]
    (d / "outline.json").write_text(pres.model_dump_json(), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps(_transcript().to_json(), ensure_ascii=False), encoding="utf-8")
    build_pptx(pres, d / "presentation.pptx", "professional", [])
    jobs.update(job_id, status="done", finished_at=time.time(),
                result={"slide_count": len(pres.slides), "title": pres.presentation_title})
    yield job_id
    jobs.delete(job_id)


def _drain(pool):
    pool.submit(lambda: None).result(timeout=60)


# ---------------------------------------------------------------------- citations

def test_points_are_cited_at_the_moment_they_are_said():
    m = Matcher(_transcript())
    assert m.locate("Máy tính học quy luật từ dữ liệu", [(40, 80)]) == 40.0
    assert m.locate("Ví dụ: lọc thư rác", []) == 120.0          # whole transcript when the slide has no spans
    assert m.locate("Năng lượng mặt trời giá rẻ", []) is None  # no convincing match -> no timestamp


def test_reading_payload_carries_point_sources():
    pres = sample_presentation()
    pres.slides[3].source_timestamps = [40.0]
    item = slide_payload(pres.slides[3], _transcript())
    assert [p["text"] for p in item["point_sources"]] == item["main_points"]
    assert item["point_sources"][0]["start"] == 40.0


# ---------------------------------------------------------------------- studio

def test_mindmap_and_timeline_need_no_llm(done_job):
    mm = studio.get(done_job, "mindmap")["data"]
    assert mm["text"].startswith("Học máy") and mm["children"]
    section = next(c for c in mm["children"] if c["text"].startswith("Phần 1"))
    assert section["children"] and section["children"][0]["children"]  # section -> slide -> points
    tl = studio.get(done_job, "timeline")["data"]["items"]
    assert [r["start"] for r in tl] == sorted(r["start"] for r in tl) and tl[0]["slide"] == 2


def test_generated_outputs_run_on_the_studio_thread_with_citations(done_job, fake):
    with pytest.raises(studio.StudioError):
        studio.submit(done_job, "mindmap")  # instant kinds are never sent to the LLM
    queued = jobs.pending_count()
    studio.submit(done_job, "quiz")
    _drain(studio._pool)
    assert jobs.pending_count() == queued  # never touches the job queue
    out = studio.get(done_job, "quiz")
    assert out["state"] == "done", out
    q = out["data"]["questions"]
    assert len(q) == 6 and all(x["slides"] == [2] for x in q)  # out-of-range slide 99 dropped
    over = studio.overview(done_job)
    assert over["items"]["quiz"]["state"] == "done" and over["items"]["faq"]["state"] == "none"
    assert over["slides"][1]["start"] == 30.0  # slide 2: window around its timestamp


def test_studio_result_is_marked_stale_after_the_deck_changes(done_job, fake):
    studio.submit(done_job, "flashcards")
    _drain(studio._pool)
    d = jobs.job_dir(done_job)
    pres = sample_presentation()
    pres.slides[1].title = "Mục tiêu mới"
    (d / "outline.json").write_text(pres.model_dump_json(), encoding="utf-8")
    assert studio.get(done_job, "flashcards").get("stale") is True


def test_side_tasks_wait_while_a_deck_is_in_an_llm_stage(done_job, fake, monkeypatch):
    busy = threading.Event()
    busy.set()
    monkeypatch.setattr(jobs, "main_llm_busy", lambda: busy.is_set())
    monkeypatch.setattr(assist, "WAIT_POLL_SEC", 0.05)
    studio.submit(done_job, "faq")
    time.sleep(0.5)
    assert not fake.chats  # no LLM request (not even the health check) while the deck is being written
    assert studio._read(done_job, "faq").get("message") == assist.WAIT_MESSAGE
    busy.clear()
    _drain(studio._pool)
    assert studio.get(done_job, "faq")["state"] == "done"


def test_main_llm_busy_follows_the_job_worker_stage(done_job):
    jobs._current = done_job
    try:
        jobs.set_stage(done_job, "writing_slides")
        assert jobs.main_llm_busy()
        jobs.set_stage(done_job, "generating")
        assert not jobs.main_llm_busy()
    finally:
        jobs._current = None
    assert not jobs.main_llm_busy()


# ---------------------------------------------------------------------- slide edits

def test_slide_edit_rewrites_rebuilds_and_can_be_undone(done_job, fake):
    d = jobs.job_dir(done_job)
    before = (d / "outline.json").read_text(encoding="utf-8")
    jobs.update(done_job, google={"state": "done", "meta": {"presentation_id": "x", "slides": []}})
    slide_edit.submit(done_job, [{"slide_number": 2, "instruction": "Đơn giản hóa"},
                                 {"slide_number": 2, "instruction": "thêm ví dụ lọc thư rác"}])
    _drain(slide_edit._pool)
    job = jobs.load(done_job)
    assert job["edit"]["state"] == "done", job["edit"]
    req = fake.chats[-1]["messages"][-1]["content"]
    assert "Đơn giản hóa; thêm ví dụ lọc thư rác" in req and "slide-02" in req
    after = json.loads((d / "outline.json").read_text(encoding="utf-8"))
    assert after["slides"][1]["key_points"] == ["Ý chính thứ nhất", "Ý chính thứ hai"]  # the fake's rewrite
    assert after["slides"][0] == json.loads(before)["slides"][0]  # other slides untouched
    assert validate_pptx(d / "presentation.pptx", expected_slides=11).ok
    assert job["result"]["edited_at"] and job["google"]["state"] == "stale"  # Google copy never overwritten
    assert slide_edit.public_state(job)["undo_available"]

    slide_edit.submit_undo(done_job)
    _drain(slide_edit._pool)
    assert json.loads((d / "outline.json").read_text(encoding="utf-8")) == json.loads(before)
    assert not slide_edit.public_state(jobs.load(done_job))["undo_available"]


def test_slide_edit_requests_are_checked(done_job):
    with pytest.raises(slide_edit.EditError):
        slide_edit.submit(done_job, [{"slide_number": 40, "instruction": "x"}])
    with pytest.raises(slide_edit.EditError):
        slide_edit.submit(done_job, [{"slide_number": 2, "instruction": "   "}])
    with pytest.raises(slide_edit.EditError):
        slide_edit.submit_undo(done_job)  # nothing to undo yet


def test_edit_api_blocks_conflicting_actions(done_job, monkeypatch):
    monkeypatch.setattr(slide_edit, "running", lambda job: True)
    with TestClient(main.app) as c:
        assert c.delete(f"/api/jobs/{done_job}").status_code == 409
        assert c.post(f"/api/jobs/{done_job}/google", json={}).status_code == 409


# ---------------------------------------------------------------------- playback

def test_video_link_is_signed_and_expires(done_job):
    d = jobs.job_dir(done_job)
    with TestClient(main.app) as c:
        assert c.get(f"/api/jobs/{done_job}/video/link").status_code == 404  # no kept video
        (d / jobs.PLAYBACK_NAME).write_bytes(b"\0" * 1000)
        assert c.get(f"/api/jobs/{done_job}/reading").json()["video_available"] is True
        url = c.get(f"/api/jobs/{done_job}/video/link").json()["url"]
        assert c.get(url).status_code == 200
        assert c.get(url.replace("sig=", "sig=0")).status_code == 403
        assert c.get(f"/api/jobs/{done_job}/video?exp=1&sig=x").status_code == 403


def test_kept_video_expires_with_the_cleanup(done_job, monkeypatch):
    d = jobs.job_dir(done_job)
    (d / jobs.PLAYBACK_NAME).write_bytes(b"x")
    jobs.cleanup_expired()
    assert (d / jobs.PLAYBACK_NAME).exists()
    jobs.update(done_job, finished_at=time.time() - 25 * 3600)
    jobs.cleanup_expired()
    assert not (d / jobs.PLAYBACK_NAME).exists() and (d / "outline.json").exists()
