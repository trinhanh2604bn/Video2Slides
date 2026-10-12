"""Speech-to-text with faster-whisper (local CPU inference, no paid API).

The model is loaded lazily, once per process, and guarded by a lock so only one
transcription runs at a time on this worker.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ALLOWED_WHISPER_MODELS, settings

log = logging.getLogger(__name__)

_model = None
_model_name: str | None = None
_load_lock = threading.Lock()
_run_lock = threading.Lock()


class TranscriptionError(Exception):
    """User-safe transcription failure."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    language: str
    duration: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments).strip()

    def to_json(self) -> dict:
        return {
            "segments": [asdict(s) for s in self.segments],
            "language": self.language,
            "duration": self.duration,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Transcript":
        return cls(
            segments=[Segment(float(s["start"]), float(s["end"]), str(s["text"])) for s in data.get("segments", [])],
            language=str(data.get("language", "")),
            duration=float(data.get("duration", 0.0)),
        )

    def to_readable(self) -> str:
        lines = []
        for s in self.segments:
            lines.append(f"[{_fmt_ts(s.start)} - {_fmt_ts(s.end)}] {s.text}")
        return "\n".join(lines) + "\n"

    def save(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        txt = directory / "transcript.txt"
        js = directory / "transcript.json"
        txt.write_text(self.to_readable(), encoding="utf-8")
        js.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        return txt, js


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def get_model(name: str | None = None):
    """Return a cached WhisperModel, downloading weights on first use only."""
    global _model, _model_name
    name = name or settings.whisper_model
    if name not in ALLOWED_WHISPER_MODELS:
        raise TranscriptionError(f"Model Whisper '{name}' không được hỗ trợ (chỉ: tiny, base, small).")
    with _load_lock:
        if _model is not None and _model_name == name:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError("Thư viện faster-whisper chưa được cài đặt.") from exc
        settings.model_cache_dir.mkdir(parents=True, exist_ok=True)
        log.info("Loading Whisper model %s (%s)", name, settings.whisper_compute_type)
        try:
            # Drop the previous model first so two models never sit in RAM together.
            _model = None
            _model = WhisperModel(
                name,
                device="cpu",
                compute_type=settings.whisper_compute_type,
                cpu_threads=settings.whisper_cpu_threads,
                num_workers=1,
                download_root=str(settings.model_cache_dir),
            )
        except MemoryError as exc:
            raise TranscriptionError(
                "Không đủ bộ nhớ để tải model Whisper. Hãy thử WHISPER_MODEL=tiny."
            ) from exc
        except Exception as exc:  # network errors, corrupted cache, etc.
            log.exception("Whisper model load failed")
            raise TranscriptionError(f"Không tải được model Whisper '{name}': {exc}") from exc
        _model_name = name
        return _model


def transcribe(audio_path: Path, language: str | None = None) -> Transcript:
    """Transcribe a 16 kHz mono WAV file. ``language='auto'`` enables detection."""
    lang = language or settings.default_language
    lang_arg = None if lang == "auto" else lang
    model = get_model()
    with _run_lock:
        try:
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=lang_arg,
                beam_size=settings.whisper_beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            segments = [
                Segment(round(s.start, 2), round(s.end, 2), s.text.strip())
                for s in segments_iter
                if s.text and s.text.strip()
            ]
        except MemoryError as exc:
            raise TranscriptionError("Không đủ bộ nhớ khi nhận dạng giọng nói.") from exc
        except Exception as exc:
            log.exception("Transcription failed")
            raise TranscriptionError(f"Nhận dạng giọng nói thất bại: {exc}") from exc

    if not segments:
        raise TranscriptionError("Không phát hiện được lời nói trong video.")
    return Transcript(segments=segments, language=info.language or (lang_arg or ""), duration=info.duration)
