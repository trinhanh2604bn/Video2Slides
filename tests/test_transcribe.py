import json
import os
from pathlib import Path

import pytest

from app import transcribe
from app.transcribe import Segment, Transcript, TranscriptionError


def test_roundtrip_and_files(tmp_path):
    t = Transcript([Segment(0.0, 5.2, "Xin chào"), Segment(65.0, 70.0, "Tạm biệt")], "vi", 70)
    txt, js = t.save(tmp_path)
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["segments"][0] == {"start": 0.0, "end": 5.2, "text": "Xin chào"}
    assert data["language"] == "vi"
    assert "[01:05 - 01:10] Tạm biệt" in txt.read_text(encoding="utf-8")
    assert Transcript.from_json(data).text == "Xin chào Tạm biệt"


def test_rejects_unknown_model():
    with pytest.raises(TranscriptionError, match="không được hỗ trợ"):
        transcribe.get_model("large-v3")


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("V2S_REAL_VIDEO"), reason="set V2S_REAL_VIDEO=path/to/vietnamese.mp4")
def test_real_vietnamese_transcription(tmp_path):
    from app.video import extract_audio

    wav = extract_audio(Path(os.environ["V2S_REAL_VIDEO"]), tmp_path / "a.wav")
    t = transcribe.transcribe(wav, "vi")
    assert t.language == "vi"
    assert len(t.segments) > 3
    assert all(s.end >= s.start for s in t.segments)
