"""Long videos in ``LLM_PROVIDER=auto``: staged generation with Gemini first and per-unit fallback to Modal Ollama.

For long transcripts one Gemini answer for the whole deck (30-40 slides with notes) is slow and can be cut off, and a
failure would throw away everything. Here the deck is produced in the same small, validated, checkpointed units as
with Ollama (transcript part summaries -> outline -> batches of slides; ``OllamaProvider``). Each unit is first sent to
Gemini (bounded transport retries + repairs); after an eligible Gemini failure the job switches to Ollama on Modal for
the remaining units only, keeping every unit already validated. The same rules as ``app.ai.router`` apply: safety
refusals and our own bad requests stop the job, cancellation stops it, auth/quota/config failures open the circuit.

Cache entries are keyed by this route (both candidate models + prompt version + settings + input) and record the
provider that actually produced them; ``route.providers_used`` lists every provider whose content is in the deck.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable

from ..config import settings
from . import ollama_prompts as P
from .base import AIError, OutlineRequest
from .gemini import GeminiError
from .gemini_chat import GeminiChatClient
from .ollama import OllamaProvider
from .schema import Presentation

log = logging.getLogger(__name__)

# Outline/slide output can be cut off or invalid; these are worth one more try on the other model.
UNIT_RETRY_CODES = {"invalid_output", "truncated_output", "missing_notes"}


class StagedRoutedProvider(OllamaProvider):
    name = "auto_staged"

    def __init__(self, gemini_client: GeminiChatClient | None, modal_factory: Callable[[], Any],
                 on_fallback: Callable[[str, Exception], None] | None = None,
                 say: Callable[[str], None] | None = None):
        self._gemini = gemini_client
        self._modal_factory = modal_factory
        self._modal = None
        self._on_fallback = on_fallback or (lambda kind, exc: None)
        self._say = say or (lambda m: None)
        self.active = "gemini" if gemini_client is not None else "modal_ollama"
        super().__init__(client=gemini_client or self._modal_client(), model=self._model_of(self.active))
        self.num_ctx = settings.llm_long_num_ctx
        self.providers_used: list[str] = []
        self.fallback: dict | None = None
        self._unit_retry = False

    # ------------------------------------------------------------------ provider switching

    def _modal_client(self):
        if self._modal is None:
            self._modal = self._modal_factory()
        return self._modal.client

    def _model_of(self, provider: str) -> str:
        return (self._gemini.model if provider == "gemini" and self._gemini else settings.modal_llm_model)

    def _switch(self, kind: str, exc: Exception) -> None:
        log.warning("staged route: Gemini unit failed (%s); remaining units go to Ollama on Modal", kind)
        self.fallback = {"kind": kind, "message": str(exc)[:200], "after_requests": self.stats.requests}
        self._on_fallback(kind, exc)
        self.active = "modal_ollama"
        self.client = self._modal_client()
        self.model = settings.modal_llm_model
        self._prepare_modal()
        self.num_ctx = self._modal.clamp_ctx(self.num_ctx)

    def _prepare(self) -> None:
        if self.active == "gemini":
            self.server_version = "gemini-api"
            self.info = self.client.show(self.model)
            self.think = None
        else:
            self._prepare_modal()
        self.num_ctx = settings.llm_long_num_ctx
        if self.active != "gemini":
            self.num_ctx = self._modal.clamp_ctx(self.num_ctx)

    def _prepare_modal(self) -> None:
        modal = self._modal or self._modal_factory()
        self._modal = modal
        modal._prepare()  # health check (CPU only), model readiness, thinking capability
        self.server_version = modal.server_version
        self.info = modal.info
        self.think = modal.think

    # ------------------------------------------------------------------ one request = one fallback unit

    def _call(self, step: str, messages: list[dict], schema: dict, num_predict: int):
        try:
            data, res = super()._call(step, messages, schema, num_predict)
        except GeminiError as exc:
            if self.active != "gemini" or not _eligible(exc):
                raise
            self._switch(exc.kind, exc)
            data, res = super()._call(step, messages, schema, num_predict)
        if res is not None:
            self.stats.calls[-1]["provider"] = self.active
            if self.active not in self.providers_used:
                self.providers_used.append(self.active)
        return data, res

    def _structured(self, step: str, build, schema: dict, num_predict: int, validate) -> Any:
        try:
            return super()._structured(step, build, schema, num_predict, validate)
        except AIError as exc:
            if self.active != "gemini" or exc.code not in UNIT_RETRY_CODES:
                raise
            self._switch("invalid_output", exc)
            return super()._structured(step, build, schema, num_predict, validate)

    def _write_batch(self, batch, attempt: int, problems: dict):
        if attempt or self._unit_retry:
            return super()._write_batch(batch, attempt, problems)
        try:
            return super()._write_batch(batch, attempt, problems)
        except AIError as exc:
            if self.active != "gemini" or exc.code not in UNIT_RETRY_CODES:
                raise
            self._switch("invalid_output", exc)
            self._unit_retry = True
            try:
                return super()._write_batch(batch, 0, {})
            finally:
                self._unit_retry = False

    # ------------------------------------------------------------------ cache identity + provenance

    def _cache_key(self, messages: list[dict], schema: dict, num_predict: int) -> str:
        blob = json.dumps({"v": P.PROMPT_VERSION, "route": "gemini>modal_ollama",
                           "models": [settings.gemini_model, settings.modal_llm_model], "messages": messages,
                           "schema": schema, "num_ctx": self.num_ctx, "temperature": settings.ollama_temperature,
                           "num_predict": num_predict}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]

    def _cache_put(self, key: str, data: Any) -> None:
        if not self._cache_dir:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_dir / f"{key}.tmp"
        tmp.write_text(json.dumps({"data": data, "provider": self.active, "model": self.model,
                                   "route": "gemini>modal_ollama", "prompt_version": P.PROMPT_VERSION,
                                   "created_at": time.time()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._cache_dir / f"{key}.json")

    def generate(self, req: OutlineRequest) -> Presentation:
        if self.active == "gemini":
            self._say("Đang phân tích nội dung bằng Gemini (theo từng phần).")
        return super().generate(req)

    def describe(self) -> dict:
        out = {**super().describe(), "provider": self.providers_used[-1] if self.providers_used else self.active,
               "mode": "staged", "providers_used": self.providers_used, "unit_fallback": self.fallback,
               "num_ctx": self.num_ctx}
        if self._gemini is not None:
            out["gemini_requests"] = self._gemini.requests
            out["gemini_retries"] = self._gemini.retries
        if self._modal is not None:
            out["modal"] = self._modal.client.usage()
        return out


def _eligible(exc: GeminiError) -> bool:
    from .router import FALLBACK_KINDS

    return exc.kind in FALLBACK_KINDS
