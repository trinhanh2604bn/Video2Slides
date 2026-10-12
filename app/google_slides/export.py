"""PPTX -> native Google Slides via Drive conversion, then validation and speaker-notes sync.

Why conversion instead of drawing every slide with the Slides API: the existing python-pptx
engine already produces the designed, editable deck (15 layouts, 3 themes, text fitting,
AI illustrations). Drive converts it into a native, editable Google Slides file in one upload.
The result is then read back through the Slides API and checked (slide count, 16:9, pictures,
titles), and every slide's speaker notes are compared with the expected text and rewritten
through the documented ``speakerNotesObjectId`` mechanism when they are missing or differ.

Idempotency: each upload carries ``appProperties`` (job id + hash of the exact .pptx), so a
retry after a crash finds the presentation created earlier instead of making a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ..ai.schema import Presentation
from ..reading import speaker_notes_text
from .client import DRIVE, DRIVE_UPLOAD, SLIDES, GoogleApi
from .errors import GoogleError

log = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SLIDES_MIME = "application/vnd.google-apps.presentation"
PRESENTATION_FIELDS = (
    "presentationId,title,pageSize,"
    "slides(objectId,pageElements(objectId,image(contentUrl),shape(text(textElements(textRun(content))))),"
    "slideProperties(notesPage(notesProperties,pageElements(objectId,shape(text(textElements(textRun(content))))))))"
)


def presentation_url(presentation_id: str) -> str:
    return f"https://docs.google.com/presentation/d/{presentation_id}/edit"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _shape_text(element: dict[str, Any]) -> str:
    runs = ((element.get("shape") or {}).get("text") or {}).get("textElements") or []
    return "".join((r.get("textRun") or {}).get("content", "") for r in runs)


def notes_shape(slide: dict[str, Any]) -> tuple[str | None, str]:
    """(speakerNotesObjectId, current notes text) of a Slides API slide resource."""
    page = (slide.get("slideProperties") or {}).get("notesPage") or {}
    sid = (page.get("notesProperties") or {}).get("speakerNotesObjectId")
    for el in page.get("pageElements") or []:
        if el.get("objectId") == sid:
            return sid, _shape_text(el)
    return sid, ""  # the shape may not exist yet; inserting text with this id creates it


def _find_existing(api: GoogleApi, job_id: str, build: str) -> dict[str, Any] | None:
    q = (f"appProperties has {{ key='v2s_job' and value='{job_id}' }} and "
         f"appProperties has {{ key='v2s_build' and value='{build}' }} and "
         f"mimeType='{SLIDES_MIME}' and trashed=false")
    files = api.json("GET", f"{DRIVE}/files", params={"q": q, "fields": "files(id,name,createdTime)",
                                                      "spaces": "drive", "pageSize": 5}).get("files") or []
    return files[0] if files else None


def file_alive(api: GoogleApi, file_id: str) -> bool:
    try:
        meta = api.json("GET", f"{DRIVE}/files/{file_id}", params={"fields": "id,trashed"})
    except GoogleError as exc:
        if exc.code == "not_found":
            return False
        raise
    return not meta.get("trashed")


def _upload_converted(api: GoogleApi, pptx: Path, name: str, app_props: dict[str, str]) -> str:
    """Resumable upload with conversion to Google Slides (works for decks larger than 5 MB)."""
    data = pptx.read_bytes()
    meta = {"name": name[:200], "mimeType": SLIDES_MIME, "appProperties": app_props,
            "description": "Tạo tự động bởi Video2Slides"}
    start = api.request("POST", DRIVE_UPLOAD, params={"uploadType": "resumable", "fields": "id"},
                        headers={"Content-Type": "application/json; charset=UTF-8",
                                 "X-Upload-Content-Type": PPTX_MIME, "X-Upload-Content-Length": str(len(data))},
                        content=json.dumps(meta).encode("utf-8"))
    session = start.headers.get("location")
    if not session or not session.startswith("https://www.googleapis.com/"):
        raise GoogleError("Google không mở được phiên tải lên.", "upload_failed")
    done = api.request("PUT", session, headers={"Content-Type": PPTX_MIME}, content=data)
    file_id = (done.json() or {}).get("id")
    if not file_id:
        raise GoogleError("Google không trả về mã tệp sau khi tải lên.", "upload_failed")
    return str(file_id)


def read_presentation(api: GoogleApi, presentation_id: str) -> dict[str, Any]:
    return api.json("GET", f"{SLIDES}/presentations/{presentation_id}", params={"fields": PRESENTATION_FIELDS})


def validate(doc: dict[str, Any], pres: Presentation, expected_pictures: int | None) -> dict[str, Any]:
    """Check the converted deck instead of assuming it matches the .pptx."""
    slides = doc.get("slides") or []
    errors: list[str] = []
    warnings: list[str] = []
    if len(slides) != len(pres.slides):
        errors.append(f"Google Slides có {len(slides)} slide, dự kiến {len(pres.slides)}.")
    size = doc.get("pageSize") or {}
    w = (size.get("width") or {}).get("magnitude") or 0
    h = (size.get("height") or {}).get("magnitude") or 0
    if not h or abs(w / h - 16 / 9) > 0.02:
        errors.append("Kích thước trang không phải 16:9.")
    images = sum(1 for sl in slides for el in sl.get("pageElements") or [] if "image" in el)
    if expected_pictures is not None and images < expected_pictures:
        warnings.append(f"Có {images}/{expected_pictures} hình ảnh sau khi chuyển đổi.")
    for idx, (gs, s) in enumerate(zip(slides, pres.slides), start=1):
        text = _norm(" ".join(_shape_text(el) for el in gs.get("pageElements") or []))
        head = _norm(s.title)[:15]
        if head and head not in text:
            warnings.append(f"Slide {idx}: không thấy tiêu đề sau chuyển đổi.")
        if not text:
            errors.append(f"Slide {idx} không có chữ có thể chỉnh sửa.")
    return {"slide_count": len(slides), "images": images, "errors": errors, "warnings": warnings[:10]}


def sync_notes(api: GoogleApi, presentation_id: str, doc: dict[str, Any], pres: Presentation) -> dict[str, int]:
    """Make every slide's speaker notes equal to the expected text; verify by reading them back."""
    slides = doc.get("slides") or []
    expected = [speaker_notes_text(s, pres.language) for s in pres.slides]
    requests: list[dict[str, Any]] = []
    for gs, want in zip(slides, expected):
        sid, have = notes_shape(gs)
        if not sid or _norm(have) == _norm(want):
            continue
        if have.strip():
            requests.append({"deleteText": {"objectId": sid, "textRange": {"type": "ALL"}}})
        if want:
            requests.append({"insertText": {"objectId": sid, "insertionIndex": 0, "text": want}})
    fixed = sum(1 for r in requests if "insertText" in r)
    if requests:
        api.json("POST", f"{SLIDES}/presentations/{presentation_id}:batchUpdate", json={"requests": requests})
        doc = read_presentation(api, presentation_id)
        slides = doc.get("slides") or []
    verified = sum(1 for gs, want in zip(slides, expected) if _norm(notes_shape(gs)[1]) == _norm(want))
    return {"total": len(expected), "verified": verified, "fixed": fixed}


def export_presentation(pptx: Path, pres: Presentation, job_id: str, *, expected_pictures: int | None = None,
                        previous: dict[str, Any] | None = None, new_copy: bool = False,
                        api_factory: Callable[[], GoogleApi] = GoogleApi,
                        progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Create (or reuse) the native Google Slides version of ``pptx``. Returns metadata for the job.

    ``previous`` is the metadata of an earlier export of this job. It is reused when it is
    still in Drive and was made from the same .pptx; a changed deck or ``new_copy`` creates a
    new presentation. Existing presentations are never overwritten (the user may have edited them).
    """
    step = progress or (lambda _msg: None)
    build = file_hash(pptx)
    with api_factory() as api:
        file_id = None
        reused = False
        if previous and previous.get("presentation_id") and not new_copy and previous.get("build_hash") == build:
            if file_alive(api, previous["presentation_id"]):
                file_id, reused = previous["presentation_id"], True
        if file_id is None and not new_copy:
            found = _find_existing(api, job_id, build)  # created by an interrupted earlier attempt
            if found:
                file_id, reused = str(found["id"]), True
        if file_id is None:
            step("uploading")
            props = {"v2s_job": job_id, "v2s_build": build, "v2s_created": str(int(time.time()))}
            file_id = _upload_converted(api, pptx, pres.presentation_title, props)
        step("validating")
        doc = read_presentation(api, file_id)
        report = validate(doc, pres, expected_pictures)
        step("notes")
        notes = sync_notes(api, file_id, doc, pres)
        slide_ids = [gs.get("objectId") for gs in doc.get("slides") or []]
        return {
            "presentation_id": file_id,
            "url": presentation_url(file_id),
            "title": doc.get("title") or pres.presentation_title,
            "created_at": (previous["created_at"] if reused and previous and previous.get("created_at")
                           and previous.get("presentation_id") == file_id else time.time()),
            "build_hash": build,
            "reused": reused,
            "slides": [{"slide_id": s.slide_id, "object_id": oid} for s, oid in zip(pres.slides, slide_ids)],
            "validation": report,
            "notes": notes,
            "api_calls": api.calls,
        }


def check_sync(meta: dict[str, Any], api_factory: Callable[[], GoogleApi] = GoogleApi) -> dict[str, Any]:
    """Compare the live presentation with the stored slide mapping. Read-only: never writes."""
    with api_factory() as api:
        if not file_alive(api, meta["presentation_id"]):
            return {"exists": False, "missing": [s["slide_id"] for s in meta.get("slides") or []],
                    "reordered": False, "added": 0, "notes_changed": []}
        doc = api.json("GET", f"{SLIDES}/presentations/{meta['presentation_id']}",
                       params={"fields": "slides(objectId,slideProperties(notesPage(notesProperties,"
                                         "pageElements(objectId,shape(text(textElements(textRun(content))))))))"})
    live = [s.get("objectId") for s in doc.get("slides") or []]
    stored = [s["object_id"] for s in meta.get("slides") or []]
    present = [oid for oid in stored if oid in live]
    missing = [s["slide_id"] for s in meta.get("slides") or [] if s["object_id"] not in live]
    order_live = [oid for oid in live if oid in stored]
    return {"exists": True, "missing": missing, "reordered": order_live != present,
            "added": len([oid for oid in live if oid not in stored]), "live_order": live,
            "notes": {s.get("objectId"): notes_shape(s)[1] for s in doc.get("slides") or []}}


def fetch_thumbnail(meta: dict[str, Any], index: int, dest: Path,
                    api_factory: Callable[[], GoogleApi] = GoogleApi) -> Path:
    """Download a PNG thumbnail of slide ``index`` (0-based). The contentUrl is short-lived,
    so the image is cached on disk and the URL itself is never stored or sent to the browser."""
    slides = meta.get("slides") or []
    if not 0 <= index < len(slides):
        raise GoogleError("Không có slide này.", "not_found", 404)
    with api_factory() as api:
        info = api.json("GET", f"{SLIDES}/presentations/{meta['presentation_id']}/pages/{slides[index]['object_id']}"
                               "/thumbnail", params={"thumbnailProperties.mimeType": "PNG",
                                                     "thumbnailProperties.thumbnailSize": "LARGE"})
        url = str(info.get("contentUrl") or "")
        if not url.startswith("https://") or ".googleusercontent.com/" not in url:
            raise GoogleError("Google không trả về ảnh thu nhỏ hợp lệ.", "thumbnail_failed")
        try:
            resp = api.http.get(url, follow_redirects=True)  # signed URL: no Authorization header needed
        except httpx.HTTPError:
            raise GoogleError("Không tải được ảnh thu nhỏ từ Google.", "thumbnail_failed") from None
    if resp.status_code != 200 or not resp.content.startswith(b"\x89PNG"):
        raise GoogleError("Không tải được ảnh thu nhỏ từ Google.", "thumbnail_failed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest
