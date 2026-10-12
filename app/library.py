"""Local library of finished presentations (SQLite, single file under DATA_DIR).

Stores what Reading Mode needs to reopen a deck later: the outline (slides, reading notes,
speaker notes, source timestamps), the transcript, and the Google Slides mapping
(presentation id + slide object ids). Job folders keep the files (.pptx, previews,
illustrations); the library survives even if a job folder is removed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS presentations (
    job_id          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    language        TEXT,
    style           TEXT,
    layout_mode     TEXT,
    slide_count     INTEGER,
    source          TEXT,
    outline_json    TEXT NOT NULL,
    transcript_json TEXT,
    google_json     TEXT
)
"""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(settings.data_dir / "library.sqlite3", timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()


def save(job_id: str, *, title: str, language: str, style: str, layout_mode: str, slide_count: int,
         outline_json: str, transcript_json: str | None, source: dict[str, Any] | None) -> None:
    now = time.time()
    with _db() as db:
        db.execute(
            """INSERT INTO presentations (job_id, title, created_at, updated_at, language, style, layout_mode,
                                          slide_count, source, outline_json, transcript_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at,
                   language=excluded.language, style=excluded.style, layout_mode=excluded.layout_mode,
                   slide_count=excluded.slide_count, source=excluded.source, outline_json=excluded.outline_json,
                   transcript_json=excluded.transcript_json""",
            (job_id, title, now, now, language, style, layout_mode, slide_count,
             json.dumps(source or {}, ensure_ascii=False), outline_json, transcript_json),
        )


def set_google(job_id: str, meta: dict[str, Any] | None) -> None:
    with _db() as db:
        db.execute("UPDATE presentations SET google_json = ?, updated_at = ? WHERE job_id = ?",
                   (json.dumps(meta, ensure_ascii=False) if meta else None, time.time(), job_id))


def get(job_id: str) -> dict[str, Any] | None:
    with _db() as db:
        row = db.execute("SELECT * FROM presentations WHERE job_id = ?", (job_id,)).fetchone()
    return _row(row) if row else None


def recent(limit: int = 20) -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute(
            """SELECT job_id, title, created_at, updated_at, language, style, layout_mode, slide_count, source,
                      google_json FROM presentations ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        g = json.loads(r["google_json"]) if r["google_json"] else None
        out.append({
            "job_id": r["job_id"], "title": r["title"], "created_at": r["created_at"],
            "language": r["language"], "style": r["style"], "layout_mode": r["layout_mode"],
            "slide_count": r["slide_count"], "google_url": g.get("url") if g else None,
        })
    return out


def delete(job_id: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM presentations WHERE job_id = ?", (job_id,))


def _row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["source"] = json.loads(d["source"]) if d.get("source") else {}
    d["google"] = json.loads(d.pop("google_json")) if d.get("google_json") else None
    return d
