# Video2Slides

Convert a video (MP4 upload or a YouTube / Facebook link) into an editable, professionally designed
presentation — **native Google Slides** in your own Drive and/or PowerPoint — with a detailed explanation
for every slide (speaker notes + a synchronized Reading Mode with the notes below the slide).

**Runs locally on Windows** (no Replit, no domain needed): double-click `install.bat` once, then
`start.bat`. With Node.js installed you can use `npm run setup` (once), `npm start`, `npm run stop`
and `npm test` instead. Vietnamese user guide: [docs/HUONG_DAN_SU_DUNG_VI.md](docs/HUONG_DAN_SU_DUNG_VI.md).

```
MP4 upload / YouTube·Facebook link (yt-dlp)
  ─► Modal media worker: FFmpeg on CPU (audio 16 kHz mono + key frames) ─► faster-whisper on an L4 GPU
      [or both on this computer with MEDIA_PROCESSING=local]
  ─► LLM (default LLM_PROVIDER=modal_ollama): Ollama + qwen3:4b on a Modal L4 GPU [auto = Gemini first, Ollama fallback]
      (outline, slide text, speaker + reading notes; JSON schema, validated, checkpointed)
  ─► [illustration planner ─► self-hosted SDXL on Modal GPU]            (optional, AI_IMAGES_ENABLED)
  ─► python-pptx design engine (Reading or Presentation layout) ─► .pptx
  ─► [Drive upload+conversion ─► Slides API validation + speaker-notes sync] ─► native Google Slides  (optional)
  ─► Reading Mode (full-width slide, synchronized explanation below it) + library (SQLite)
```

* **Single local process**: FastAPI serves the API and a plain HTML/JS frontend on `127.0.0.1`.
  No Redis or external storage — job state is JSON on disk, the library of finished decks is one SQLite file.
* **Cloud processing by default** (when the Modal workers are configured): the Windows app keeps the UI,
  orchestration, PPTX and previews; video processing (FFmpeg + GPU Whisper) and the fallback LLM run on private,
  proxy-auth Modal workers that scale to zero. No local FFmpeg, Whisper, Ollama or model download is needed.
  By default only Ollama on Modal writes the slides; set `LLM_PROVIDER=auto` to try Gemini first.
  Setup, routing rules and costs: [docs/MODAL_CLOUD_SETUP_VI.md](docs/MODAL_CLOUD_SETUP_VI.md).

## Features

| Area | What it does |
|---|---|
| Upload | Drag & drop, client-side size/duration check, server-side re-validation with ffprobe |
| Video | Validates container/streams/limits, extracts mono 16 kHz WAV, samples frames and drops black, blank and duplicate frames (perceptual hash + colour) |
| Speech | faster-whisper `tiny`/`base`/`small`, CPU int8, VAD, timestamps; model loaded once per process, one transcription at a time |
| AI | Provider interface (`app/ai`): **Ollama** (default, `qwen3:4b`, native `/api/chat`, JSON-schema `format`, streamed so jobs can be cancelled), Gemini (optional, explicit), offline. Ollama generation is staged: long transcripts are split at pauses and condensed with timestamps, an outline is planned, then slides are written 3 at a time. Every answer is validated (Pydantic + slide ids/order/count, non-empty notes, timestamps inside the video, truncation) with at most 2 targeted repairs; validated steps are cached by model + prompt version + settings + input. Details: [docs/OLLAMA_SETUP_VI.md](docs/OLLAMA_SETUP_VI.md) |
| Design | 3 themes (Professional, Academic, Creative) × 15 layouts: title, agenda, section, bullets, concept, key insight, image-left, image-right, two-column, process, timeline, cards, stats, summary, conclusion; theme colours/fonts written into the slide master |
| Preview | Slide images rendered by the local PowerPoint (COM) or LibreOffice, shown in the result page |
| Output | 16:9, native editable text and shapes, embedded video frames, speaker notes with source timestamps, overflow prevention, post-build validation |
| Jobs | Stage-based progress with measured values only (decoded audio seconds, slides written, finished images), cancel, duplicate-upload detection, retry reusing cached transcript/outline/images, auto-cleanup after `JOB_TTL_HOURS` |
| AI illustrations (optional) | The LLM writes an English `image_prompt` per suitable slide; a planner applies one shared style per deck; a private SDXL 1.0 worker on Modal (L4, scale-to-zero, max 1 GPU) draws them; images are validated, cached and inserted beside editable text. Budgets, daily/monthly limits and a kill switch. Setup: [docs/AI_ILLUSTRATIONS_SETUP_VI.md](docs/AI_ILLUSTRATIONS_SETUP_VI.md) |
| Reading notes | For every slide the LLM also writes a summary, a detailed explanation (80–160 words), examples the speaker actually gave, a key takeaway and source time ranges. Older outlines / offline mode fall back to the transcript spans of the slide |
| Slide canvas | Every slide uses the full 16:9 canvas; there is no explanation panel on the slide. The detailed explanation, examples, key takeaway and video timestamps go into the speaker notes (PPTX and Google Slides) and the in-app Reading Mode. `layout_mode` is still accepted for compatibility |
| Google Slides | OAuth (Desktop client, loopback + PKCE, scope `drive.file` only, token in `data/google/token.json`). The finished .pptx is uploaded with conversion to a native, editable Google Slides file, read back through the Slides API and validated (slide count, 16:9, pictures, titles); speaker notes are compared per slide and rewritten via `speakerNotesObjectId` when missing, then verified. Idempotent (Drive `appProperties`), never overwrites an existing presentation, retries 429/5xx, reuses the existing AI images. Setup: [docs/GOOGLE_SLIDES_SETUP_VI.md](docs/GOOGLE_SLIDES_SETUP_VI.md) |
| Reading Mode | Slide image (local PowerPoint render, else cached Google thumbnail, else a text card) with the explanation below it; prev/next, thumbnails, ← → / Home / End / F, swipe, fullscreen, counter, progress, timestamps, transcript excerpt; stacks on phones. A read-only "check sync" reports slides deleted/added/reordered or notes edited in Google Slides |
| Library | "Bài đã tạo": reopen earlier decks in Reading Mode / Google Slides / re-download (`KEEP_FINISHED_PRESENTATIONS`) |
| Citations | Every on-slide point is linked to the moment it is said (word matching against the transcript in `app/citations.py`, no LLM, computed when Reading Mode opens). Timestamps play the kept source video in the app (signed, short-lived link with HTTP Range; kept `KEEP_SOURCE_VIDEO_HOURS`, default 24, then deleted by the cleanup) or open YouTube at that second |
| Studio | Mind map and timeline straight from the outline (instant); summary, FAQ, quiz and flashcards written by the LLM only on request, citing slide numbers (`app/studio.py`). Stored per deck, marked stale after the deck changes |
| Slide edits | Plain-language edit requests, queued and applied together; the pipeline's slide writer (same schema, grounding checks, repairs) rewrites only those slides, then the .pptx is rebuilt from cached frames/illustrations. Undo keeps the last 10 outlines; a Google Slides copy is marked stale, never overwritten (`app/slide_edit.py`) |
| Side tasks | Studio and slide edits never enter the job queue: each runs on its own background thread, and before every LLM request it waits while the job worker is in an LLM stage (`app/ai/assist.py`, `jobs.main_llm_busy`), so a deck being made is not slowed down |
| Security | Server-issued job IDs only (no user paths), no `shell=True`, upload size cap while streaming, optional shared password (`APP_PASSWORD`), API key sent in a header (never in URLs/logs) |

## Project layout

```
app/
  main.py            FastAPI routes + static frontend
  config.py          all limits/settings from environment variables
  jobs.py            file-backed job store + single worker thread
  pipeline.py        stage orchestration with caching
  video.py           ffprobe/ffmpeg helpers
  transcribe.py      faster-whisper wrapper
  ai/                schema, provider registry, Ollama provider (ollama.py, ollama_client.py, ollama_prompts.py),
                     health check, Gemini provider (optional), offline fallback
  reading.py         reading notes: transcript fallback, speaker-notes text, Reading Mode payload
  library.py         SQLite library of finished presentations (outline, transcript, Google mapping)
  google_slides/     OAuth (auth.py), REST client with retries (client.py), PPTX→Slides export, validation,
                     speaker-notes sync, sync check, thumbnails (export.py)
  illustrations/     image provider interface, Modal SDXL + mock providers, planner, styles,
                     batch orchestration/cache, usage limits & kill switch
  pptx_engine/       themes, text fitting, layouts, validation
  static/            index.html, app.js, styles.css
infrastructure/modal_image_worker.py   private SDXL GPU worker (deployed with `modal deploy`)
scripts/download_model.py   pre-download Whisper weights
scripts/test_image_worker.py  admin smoke test of the GPU worker (1-3 images, timings)
tests/                      pytest suite
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | ffmpeg/ffprobe availability |
| `GET` | `/api/config` | limits, styles, languages, active AI provider |
| `POST` | `/api/jobs` | multipart upload (`file`, `title`, `language`, `style`, `slide_count`) → `202 {job_id}` |
| `GET` | `/api/jobs/{id}` | status, current stage, stage timings, result/error |
| `POST` | `/api/jobs/{id}/retry` | re-run a failed or cancelled job (cached steps reused) |
| `POST` | `/api/jobs/{id}/cancel` | stop a queued/running job (pending GPU work is cancelled) |
| `GET` | `/api/jobs/{id}/preview` | list of slide preview images (rendered on demand) |
| `GET` | `/api/jobs/{id}/preview/{n}.png` | one slide preview |
| `GET` | `/api/jobs/{id}/transcript` | `transcript.json` |
| `GET` | `/api/jobs/{id}/transcript.txt` | readable transcript |
| `GET` | `/api/jobs/{id}/download` | the `.pptx` |
| `GET` | `/api/jobs/{id}/download.pdf` | vector PDF, one 960×540 pt page per slide (text kept as text, fonts embedded); rendered on demand by PowerPoint/LibreOffice and cached until the deck changes |
| `GET` | `/api/jobs/{id}/figma.zip` | one SVG per slide for Figma Design (which cannot import PDF): live `<text>` layers, vector shapes, embedded pictures; PDF → SVG with PyMuPDF (AGPL), cached (`app/figma_export.py`) |
| `GET` | `/api/jobs/{id}/figma/{n}.svg` | one slide's SVG (the UI copies it for Ctrl+V into Figma) |
| `DELETE` | `/api/jobs/{id}` | delete a finished job and its files |
| `GET` | `/api/jobs/{id}/illustrations` | per-slide illustration status |
| `GET` | `/api/jobs/{id}/illustrations/{slide_id}.jpg` | one generated illustration |
| `POST` | `/api/jobs/{id}/illustrations/{slide_id}/regenerate` | redraw one image (new seed) and rebuild the deck |
| `GET` | `/api/jobs/{id}/reading` | slides + explanations + image URLs for Reading Mode |
| `POST` | `/api/jobs/{id}/google` | `{"new_copy": false}` create (or reuse) the Google Slides version in the background |
| `POST` | `/api/jobs/{id}/google/check` | read-only comparison with the live Google Slides file |
| `GET` | `/api/jobs/{id}/google/thumbnail/{n}.png` | slide thumbnail from Google (cached on disk) |
| `GET` | `/api/google/status` | configured / connected / account (never tokens) |
| `POST` | `/api/google/connect` | returns the Google consent URL (loopback redirect, PKCE) |
| `GET` | `/api/google/oauth/callback` | OAuth redirect target |
| `POST` | `/api/google/disconnect` | revoke access and delete the local token |
| `GET` | `/api/library` | finished presentations |
| `GET` | `/api/jobs/{id}/video/link` | signed, short-lived URL of the kept source video (in-app playback) |
| `GET` | `/api/jobs/{id}/video?exp=&sig=` | the source video (HTTP Range) |
| `GET` | `/api/jobs/{id}/studio` | Studio outputs and their state + slide index (number, title, first moment) |
| `GET` | `/api/jobs/{id}/studio/{kind}` | `mindmap` / `timeline` (instant) or `summary` / `faq` / `quiz` / `flashcards` |
| `POST` | `/api/jobs/{id}/studio/{kind}` | write one generated output on the studio thread (202) |
| `POST` | `/api/jobs/{id}/edits` | `{"edits": [{"slide_number": 3, "instruction": "..."}]}` rewrite those slides and rebuild (202) |
| `POST` | `/api/jobs/{id}/edits/undo` | restore the outline from before the last edit and rebuild (202) |
| `GET` | `/api/admin/images/usage` | GPU usage/limits (header `X-Admin-Token`, needs `ADMIN_TOKEN`) |
| `POST` | `/api/admin/images/kill-switch` | `{"active": true}` stops new GPU work immediately |

`POST /api/jobs` and `/api/videos/url/import` accept `ai_images` (bool), `image_density`
(`standard` / `high`), `layout_mode` (`reading` / `presentation`) and `google_slides` (bool, needs a
connected Google account).

When `APP_PASSWORD` is set, every `/api/jobs*` call needs the header `X-App-Password`.

## Configuration

`install.bat` copies `.env.example` to `.env` once; edit `.env` (never commit it). On a cloud host
use its secret store instead. Main variables:

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `modal_ollama` | `modal_ollama` (Ollama on Modal only, default), `auto` (Gemini first, Ollama on Modal fallback), `gemini`, `modal_ollama`, `ollama` (local, explicit only), `offline`. The older `AI_PROVIDER` name is still read |
| `MODAL_LLM_WORKER_URL` / `MODAL_MEDIA_WORKER_URL` | – | URLs of the deployed Modal workers (same `MODAL_PROXY_TOKEN_*` as the image worker) |
| `MEDIA_PROCESSING` | `auto` | `auto` (Modal when `MODAL_MEDIA_WORKER_URL` is set), `modal`, `local` |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Keep Ollama on localhost |
| `OLLAMA_MODEL` | `qwen3:4b` | Never upgraded automatically |
| `OLLAMA_NUM_CTX` / `OLLAMA_TEMPERATURE` / `OLLAMA_KEEP_ALIVE` | 8192 / 0.2 / `5m` | Sent as `options.num_ctx`, `options.temperature`, `keep_alive` |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | 600 | Per request |
| `OLLAMA_THINK` | `auto` | `auto`/`off` disable Qwen3 thinking (sent as `think: false` only if the model reports the capability), `on` enables it |
| `OLLAMA_SLIDES_PER_BATCH` / `OLLAMA_MAX_REPAIRS` | 3 / 2 | Slides per request; repair attempts after an invalid answer |
| `RELEASE_WHISPER_BEFORE_LLM` | `true` | Free the Whisper model before LLM inference |
| `GEMINI_API_KEY` / `GEMINI_MODEL` / `GEMINI_FALLBACK_MODELS` | – | Used first in `auto` mode when the key is set; `GEMINI_MAX_ATTEMPTS`, `LLM_MAX_REPAIR_ATTEMPTS`, `LLM_CIRCUIT_*` bound retries |
| `DEFAULT_LAYOUT_MODE` | `presentation` | `reading` / `presentation` (both render full-width slides) |
| `YOUTUBE_RETRIEVAL` / `FACEBOOK_RETRIEVAL` | `off` (template: `ytdlp`) | `ytdlp` downloads links with yt-dlp; only for content you may process |
| `GOOGLE_SLIDES_ENABLED` | `true` | Google Slides output (needs the OAuth client file) |
| `GOOGLE_OAUTH_CLIENT_FILE` | `data/google/client_secret.json` | Desktop-app OAuth client JSON |
| `KEEP_FINISHED_PRESENTATIONS` | `true` | keep finished decks for the library |
| `KEEP_SOURCE_VIDEO_HOURS` | 24 | source video kept (renamed, not copied) for in-app playback of Reading Mode timestamps, then deleted by the periodic cleanup; `0` = delete as soon as the deck is done |
| `WHISPER_MODEL` | `base` | `tiny` / `base` / `small` |
| `DEFAULT_LANGUAGE` | `vi` | `vi`, `en`, `auto` |
| `MAX_UPLOAD_MB` / `MAX_DURATION_SEC` / `MAX_FRAMES` | 80 / 600 / 8 | MVP limits |
| `MAX_QUEUE_SIZE` | 3 | uploads beyond this get HTTP 429 |
| `JOB_TTL_HOURS` | 24 | finished jobs are deleted afterwards |
| `APP_PASSWORD` | – | optional shared password for the whole app |
| `AI_IMAGES_ENABLED` | `false` | feature flag for AI illustrations; see `.env.example` for all `IMAGE_*` / `MODAL_*` settings |

### Cloud: Modal workers (default path)

See [docs/MODAL_CLOUD_SETUP_VI.md](docs/MODAL_CLOUD_SETUP_VI.md): deploy `infrastructure/modal_llm_worker.py`
and `infrastructure/modal_media_worker.py`, run `setup_model` once (qwen3:4b, ~2.5 GB, into a Modal Volume),
then set `MODAL_LLM_WORKER_URL` / `MODAL_MEDIA_WORKER_URL`. Live checks: `scripts/modal_llm_live_check.py`,
`scripts/modal_media_live_check.py`.

### Optional: local LLM (Ollama, `LLM_PROVIDER=ollama`)

`install.bat` checks Ollama (offers `winget install Ollama.Ollama`), starts it on localhost if it is not
running (an already running Ollama is reused) and, after asking, downloads `qwen3:4b` (~2.5 GB) once.
`start.bat` checks the service and the model on every start. `GET /api/llm/status` reports readiness.
Setup, hardware notes and measurements: [docs/OLLAMA_SETUP_VI.md](docs/OLLAMA_SETUP_VI.md),
[docs/OLLAMA_TEST_REPORT_VI.md](docs/OLLAMA_TEST_REPORT_VI.md).

### Gemini API

With `LLM_PROVIDER=auto` (opt-in; the default `modal_ollama` skips Gemini) a configured `GEMINI_API_KEY` (from <https://aistudio.google.com/apikey>)
is used first; this sends the transcript text to Google and may be billed depending on your plan. Without a
key, or when Gemini fails (bad key, quota, outage, timeout, invalid output), the deck is written by Ollama on
Modal automatically. Safety refusals are never bypassed. `LLM_PROVIDER=gemini` = Gemini only.

## Run locally (Windows)

| File | What it does |
|---|---|
| `install.bat` | One-time setup: `.venv` + packages, `.env` from template. FFmpeg (offers `winget install Gyan.FFmpeg`) and the Whisper model only for local video processing; Ollama + `qwen3:4b` only with `LLM_PROVIDER=ollama`. Safe to re-run. |
| `start.bat` | Checks the Modal / Gemini configuration (no secrets printed) or, for local processing, FFmpeg and Ollama; Google authorization; starts the server on `127.0.0.1:8000` (`APP_PORT`), opens the browser when ready. Nothing is reinstalled. |
| `stop.bat` | Stops a server started by `start.bat` (via `data/server.pid`). |

Manual equivalent (any OS; requirements: Python 3.11–3.12, FFmpeg on `PATH`):

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python scripts/download_model.py          # use .venv/bin/python on Linux/macOS
.venv/Scripts/python scripts/run_local.py               # or: -m uvicorn app.main:app --port 8000
```

Admin scripts: `scripts/check_worker_auth.py` (GPU worker auth, no GPU used),
`scripts/test_image_worker.py` (1–3 real images, uses GPU), `scripts/make_test_video.ps1`
(synthetic Vietnamese lecture for end-to-end tests, Windows vi-VN voice).

### Tests

```bash
.venv/Scripts/python -m pytest -q
```

The real-speech test is opt-in because it loads the Whisper model:

```bash
V2S_REAL_VIDEO=path/to/vietnamese.mp4 .venv/Scripts/python -m pytest -m slow
```

## Optional: run on Replit

Replit is no longer required; the configuration (`.replit`, `replit.nix`) is kept for an optional
cloud deployment.

1. Import the GitHub repository into Replit (Python template).
2. `.replit` / `replit.nix` already request Python 3.11 and `ffmpeg-headless`; no existing
   packages are removed.
3. In the Shell:
   ```bash
   pip install -e .
   python scripts/download_model.py
   ```
   (`uv sync` also works.) Weights go to `.cache/models` inside the workspace, so they are
   downloaded once, not per request.
4. A cloud host has no local Ollama: set `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` (paid API) or point
   `OLLAMA_BASE_URL` at an Ollama you run (optionally `APP_PASSWORD`, `WHISPER_MODEL`) in **Secrets**.
5. Press **Run**. Verify `GET /api/health` returns `"ok": true`.

### Resource measurements

Measured on a desktop CPU with `cpu_threads=2`, int8, 110 s Vietnamese speech:

| Model | Transcription time | Peak RAM (Whisper process) | Quality on the test clip |
|---|---|---|---|
| `tiny` | 15 s | ~270 MB | many word errors |
| `base` | 23 s | ~340 MB | understandable; some errors, e.g. "25 phút" heard as "205 phút" |
| `small` | 56 s | ~710 MB | clearly better; numbers correct |

Add roughly 100–150 MB for FastAPI, python-pptx and image handling. A shared 0.5 vCPU on
Replit will be several times slower than these numbers (not measured yet) — expect minutes,
not seconds, for a 10-minute video with `base`.

### Choosing a Replit runtime

| Option | Fits this app? | Notes |
|---|---|---|
| **Workspace (Run button)** | Good for development and demos | Free, but stops when idle/closed; workspace RAM on the free plan may be too small for `base` (use `WHISPER_MODEL=tiny`) |
| **Autoscale deployment** | Not recommended | Scales to zero and can stop instances between requests; long background jobs and the on-disk job state can be lost; cold starts reload the Whisper model |
| **Reserved VM deployment** | Recommended for real use | Always-on process keeps the model in memory and background jobs alive; pick ≥ 2 GB RAM for `base`/`small`. Paid monthly — check current pricing in Replit before choosing |

Deployments are **not** configured in `.replit`; choose and pay for one deliberately via
Replit's Publishing tool. Deployment file systems are not persistent across redeploys, which
is fine because jobs are temporary (24 h) — but the Whisper model must be downloaded again
after a redeploy (run `scripts/download_model.py` in the build command).

## Limitations

* MVP limits: MP4 only, ≤ 80 MB, ≤ 10 minutes, ≤ 8 frames (all configurable).
* Google Slides is produced by Drive's PPTX conversion. It keeps text, shapes, pictures and notes editable,
  but the rendering can differ slightly from PowerPoint (font substitution, e.g. Segoe UI → a Google font,
  line breaks). Each export is validated, not assumed identical.
* Reading Mode shows the explanation generated with the deck. Edits made later in Google Slides are **not**
  synchronised back; "Kiểm tra đồng bộ" only reports them, and the app never overwrites a Google file.
* With the OAuth app in *Testing* status Google expires the login after 7 days (reconnect, or publish the app).
* One job runs at a time; others wait (queue max `MAX_QUEUE_SIZE`).
* The job queue is in-process: if the server restarts mid-job, that job is marked failed and
  can be retried (cached transcript is reused). It is not a durable queue.
* Text-fit uses conservative glyph-width estimates rather than real font metrics, so some
  boxes may end up with more whitespace than strictly necessary.
* Speech recognition quality depends on the model size and audio; proper names are often wrong.
* The local `qwen3:4b` runs on the CPU when there is no supported GPU: a short video takes minutes of LLM
  time (see the test report). Valid JSON does not prove factual accuracy — review the slides.
* `LLM_PROVIDER=offline` only splits and extracts transcript sentences — a fallback, not a substitute.
* No external image search. AI illustrations are optional and need the Modal GPU worker; without it
  visuals are video frames and native shapes. The illustration usage ledger is stored on local disk
  (reset by a Replit redeploy), so always also set a budget in the Modal dashboard.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/api/health` shows `ffmpeg: null` | Make sure `replit.nix` / `.replit` includes `ffmpeg-headless` and reload the Repl; locally install FFmpeg and add it to `PATH` |
| "Không đủ bộ nhớ…" / process killed during transcription | Set `WHISPER_MODEL=tiny` or use a machine with more RAM |
| "Không kết nối được Ollama…" | Open the Ollama app (or `ollama serve`), then **Thử lại**; transcript is reused |
| "Ollama chưa có model…" | `ollama pull qwen3:4b` (or re-run `install.bat`) |
| "Không đủ RAM…" / very slow LLM step | Close memory-heavy apps, keep `OLLAMA_NUM_CTX=8192` or lower; see docs/OLLAMA_SETUP_VI.md |
| "…chưa tạo được … hợp lệ sau 3 lần thử" | Press **Thử lại** (finished steps are cached); try fewer slides |
| "GEMINI_API_KEY không hợp lệ…" (only with `LLM_PROVIDER=gemini`) | Recreate the key in AI Studio and update `.env` |
| "Model Gemini … không tồn tại" | Set `GEMINI_MODEL` to a model listed in AI Studio for your key |
| HTTP 429 from Gemini after retries | Free-tier quota reached; wait or reduce usage, then press **Thử lại** |
| Job failed after a restart | Press **Thử lại**; cached steps are reused |
