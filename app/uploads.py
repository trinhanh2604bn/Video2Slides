"""Resumable chunked uploads.

A hosted web request is time-limited (Modal: 150 s), so one big multipart POST fails on a slow connection.
The browser instead sends the file as a series of small PUTs, each appended at an explicit byte offset;
a failed or interrupted chunk is simply re-sent from the offset the server reports.

Layout: ``DATA_DIR/uploads/<upload_id>/meta.json`` + ``data.part``. Finished uploads are moved into the job
folder; abandoned ones are removed after ``UPLOAD_TTL_SEC``.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from .config import settings

UPLOAD_TTL_SEC = 24 * 3600
# Upper bound for one PUT body; the browser picks the actual size from its measured speed.
MAX_CHUNK_BYTES = 64 * 1024 * 1024
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class UploadNotFound(Exception):
    pass


class OffsetMismatch(Exception):
    def __init__(self, received: int):
        self.received = received
        super().__init__(received)


def _root() -> Path:
    return settings.data_dir / "uploads"


def _dir(upload_id: str) -> Path:
    if not _ID_RE.match(upload_id or ""):
        raise UploadNotFound(upload_id)
    return _root() / upload_id


def _lock(upload_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(upload_id, threading.Lock())


def create(filename: str, size: int) -> dict:
    cleanup_stale()
    upload_id = secrets.token_hex(16)
    d = _dir(upload_id)
    d.mkdir(parents=True)
    (d / "data.part").touch()
    meta = {"id": upload_id, "filename": filename, "size": size, "created_at": time.time()}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return status(upload_id)


def _meta(upload_id: str) -> dict:
    try:
        return json.loads((_dir(upload_id) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UploadNotFound(upload_id) from None


def status(upload_id: str) -> dict:
    meta = _meta(upload_id)
    received = (_dir(upload_id) / "data.part").stat().st_size
    return {"upload_id": upload_id, "size": meta["size"], "received": received,
            "complete": received == meta["size"]}


def chunk_file(upload_id: str) -> Path:
    """A fresh temporary file inside the upload folder for one incoming chunk: the request body is
    streamed to disk instead of being held in memory (many parallel 64 MB chunks would exhaust RAM)."""
    _meta(upload_id)
    return _dir(upload_id) / f"chunk-{secrets.token_hex(8)}.tmp"


def append(upload_id: str, offset: int, chunk: Path) -> dict:
    """Append the bytes of the file ``chunk`` at ``offset``. A chunk that was already stored (client retry
    after a lost response) is accepted without writing it twice; any other offset gap raises OffsetMismatch."""
    meta = _meta(upload_id)
    part = _dir(upload_id) / "data.part"
    length = chunk.stat().st_size
    with _lock(upload_id):
        received = part.stat().st_size
        if offset + length <= received and offset < received:
            return status(upload_id)  # duplicate of a stored chunk
        if offset != received:
            raise OffsetMismatch(received)
        if received + length > meta["size"]:
            raise ValueError("chunk exceeds declared size")
        with chunk.open("rb") as src, part.open("ab") as fh:
            shutil.copyfileobj(src, fh, 1024 * 1024)
    return status(upload_id)


def filename(upload_id: str) -> str:
    return _meta(upload_id)["filename"]


def take(upload_id: str, target: Path) -> None:
    """Move a complete upload to ``target``; the upload record is removed."""
    st = status(upload_id)
    if not st["complete"]:
        raise OffsetMismatch(st["received"])
    with _lock(upload_id):
        shutil.move(str(_dir(upload_id) / "data.part"), str(target))
        discard(upload_id)


def discard(upload_id: str) -> None:
    try:
        shutil.rmtree(_dir(upload_id), ignore_errors=True)
    except UploadNotFound:
        return
    with _locks_guard:
        _locks.pop(upload_id, None)


def cleanup_stale() -> None:
    root = _root()
    if not root.is_dir():
        return
    cutoff = time.time() - UPLOAD_TTL_SEC
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff and (d / "data.part").stat().st_mtime < cutoff:
                discard(d.name)
        except OSError:
            continue
