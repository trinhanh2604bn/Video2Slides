"""Local LLM provider: Ollama (default model qwen3:4b) with staged, validated generation.

Why staged: a 4B model with an 8k context window cannot reliably write a whole deck with detailed
notes in a single answer. The work is split (see ``ollama_prompts``) and every answer is checked:

* JSON-schema structured output (Ollama ``format``) + Pydantic validation (``app.ai.schema``);
* application checks: slide ids/order, requested slide count, non-empty content and notes,
  timestamps inside the video, truncated answers (``done_reason == "length"``);
* bounded repairs: the first attempt plus at most ``OLLAMA_MAX_REPAIRS`` (<= 2) targeted retries that
  re-request only the slides that failed; a truncated multi-slide answer is split in halves;
* validated answers are cached on disk (key = model + prompt version + schema + settings + input),
  so a retry after a failure or a restart does not redo finished requests.

The transcript is never silently cut: when it does not fit the context budget it is split at pauses
into parts that are condensed (with their timestamps) before the outline is planned.
Only one LLM request runs at a time in this process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..config import settings
from ..transcribe import Segment, Transcript
from . import grounding
from . import ollama_prompts as P
from .base import MAX_SLIDES, AIError, OutlineProvider, OutlineRequest, recommend_slide_count
from .ollama_client import ChatResult, ModelInfo, OllamaClient
from .schema import Presentation, Slide

log = logging.getLogger(__name__)

# One active generation request per process (16 GB machines cannot afford parallel inference).
_LLM_LOCK = threading.Lock()

# Conservative token estimate: over-estimating only makes batches smaller, never truncates input.
CHARS_PER_TOKEN = 3.0  # measured 3.4-3.5 chars/token on Vietnamese (qwen3:4b); kept lower for safety
MARGIN_TOKENS = 384
SLIDE_OUT_TOKENS = 1000      # one slide: points + speaker notes + 80-160 word explanation (Vietnamese)
OUTLINE_SLIDE_TOKENS = 110   # one outline entry
OUTLINE_BASE_TOKENS = 200
CHUNK_OUT_TOKENS = 1400
MIN_CHUNK_TOKENS = 800
RANGE_PAD_SEC = 4.0
ILLUSTRATION_LAYOUTS = ("title", "concept", "image_left", "image_right", "bullets")


def est_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 8


def _clock(sec: float) -> str:
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def transcript_lines(segments: list[Segment]) -> str:
    return "\n".join(f"[{s.start:.0f}] {s.text.strip()}" for s in segments if s.text.strip())


def split_segments(segments: list[Segment], max_tokens: int) -> list[list[Segment]]:
    """Split at coherent boundaries: each part stays under ``max_tokens`` and ends, when possible, at the
    longest pause in its last third (a pause usually separates ideas). Nothing is dropped."""
    parts: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_tokens = 0
    for seg in segments:
        t = est_tokens(f"[{seg.start:.0f}] {seg.text}\n")
        if cur and cur_tokens + t > max_tokens:
            cut = len(cur)
            third = max(1, len(cur) * 2 // 3)
            if len(cur) - third >= 1:
                gaps = [(cur[i].start - cur[i - 1].end, i) for i in range(third, len(cur))]
                gap, idx = max(gaps)
                if gap >= 0.6:
                    cut = idx
            parts.append(cur[:cut])
            cur = cur[cut:]
            cur_tokens = sum(est_tokens(f"[{s.start:.0f}] {s.text}\n") for s in cur)
        cur.append(seg)
        cur_tokens += t
    if cur:
        parts.append(cur)
    return parts


@dataclass
class OutlineEntry:
    slide_id: str
    number: int
    title: str
    layout_type: str
    purpose: str
    ranges: list[dict]
    illustration: bool = False


@dataclass
class GenerationStats:
    requests: int = 0
    cache_hits: int = 0
    repairs: int = 0
    splits: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    llm_seconds: float = 0.0
    cold_start_sec: float | None = None
    chunks: int = 0
    condensed_sources: int = 0
    warnings: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def add(self, step: str, res: ChatResult, wall: float) -> None:
        self.requests += 1
        self.prompt_tokens += res.prompt_tokens
        self.output_tokens += res.output_tokens
        self.llm_seconds += wall
        if self.cold_start_sec is None:
            self.cold_start_sec = round(res.load_sec, 2)
        self.calls.append({"step": step, "wall_sec": round(wall, 2), **res.stats()})

    def to_json(self) -> dict:
        tps = [c["tokens_per_sec"] for c in self.calls if c.get("tokens_per_sec")]
        return {
            "requests": self.requests, "cache_hits": self.cache_hits, "repairs": self.repairs,
            "splits": self.splits, "prompt_tokens": self.prompt_tokens, "output_tokens": self.output_tokens,
            "llm_seconds": round(self.llm_seconds, 1), "cold_start_load_sec": self.cold_start_sec,
            "avg_tokens_per_sec": round(sum(tps) / len(tps), 2) if tps else None,
            "transcript_parts": self.chunks, "condensed_sources": self.condensed_sources,
            "warnings": self.warnings[:10], "calls": self.calls[-60:],
        }


class _Rejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class OllamaProvider(OutlineProvider):
    name = "ollama"

    def __init__(self, client: OllamaClient | None = None, model: str | None = None):
        self.client = client or OllamaClient()
        self.model = model or settings.ollama_model
        self.used_model = self.model
        self.num_ctx = settings.ollama_num_ctx
        self.stats = GenerationStats()
        self.info: ModelInfo | None = None
        self.think: bool | None = None
        self.server_version = ""

    # ------------------------------------------------------------------ public

    def describe(self) -> dict:
        """Settings and measurements reported with the job (no secrets)."""
        return {
            "provider": self.name, "model": self.model, "ollama_version": self.server_version,
            "num_ctx": self.num_ctx, "temperature": settings.ollama_temperature,
            "keep_alive": settings.ollama_keep_alive, "think": self.think,
            "capabilities": self.info.capabilities if self.info else [],
            "parameter_size": self.info.parameter_size if self.info else "",
            "quantization": self.info.quantization if self.info else "",
            "prompt_version": P.PROMPT_VERSION, **self.stats.to_json(),
        }

    def generate(self, req: OutlineRequest) -> Presentation:
        self._prepare()
        t = req.transcript
        if not t.segments:
            raise AIError("Bản ghi lời nói trống, không có nội dung để tạo slide.", "empty_transcript")
        lang = req.language if req.language not in ("", "auto") else (t.language or "vi")
        if lang not in P.LANG_NAMES:
            lang = "en" if lang != "vi" else "vi"
        duration = t.duration or max(s.end for s in t.segments)
        self._req = req
        self._duration = duration
        self._lang = lang
        self._cache_dir = (req.cache_dir / "llm_cache") if req.cache_dir else None

        source, source_kind, notes = self._outline_source(t, lang)
        entries, deck_title, subtitle = self._outline(req, source, source_kind, lang, duration)
        slides = self._write_all(req, entries, deck_title, lang, t, notes)

        if slides and slides[0].layout_type == "title" and not slides[0].subtitle and subtitle:
            slides[0].subtitle = subtitle[:200]  # the deck subtitle written in the outline step
        pres = Presentation(presentation_title=(req.title_hint or deck_title)[:150], subtitle=subtitle[:200],
                            language=lang, slides=slides)
        problems = check_presentation(pres, duration, [e.slide_id for e in entries])
        if problems:
            raise AIError("Bài trình chiếu do model tạo chưa đạt kiểm tra: " + "; ".join(problems[:3]),
                          "invalid_output")
        try:
            loaded = self.client.loaded_models()
            self.stats.calls.append({"step": "loaded_models", "models": loaded})
        except AIError:
            pass
        return pres

    # ------------------------------------------------------------------ setup

    def _prepare(self) -> None:
        self.server_version = self.client.version()
        installed = self.client.installed_models()
        if not any(_same_model(m, self.model) for m in installed):
            raise AIError(
                f"Ollama chưa có model '{self.model}'. Mở cửa sổ lệnh và chạy: ollama pull {self.model} "
                "(khoảng 2,5 GB với qwen3:4b), hoặc chạy lại install.bat, rồi bấm 'Thử lại'.", "model_missing")
        self.info = self.client.show(self.model)
        mode = settings.ollama_think
        if not self.info.supports_thinking:
            self.think = None  # the field is rejected by models without the capability
        else:
            self.think = mode == "on"
        if self.info.context_length and self.num_ctx > self.info.context_length:
            self.num_ctx = self.info.context_length

    # ------------------------------------------------------------------ LLM call + cache

    def _call(self, step: str, messages: list[dict], schema: dict, num_predict: int) -> tuple[Any, ChatResult | None]:
        """One structured request (cache first). Returns (parsed JSON or None, result or None if cached)."""
        key = self._cache_key(messages, schema, num_predict)
        cached = self._cache_get(key)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached, None
        if self._req.check_cancel:
            self._req.check_cancel()
        prompt_est = sum(est_tokens(m["content"]) for m in messages) + est_tokens(json.dumps(schema)) // 3
        if prompt_est + num_predict > self.num_ctx:
            raise AIError(f"Yêu cầu tới model vượt cửa sổ ngữ cảnh ({prompt_est + num_predict} > {self.num_ctx} token).",
                          "context_overflow")
        started = time.monotonic()
        with _LLM_LOCK:
            res = self.client.chat(self.model, messages, format=schema, num_ctx=self.num_ctx,
                                   num_predict=num_predict, think=self.think, check_cancel=self._req.check_cancel)
        self.stats.add(step, res, time.monotonic() - started)
        if res.prompt_tokens and res.prompt_tokens + res.output_tokens >= self.num_ctx:
            raise AIError("Nội dung vượt cửa sổ ngữ cảnh của model (OLLAMA_NUM_CTX); câu trả lời bị cắt.",
                          "context_overflow")
        try:
            data = json.loads(res.content)
        except ValueError:
            data = None
        return data, res

    def _cache_key(self, messages: list[dict], schema: dict, num_predict: int) -> str:
        # provider + server version: local Ollama and Ollama on Modal never share cached answers
        blob = json.dumps({"v": P.PROMPT_VERSION, "provider": self.name, "server": self.server_version,
                           "model": self.model, "messages": messages, "schema": schema,
                           "num_ctx": self.num_ctx, "temperature": settings.ollama_temperature,
                           "num_predict": num_predict, "think": self.think}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]

    def _cache_get(self, key: str) -> Any:
        if not self._cache_dir:
            return None
        f = self._cache_dir / f"{key}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))["data"] if f.is_file() else None
        except (ValueError, KeyError, OSError):
            return None

    def _cache_put(self, key: str, data: Any) -> None:
        if not self._cache_dir:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_dir / f"{key}.tmp"
        tmp.write_text(json.dumps({"data": data, "provider": self.name, "model": self.model,
                                  "prompt_version": P.PROMPT_VERSION, "created_at": time.time()},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._cache_dir / f"{key}.json")

    def _structured(self, step: str, build, schema: dict, num_predict: int, validate) -> Any:
        """Initial attempt + bounded repairs for a single-object answer (chunk, outline)."""
        problems: list[str] = []
        truncated = False
        for attempt in range(settings.ollama_max_repairs + 1):
            messages = build(P.repair_note(problems, truncated) if attempt else "")
            key = self._cache_key(messages, schema, num_predict)
            data, res = self._call(step, messages, schema, num_predict)
            if attempt:
                self.stats.repairs += res is not None
            truncated = bool(res and res.truncated)
            try:
                if data is None:
                    raise _Rejected(["the answer was not valid JSON" + (" (it was cut off)" if truncated else "")])
                value = validate(data, final=attempt == settings.ollama_max_repairs)
            except _Rejected as exc:
                problems = exc.problems
                log.warning("Ollama %s attempt %d rejected: %s", step, attempt + 1, "; ".join(problems))
                continue
            if res is not None:
                self._cache_put(key, data)
            return value
        code = "truncated_output" if truncated else "invalid_output"
        raise AIError(f"Model {self.model} chưa tạo được {_STEP_VI.get(step, step)} hợp lệ sau "
                      f"{settings.ollama_max_repairs + 1} lần thử: {'; '.join(problems[:3])}. Bấm 'Thử lại' "
                      "(các phần đã xong được giữ lại).", code)

    # ------------------------------------------------------------------ 1. transcript -> outline source

    def _outline_budget(self, n_max: int) -> tuple[int, int]:
        out = OUTLINE_BASE_TOKENS + OUTLINE_SLIDE_TOKENS * n_max
        system = est_tokens(P.OUTLINE_SYSTEM) + 250
        return self.num_ctx - MARGIN_TOKENS - out - system, out

    def _outline_source(self, t: Transcript, lang: str) -> tuple[str, str, str]:
        """Full timestamped transcript when it fits; otherwise condensed, timestamped notes."""
        lines = transcript_lines(t.segments)
        _, n_max = self._count_range(self._req, t)
        budget, _ = self._outline_budget(n_max)
        if est_tokens(lines) <= budget:
            return lines, "timestamped transcript lines, [seconds] text", ""
        notes = self._condense(t.segments, lang, budget)
        return notes, "timestamped notes condensed from the full transcript, [start-end seconds] fact", notes

    def _condense(self, segments: list[Segment], lang: str, budget: int, report: bool = True) -> str:
        chunk_budget = self.num_ctx - MARGIN_TOKENS - CHUNK_OUT_TOKENS - est_tokens(P.CHUNK_SYSTEM) - 120
        chunk_budget = max(MIN_CHUNK_TOKENS, min(chunk_budget, 3200))
        parts = split_segments(segments, chunk_budget)
        if report:
            self.stats.chunks = len(parts)
        notes: list[str] = []
        for i, part in enumerate(parts, start=1):
            if report and self._req.progress:
                self._req.progress("analyzing", i - 1, len(parts) + 1)
            notes.append(self._summarise(transcript_lines(part), lang, i, len(parts), part[0].start, part[-1].end))
        text = "\n\n".join(notes)
        # Very long videos: condense the notes again (pairs of parts) until they fit. Bounded by log2(parts).
        while est_tokens(text) > budget and len(notes) > 1:
            grouped = ["\n\n".join(notes[i:i + 2]) for i in range(0, len(notes), 2)]
            notes = [self._summarise(g, lang, i + 1, len(grouped), 0, self._duration) for i, g in enumerate(grouped)]
            text = "\n\n".join(notes)
        if est_tokens(text) > budget:
            raise AIError("Video quá dài so với cửa sổ ngữ cảnh của model; hãy tăng OLLAMA_NUM_CTX hoặc dùng video ngắn hơn.",
                          "context_overflow")
        if report and self._req.progress:
            self._req.progress("analyzing", len(parts), len(parts) + 1)
        return text

    def _summarise(self, lines: str, lang: str, part: int, parts: int, start: float, end: float) -> str:
        duration = self._duration

        def validate(data: dict, final: bool) -> str:
            facts = data.get("facts") or []
            problems = []
            if not str(data.get("summary") or "").strip():
                problems.append("summary is empty")
            good = [f for f in facts if str(f.get("text") or "").strip()]
            if not good:
                problems.append("facts is empty")
            bad_t = [f for f in good if not (0 <= float(f.get("start", -1)) <= duration + 2)]
            if bad_t and not final:
                problems.append(f"fact times must be seconds between 0 and {duration:.0f}")
            if problems:
                raise _Rejected(problems)
            out = [f"Part {part}/{parts} ({start:.0f}-{end:.0f}s): {data['summary'].strip()}"]
            for f in good:
                s, e = _clamp(float(f.get("start", 0)), duration), _clamp(float(f.get("end", 0)), duration)
                out.append(f"- [{s:.0f}-{max(s, e):.0f}] {f['text'].strip()}")
            terms = [x.strip() for x in data.get("terms") or [] if str(x).strip()]
            if terms:
                out.append("Terms: " + ", ".join(terms[:10]))
            return "\n".join(out)

        return self._structured(
            "chunk", lambda note: _append(P.chunk_messages(lines, lang, part, parts, start, end), note),
            P.CHUNK_SCHEMA, CHUNK_OUT_TOKENS, validate)

    # ------------------------------------------------------------------ 2. outline

    @staticmethod
    def _count_range(req: OutlineRequest, t: Transcript) -> tuple[int, int]:
        if req.slide_count:
            n = max(2, min(MAX_SLIDES, int(req.slide_count)))
            return n, n
        rec = recommend_slide_count(t.duration, len(t.text.split()))
        return max(4, rec - 2), min(MAX_SLIDES, rec + 2)

    def _outline(self, req: OutlineRequest, source: str, source_kind: str, lang: str,
                 duration: float) -> tuple[list[OutlineEntry], str, str]:
        n_min, n_max = self._count_range(req, req.transcript)
        if n_min == n_max:
            count_rule = f"Plan exactly {n_min} slides (including the title slide and a closing slide)."
        else:
            count_rule = (f"Choose the number of slides that fits the content: between {n_min} and {n_max} "
                          "(including the title slide and a closing slide).")
        _, out_tokens = self._outline_budget(n_max)
        schema = P.outline_schema(n_min, n_max)

        def validate(data: dict, final: bool):
            slides = [s for s in data.get("slides") or [] if isinstance(s, dict)]
            problems = []
            if not str(data.get("presentation_title") or "").strip() and not req.title_hint:
                problems.append("presentation_title is empty")
            if not (n_min <= len(slides) <= n_max):
                msg = (f"the deck must have exactly {n_min} slides" if n_min == n_max
                       else f"the deck must have {n_min}-{n_max} slides") + f", not {len(slides)}"
                if not final or len(slides) < 2:
                    problems.append(msg)
                else:
                    self.stats.warnings.append(f"Số slide {len(slides)} khác yêu cầu ({n_min}-{n_max}).")
            if any(not str(s.get("title") or "").strip() for s in slides):
                problems.append("every slide needs a title")
            if len(slides) >= 4 and slides[-1].get("layout_type") not in ("summary", "conclusion"):
                if not final:
                    problems.append("the last slide must close the deck: layout summary or conclusion")
                else:
                    self.stats.warnings.append("Dàn ý không kết thúc bằng slide tổng kết/kết luận.")
            out_of_range = [i + 1 for i, s in enumerate(slides) for r in s.get("source_ranges") or []
                            if not _valid_range(r, duration)]
            if out_of_range and not final:
                problems.append(f"slides {sorted(set(out_of_range))[:5]}: source_ranges must be seconds "
                                f"between 0 and {duration:.0f} (start <= end)")
            # Grounding: no boilerplate slides the source does not contain (Contact, Acknowledgements, Q&A, ...),
            # every content slide cites transcript times. Last attempt: unsupported boilerplate is dropped.
            g_problems, g_drop = grounding.outline_problems(slides, req.transcript.text)
            if g_problems and not final:
                problems.extend(g_problems)
            elif g_drop and len(slides) - len(g_drop) >= 2:
                slides = [s for i, s in enumerate(slides) if i not in set(g_drop)]
                self.stats.warnings.append(f"Bỏ {len(g_drop)} slide không có trong nguồn khỏi dàn ý.")
            if problems:
                raise _Rejected(problems)
            if len(slides) > n_max:
                slides = slides[:n_max - 1] + [slides[-1]]
            return data, slides

        data, slides = self._structured(
            "outline", lambda note: _append(P.outline_messages(source, lang, count_rule, duration, req.title_hint,
                                                               req.style, source_kind), note),
            schema, out_tokens, validate)
        if self._req.progress:
            total = self.stats.chunks + 1 if self.stats.chunks else 1
            self._req.progress("analyzing", total, total)

        entries: list[OutlineEntry] = []
        for i, s in enumerate(slides, start=1):
            layout = s.get("layout_type") if s.get("layout_type") in P.OUTLINE_LAYOUTS else "bullets"
            if i == 1:
                layout = "title"
            elif layout == "title":
                layout = "section"
            ranges = [_norm_range(r, duration) for r in s.get("source_ranges") or [] if _valid_range(r, duration)]
            dropped = len(s.get("source_ranges") or []) - len(ranges)
            if dropped:
                self.stats.warnings.append(f"Bỏ {dropped} mốc thời gian ngoài thời lượng video ở slide {i}.")
            entries.append(OutlineEntry(slide_id=f"slide-{i:02d}", number=i, title=str(s["title"]).strip()[:120],
                                        layout_type=layout, purpose=str(s.get("purpose") or "").strip(),
                                        ranges=ranges))
        self._mark_illustrations(entries, req.max_illustrations)
        return entries, str(data.get("presentation_title") or "").strip() or entries[0].title, \
            str(data.get("subtitle") or "").strip()

    @staticmethod
    def _mark_illustrations(entries: list[OutlineEntry], cap: int) -> None:
        """Pick at most ``cap`` slides for an AI illustration: the title slide, then spread over the deck."""
        if cap <= 0:
            return
        eligible = [e for e in entries if e.layout_type in ILLUSTRATION_LAYOUTS]
        chosen = [e for e in eligible if e.layout_type == "title"][:1]
        rest = [e for e in eligible if e not in chosen]
        rest.sort(key=lambda e: (0 if e.layout_type in ("concept", "image_left", "image_right") else 1, e.number))
        for e in rest[:max(0, cap - len(chosen))]:
            chosen.append(e)
        for e in chosen:
            e.illustration = True

    # ------------------------------------------------------------------ 3. slides

    def _slides_budget(self, n: int, fixed: str) -> int:
        """Tokens left for source text when writing ``n`` slides with the given fixed prompt text."""
        system = est_tokens(P.SLIDES_SYSTEM) + est_tokens(P.IMAGE_RULE) + 120
        return self.num_ctx - MARGIN_TOKENS - n * SLIDE_OUT_TOKENS - system - est_tokens(fixed)

    def _write_all(self, req: OutlineRequest, entries: list[OutlineEntry], deck_title: str, lang: str,
                   t: Transcript, notes: str) -> list[Slide]:
        deck_outline = "\n".join(
            f"{e.slide_id} [{e.layout_type}] {e.title} - {e.purpose}"
            + (f" ({', '.join(_clock(r['start']) + '-' + _clock(r['end']) for r in e.ranges)})" if e.ranges else "")
            for e in entries)
        self._ctx = {"deck_outline": deck_outline, "deck_title": deck_title, "lang": lang, "t": t, "notes": notes,
                     "with_images": req.max_illustrations > 0, "style": req.style}
        written: dict[str, Slide] = {}
        total = len(entries)
        batch_size = settings.ollama_slides_per_batch
        i = 0
        while i < total:
            n = min(batch_size, total - i)
            # Shrink the batch until the relevant source fits next to the output reserve.
            while n > 1 and est_tokens(self._source(entries[i:i + n], n, raw_only=True)) > \
                    self._slides_budget(n, deck_outline):
                n -= 1
            batch = entries[i:i + n]
            if req.progress:
                req.progress("writing_slides", len(written), total)
            written.update(self._write_batch(batch, attempt=0, problems={}))
            i += n
        if req.progress:
            req.progress("writing_slides", total, total)
        return [written[e.slide_id] for e in entries]

    def _source(self, batch: list[OutlineEntry], n: int, raw_only: bool = False) -> str:
        """Transcript lines behind the batch (padded); overview slides get the whole video or its notes."""
        t: Transcript = self._ctx["t"]
        ranges = [r for e in batch for r in e.ranges]
        if ranges:
            segs = [s for s in t.segments
                    if any(s.end >= r["start"] - RANGE_PAD_SEC and s.start <= r["end"] + RANGE_PAD_SEC for r in ranges)]
            if segs:
                return transcript_lines(segs)
        full = transcript_lines(t.segments)
        if raw_only or est_tokens(full) <= self._slides_budget(n, self._ctx["deck_outline"]):
            return full
        return self._ctx["notes"] or full

    def _fit_source(self, batch: list[OutlineEntry]) -> str:
        """Source that fits the budget for this batch; condensed (never cut) when the raw lines are too long."""
        budget = self._slides_budget(len(batch), self._ctx["deck_outline"])
        text = self._source(batch, len(batch))
        if est_tokens(text) <= budget:
            return text
        self.stats.condensed_sources += 1
        t: Transcript = self._ctx["t"]
        ranges = [r for e in batch for r in e.ranges]
        segs = [s for s in t.segments
                if not ranges or any(s.end >= r["start"] - RANGE_PAD_SEC and s.start <= r["end"] + RANGE_PAD_SEC
                                     for r in ranges)]
        return self._condense(segs, self._ctx["lang"], budget, report=False)

    def _write_batch(self, batch: list[OutlineEntry], attempt: int, problems: dict[str, list[str]]) -> dict[str, Slide]:
        ids = [e.slide_id for e in batch]
        with_images = self._ctx["with_images"]
        requests = "\n".join(
            f"- slide_id: {e.slide_id} | planned title: {e.title} | layout: {e.layout_type} | purpose: {e.purpose}"
            + (" | source: " + ", ".join(f"{r['start']:.0f}-{r['end']:.0f}s" for r in e.ranges) if e.ranges else "")
            + (f" | illustration: {'yes' if e.illustration else 'no'}" if with_images else "")
            for e in batch)
        if problems:
            layouts = {e.slide_id: e.layout_type for e in batch}
            requests += P.repair_note([f"{sid} (layout {layouts.get(sid)}): {p}" for sid, ps in problems.items() for p in ps],
                                      truncated=any("cut off" in p for ps in problems.values() for p in ps))
        source = self._fit_source(batch)
        messages = P.slides_messages(language=self._ctx["lang"], deck_title=self._ctx["deck_title"],
                                     deck_outline=self._ctx["deck_outline"], requests=requests, source=source,
                                     with_image_prompt=with_images, style=self._ctx["style"])
        schema = P.slides_schema(ids, with_images)
        num_predict = min(len(batch) * SLIDE_OUT_TOKENS + 200,
                          self.num_ctx - sum(est_tokens(m["content"]) for m in messages) - MARGIN_TOKENS // 2)
        key = self._cache_key(messages, schema, num_predict)
        data, res = self._call("slides", messages, schema, num_predict)
        if attempt:
            self.stats.repairs += res is not None
        truncated = bool(res and res.truncated)

        if truncated and len(batch) > 1:
            # Too much text for one answer: write the halves separately (bounded: depth <= log2(batch)).
            self.stats.splits += 1
            half = len(batch) // 2
            out = self._write_batch(batch[:half], attempt, {})
            out.update(self._write_batch(batch[half:], attempt, {}))
            return out

        accepted, failed = self._validate_slides(batch, data, truncated,
                                                 final=attempt >= settings.ollama_max_repairs)
        if accepted and res is not None:
            self._cache_put(key, {"slides": [{"slide_id": sid, **raw} for sid, (raw, _) in accepted.items()]})
        result = {sid: slide for sid, (_, slide) in accepted.items()}
        if failed:
            if attempt >= settings.ollama_max_repairs:
                first = next(iter(failed.items()))
                code = "missing_notes" if any("notes" in p for p in first[1]) else (
                    "truncated_output" if truncated else "invalid_output")
                raise AIError(
                    f"Model {self.model} chưa viết được nội dung hợp lệ cho {len(failed)} slide "
                    f"({first[0]}: {'; '.join(first[1][:2])}) sau {attempt + 1} lần thử. Bấm 'Thử lại' "
                    "(các slide đã viết xong được giữ lại).", code)
            log.warning("Ollama slides attempt %d: repairing %s: %s", attempt + 1, list(failed),
                        "; ".join(p for ps in failed.values() for p in ps)[:300])
            retry = [e for e in batch if e.slide_id in failed]
            result.update(self._write_batch(retry, attempt + 1, failed))
        return result

    def _validate_slides(self, batch: list[OutlineEntry], data: Any, truncated: bool, final: bool = False):
        """Validate each slide on its own: returns ({id: (raw, Slide)}, {id: [problems]})."""
        duration = self._duration
        failed: dict[str, list[str]] = {}
        accepted: dict[str, tuple[dict, Slide]] = {}
        items = data.get("slides") if isinstance(data, dict) else None
        if not isinstance(items, list):
            why = "the answer was cut off before the JSON ended" if truncated else "the answer was not valid JSON"
            return {}, {e.slide_id: [why] for e in batch}
        by_id: dict[str, dict] = {}
        for raw in items:
            if isinstance(raw, dict) and raw.get("slide_id") in [e.slide_id for e in batch]:
                by_id.setdefault(raw["slide_id"], raw)  # duplicates: keep the first
        for e in batch:
            raw = by_id.get(e.slide_id)
            if raw is None:
                failed[e.slide_id] = ["this slide is missing from the answer"]
                continue
            try:
                slide, problems = self._to_slide(e, raw, duration, final)
            except ValidationError as exc:
                failed[e.slide_id] = [f"invalid fields: {_short_validation(exc)}"]
                continue
            if problems:
                log.warning("Ollama %s rejected (%s): %s", e.slide_id, "; ".join(problems),
                            json.dumps(raw, ensure_ascii=False)[:600])
                failed[e.slide_id] = problems
            else:
                accepted[e.slide_id] = ({k: v for k, v in raw.items() if k != "slide_id"}, slide)
        return accepted, failed

    def _to_slide(self, e: OutlineEntry, raw: dict, duration: float, final: bool = False) -> tuple[Slide, list[str]]:
        problems: list[str] = []
        rn = raw.get("reading_notes") if isinstance(raw.get("reading_notes"), dict) else {}
        ranges_raw = [r for r in raw.get("source_ranges") or [] if isinstance(r, dict)]
        bad = [r for r in ranges_raw if not _valid_range(r, duration)]
        if bad:
            problems.append(f"source_ranges must be seconds between 0 and {duration:.0f}")
        ranges = [_norm_range(r, duration) for r in ranges_raw if _valid_range(r, duration)] or e.ranges
        layout = raw.get("layout_type") or e.layout_type
        if e.number == 1:
            layout = "title"
        data = {
            "slide_number": e.number,
            "title": str(raw.get("title") or e.title).strip()[:120] or e.title,
            "subtitle": str(raw.get("subtitle") or "").strip()[:200],
            "layout_type": layout,
            "key_points": [str(p) for p in raw.get("key_points") or []][:8],
            "items": [{"heading": str(i.get("heading") or "")[:80], "text": str(i.get("text") or "")[:220]}
                      for i in raw.get("items") or [] if isinstance(i, dict) and str(i.get("heading") or "").strip()][:6],
            "left_title": str(raw.get("left_title") or "")[:60],
            "left_points": [str(p) for p in raw.get("left_points") or []][:6],
            "right_title": str(raw.get("right_title") or "")[:60],
            "right_points": [str(p) for p in raw.get("right_points") or []][:6],
            "speaker_notes": str(raw.get("speaker_notes") or "").strip()[:3000],
            "visual_type": raw.get("visual_type") or "none",
            "visual_description": str(raw.get("visual_description") or "").strip()[:300],
            "image_prompt": str(raw.get("image_prompt") or "") if e.illustration else "",
            "reading_notes": {
                "summary": str(rn.get("summary") or ""), "detailed_explanation": str(rn.get("detailed_explanation") or ""),
                "examples": [str(x) for x in rn.get("examples") or []], "key_takeaway": str(rn.get("key_takeaway") or ""),
            },
            "brief_explanation": str(rn.get("summary") or ""),
            "source_ranges": ranges,
            "source_timestamps": [round(r["start"], 1) for r in ranges][:10],
        }
        if layout in P.TEXT_LAYOUTS and not data["key_points"] and not data["items"]:
            # Small models sometimes put the bullets of an image/bullet slide into the column fields.
            data["key_points"] = (data["left_points"] + data["right_points"])[:6]
        slide = Slide.model_validate(data)
        if not slide.has_body() and final:
            # Last attempt: use the model's own one-line summary/takeaway for this slide as its bullets
            # (no new content) instead of failing the whole deck; reported as a warning.
            points = [x for x in (slide.reading_notes.summary, slide.reading_notes.key_takeaway) if x]
            if points:
                data.update(key_points=points, layout_type="bullets" if layout not in P.TEXT_LAYOUTS else layout)
                slide = Slide.model_validate(data)
                self.stats.warnings.append(f"{e.slide_id}: gạch đầu dòng lấy từ tóm tắt của chính slide "
                                           "(model không điền nội dung trên slide).")
        if not slide.has_body():
            problems.append(f"no on-slide content for layout {slide.layout_type}: put 2-5 short bullets in "
                            "key_points (or 2-6 items for process/timeline/cards/stats/concept)")
        if not slide.speaker_notes.strip():
            problems.append("speaker_notes is empty")
        if slide.layout_type not in ("title", "section", "agenda") and not slide.reading_notes.detailed_explanation:
            problems.append("reading_notes.detailed_explanation is empty")
        if not slide.reading_notes.summary:
            problems.append("reading_notes.summary is empty")
        if not final:  # last attempt: grounding.sanitize() in the pipeline removes what is still unsupported
            problems.extend(grounding.slide_problems(slide, self._req.transcript.text))
        return slide, problems


# ---------------------------------------------------------------------- validation shared with tests

def check_presentation(pres: Presentation, duration: float, expected_ids: list[str] | None = None) -> list[str]:
    """Checks beyond the Pydantic schema. Empty list = OK. (Valid JSON does not prove factual accuracy.)"""
    problems: list[str] = []
    ids = [s.slide_id for s in pres.slides]
    if len(ids) != len(set(ids)):
        problems.append("trùng slide_id")
    if [s.slide_number for s in pres.slides] != list(range(1, len(pres.slides) + 1)):
        problems.append("thứ tự slide không liên tục")
    if expected_ids is not None and ids != expected_ids:
        problems.append(f"thiếu hoặc sai thứ tự slide ({len(ids)}/{len(expected_ids)})")
    for s in pres.slides:
        if not s.has_body():
            problems.append(f"{s.slide_id} không có nội dung")
        if not (s.speaker_notes.strip() or s.reading_notes.summary):
            problems.append(f"{s.slide_id} thiếu ghi chú")
        for r in s.source_ranges:
            if r.start > duration + 1 or r.end > duration + 1:
                problems.append(f"{s.slide_id} có mốc thời gian ngoài video")
    return problems


# ---------------------------------------------------------------------- helpers

_STEP_VI = {"chunk": "bản tóm tắt đoạn transcript", "outline": "dàn ý", "slides": "nội dung slide"}


def _append(messages: list[dict], note: str) -> list[dict]:
    if note:
        messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + "\n" + note}]
    return messages


def _same_model(installed: str, wanted: str) -> bool:
    if installed == wanted:
        return True
    return ":" not in wanted and installed == f"{wanted}:latest"


def _clamp(x: float, duration: float) -> float:
    return max(0.0, min(float(x), duration))


def _valid_range(r: Any, duration: float) -> bool:
    try:
        s, e = float(r["start"]), float(r["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= s <= duration + 1 and 0 <= e <= duration + 1


def _norm_range(r: dict, duration: float) -> dict:
    s, e = _clamp(r["start"], duration), _clamp(r["end"], duration)
    return {"start": round(min(s, e), 1), "end": round(max(s, e), 1)}


def _short_validation(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in exc.errors()[:3])
