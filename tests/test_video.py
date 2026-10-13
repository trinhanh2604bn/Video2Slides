import dataclasses
import subprocess

import pytest

from app import video
from app.video import MediaError
from tests.conftest import make_video, needs_ffmpeg

pytestmark = needs_ffmpeg


def test_probe_reads_streams(sample_mp4):
    info = video.probe(sample_mp4)
    assert info.width == 640 and info.height == 360
    assert info.video_codec == "h264"
    assert info.has_audio
    assert 5 < info.duration < 7


def test_validate_accepts_mp4(sample_mp4):
    assert video.validate(sample_mp4).duration > 0


def test_validate_rejects_non_mp4(tmp_path):
    mkv = make_video(tmp_path / "x.mkv", seconds=2, fmt="matroska")
    with pytest.raises(MediaError, match="MP4"):
        video.validate(mkv)


def test_validate_rejects_missing_audio(tmp_path):
    silent = make_video(tmp_path / "silent.mp4", seconds=2, audio=False)
    with pytest.raises(MediaError, match="âm thanh"):
        video.validate(silent)


def test_validate_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    with pytest.raises(MediaError):
        video.validate(bad)


def test_validate_rejects_empty(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaError, match="rỗng"):
        video.validate(empty)


def test_validate_enforces_duration_limit(sample_mp4, monkeypatch):
    monkeypatch.setattr(video, "settings", dataclasses.replace(video.settings, max_duration_sec=3))
    with pytest.raises(MediaError, match="vượt giới hạn"):
        video.validate(sample_mp4)


def test_validate_enforces_size_limit(sample_mp4, monkeypatch):
    monkeypatch.setattr(video, "settings", dataclasses.replace(video.settings, max_upload_mb=0))
    with pytest.raises(MediaError, match="MB"):
        video.validate(sample_mp4)


def test_extract_audio_is_mono_16k(sample_mp4, tmp_path):
    wav = video.extract_audio(sample_mp4, tmp_path / "a.wav")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(wav)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "16000,1"


def test_extract_frames_respects_limit_and_cleans_up(sample_mp4, tmp_path):
    frames = video.extract_frames(sample_mp4, tmp_path / "f", 6.0, max_frames=3, candidates=6)
    assert 1 <= len(frames) <= 3
    names = sorted(p.name for p in (tmp_path / "f").iterdir())
    assert names == [f"frame_{i:02d}.jpg" for i in range(len(frames))]  # candidates removed
    ts = [f.timestamp for f in frames]
    assert ts == sorted(ts)


def test_duplicate_detection():
    a = video.Frame(path=None, timestamp=1, hash_bits=0b1010, mean_rgb=(100, 100, 100))
    b = video.Frame(path=None, timestamp=2, hash_bits=0b1011, mean_rgb=(105, 100, 100))
    c = video.Frame(path=None, timestamp=3, hash_bits=0b1011, mean_rgb=(200, 50, 50))
    assert video._is_duplicate(a, b)
    assert not video._is_duplicate(a, c)  # same layout, different colour


def test_no_shell_used(monkeypatch, sample_mp4):
    calls = []
    real = subprocess.run

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(video.subprocess, "run", spy)
    video.probe(sample_mp4)
    assert calls and not any(k.get("shell") for k in calls)


def test_mux_separate_streams_without_ffmpeg_binary(sample_mp4, tmp_path):
    """URL import without FFmpeg: yt-dlp's video-only + audio-only downloads are joined with PyAV."""
    from app import media_local
    from app.url_import.retrieval import mux_streams

    video_only = tmp_path / "video.mp4"
    audio_only = tmp_path / "audio.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(sample_mp4), "-an", "-c", "copy",
                    str(video_only)], check=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(sample_mp4), "-vn", "-c", "copy",
                    str(audio_only)], check=True)
    out = tmp_path / "media.mp4"
    mux_streams(video_only, audio_only, out)
    info = media_local.validate(out)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert 5 < info.duration < 7
