# Video2Slides

Convert an MP4 video into an editable, professionally designed PowerPoint deck.

**Runs locally on Windows** (no Replit, no domain needed): double-click `install.bat` once, then
`start.bat`. Vietnamese user guide: [docs/HUONG_DAN_SU_DUNG_VI.md](docs/HUONG_DAN_SU_DUNG_VI.md).

```
MP4 upload ─► FFmpeg (audio 16 kHz mono + key frames) ─► faster-whisper (local CPU)
          ─► Gemini (structured outline, JSON schema) ─► python-pptx design engine ─► .pptx download
                                  │ optional (AI_IMAGES_ENABLED)
                                  └► illustration planner ─► self-hosted SDXL on Modal GPU ─► images in the deck
```

* **Single local process**: FastAPI serves the API and a plain HTML/JS frontend on `127.0.0.1`.
  No database, Redis or external storage — job state is JSON on disk.
* **Free/open-source by default**: FFmpeg, faster-whisper and python-pptx run locally. The only
  external call is Gemini (free tier), and the app still works without it in a reduced offline mode.

## Features

| Area | What it does |
|---|---|
| Upload | Drag & drop, client-side size/duration check, server-side re-validation with ffprobe |
| Video | Validates container/streams/limits, extracts mono 16 kHz WAV, samples frames and drops black, blank and duplicate frames (perceptual hash + colour) |
| Speech | faster-whisper `tiny`/`base`/`small`, CPU int8, VAD, timestamps; model loaded once per process, one transcription at a time |
| AI | Gemini REST with `responseSchema`, pydantic validation, self-repair on invalid JSON, backoff on 429/5xx; provider interface for adding Groq etc. |
| Design | 3 themes (Professional, Academic, Creative) × 15 layouts: title, agenda, section, bullets, concept, key insight, image-left, image-right, two-column, process, timeline, cards, stats, summary, conclusion; theme colours/fonts written into the slide master |
| Preview | Slide images rendered by the local PowerPoint (COM) or LibreOffice, shown in the result page |
| Output | 16:9, native editable text and shapes, embedded video frames, speaker notes with source timestamps, overflow prevention, post-build validation |
| Jobs | Stage-based progress with measured values only (decoded audio seconds, finished images), cancel, duplicate-upload detection, retry reusing cached transcript/outline/images, auto-cleanup after `JOB_TTL_HOURS` |
| AI illustrations (optional) | Gemini writes an English `image_prompt` per suitable slide; a planner applies one shared style per deck; a private SDXL 1.0 worker on Modal (L4, scale-to-zero, max 1 GPU) draws them; images are validated, cached and inserted beside editable text. Budgets, daily/monthly limits and a kill switch. Setup: [docs/AI_ILLUSTRATIONS_SETUP_VI.md](docs/AI_ILLUSTRATIONS_SETUP_VI.md) |
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
  ai/                schema, prompt, Gemini provider, offline fallback
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
| `DELETE` | `/api/jobs/{id}` | delete a finished job and its files |
| `GET` | `/api/jobs/{id}/illustrations` | per-slide illustration status |
| `GET` | `/api/jobs/{id}/illustrations/{slide_id}.jpg` | one generated illustration |
| `POST` | `/api/jobs/{id}/illustrations/{slide_id}/regenerate` | redraw one image (new seed) and rebuild the deck |
| `GET` | `/api/admin/images/usage` | GPU usage/limits (header `X-Admin-Token`, needs `ADMIN_TOKEN`) |
| `POST` | `/api/admin/images/kill-switch` | `{"active": true}` stops new GPU work immediately |

`POST /api/jobs` and `/api/videos/url/import` accept `ai_images` (bool) and `image_density`
(`standard` / `high`).

When `APP_PASSWORD` is set, every `/api/jobs*` call needs the header `X-App-Password`.

## Configuration

`install.bat` copies `.env.example` to `.env` once; edit `.env` (never commit it). On a cloud host
use its secret store instead. Main variables:

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | – | Free key from Google AI Studio (see below). Without it the app uses the offline outline. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Any model your key can call, e.g. a specific Flash / Flash-Lite version |
| `AI_PROVIDER` | `auto` | `auto`, `gemini`, `offline` |
| `WHISPER_MODEL` | `base` | `tiny` / `base` / `small` |
| `DEFAULT_LANGUAGE` | `vi` | `vi`, `en`, `auto` |
| `MAX_UPLOAD_MB` / `MAX_DURATION_SEC` / `MAX_FRAMES` | 80 / 600 / 8 | MVP limits |
| `MAX_QUEUE_SIZE` | 3 | uploads beyond this get HTTP 429 |
| `JOB_TTL_HOURS` | 24 | finished jobs are deleted afterwards |
| `APP_PASSWORD` | – | optional shared password for the whole app |
| `AI_IMAGES_ENABLED` | `false` | feature flag for AI illustrations; see `.env.example` for all `IMAGE_*` / `MODAL_*` settings |

### Getting a Gemini API key

1. Open <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** (a free-tier project is created automatically).
3. Put it in Replit → **Tools → Secrets** as `GEMINI_API_KEY` (or in your local `.env`).
4. Restart the app. The header badge changes from "AI: offline" to "AI: Gemini".

Free-tier quotas (requests/minute and per day) change over time and depend on the model;
check your project's limits in AI Studio. The app makes **one** Gemini request per video
(plus at most two repair/retry requests on errors).

## Run locally (Windows)

| File | What it does |
|---|---|
| `install.bat` | One-time setup: `.venv` + packages, FFmpeg check (offers `winget install Gyan.FFmpeg`), `.env` from template, Whisper model download. Safe to re-run. |
| `start.bat` | Checks the environment, starts the server on `127.0.0.1:8000` (`APP_PORT`), opens the browser when ready. Nothing is reinstalled. |
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
4. Add `GEMINI_API_KEY` (and optionally `APP_PASSWORD`, `WHISPER_MODEL`) in **Secrets**.
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
* One job runs at a time; others wait (queue max `MAX_QUEUE_SIZE`).
* The job queue is in-process: if the server restarts mid-job, that job is marked failed and
  can be retried (cached transcript is reused). It is not a durable queue.
* Text-fit uses conservative glyph-width estimates rather than real font metrics, so some
  boxes may end up with more whitespace than strictly necessary.
* Speech recognition quality depends on the model size and audio; proper names are often wrong.
* Without `GEMINI_API_KEY` the offline outline only splits and extracts transcript sentences —
  it is a fallback, not a substitute for the AI outline.
* No external image search. AI illustrations are optional and need the Modal GPU worker; without it
  visuals are video frames and native shapes. The illustration usage ledger is stored on local disk
  (reset by a Replit redeploy), so always also set a budget in the Modal dashboard.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/api/health` shows `ffmpeg: null` | Make sure `replit.nix` / `.replit` includes `ffmpeg-headless` and reload the Repl; locally install FFmpeg and add it to `PATH` |
| "Không đủ bộ nhớ…" / process killed during transcription | Set `WHISPER_MODEL=tiny` or use a machine with more RAM |
| "GEMINI_API_KEY không hợp lệ…" | Recreate the key in AI Studio and update the Secret |
| "Model Gemini … không tồn tại" | Set `GEMINI_MODEL` to a model listed in AI Studio for your key |
| HTTP 429 from Gemini after retries | Free-tier quota reached; wait or reduce usage, then press **Thử lại** |
| Job failed after a restart | Press **Thử lại**; cached steps are reused |
