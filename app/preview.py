"""Slide preview images rendered by a real presentation engine.

Renderers, in order of preference:
* Microsoft PowerPoint through COM (Windows, when Office is installed) - pixel-exact.
* LibreOffice ``soffice`` (any OS) -> PDF -> PNG via ``pypdfium2`` when both are installed.

When neither is available the UI falls back to the outline-based preview, and the
.pptx download still works. Rendering is serialised with a lock: PowerPoint COM and
LibreOffice both misbehave with concurrent automation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PS_SCRIPT = Path(__file__).with_name("preview_powerpoint.ps1")
PREVIEW_WIDTH = 1280
TIMEOUT_SEC = 240
_lock = threading.Lock()
_engine_cache: str | None = None


class PreviewError(Exception):
    """User-safe preview failure."""


def _powerpoint_installed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application\CLSID"):
            return True
    except OSError:
        return False


def _soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base and (p := Path(base) / "LibreOffice" / "program" / "soffice.exe").is_file():
            return str(p)
    return None


def engine() -> str:
    """'powerpoint', 'libreoffice' or '' (no renderer). Cached; set PREVIEW_ENGINE=off to disable."""
    global _engine_cache
    forced = os.environ.get("PREVIEW_ENGINE", "").lower()
    if forced == "off":
        return ""
    if _engine_cache is None:
        if forced in ("", "powerpoint") and _powerpoint_installed():
            _engine_cache = "powerpoint"
        elif forced in ("", "libreoffice") and _soffice():
            try:
                import pypdfium2  # noqa: F401
                _engine_cache = "libreoffice"
            except ImportError:
                _engine_cache = ""
        else:
            _engine_cache = ""
    return _engine_cache


def preview_dir(job_dir: Path) -> Path:
    return job_dir / "preview"


def cached_previews(job_dir: Path, pptx: Path) -> list[Path]:
    """Existing previews, if they are newer than the deck (a regenerated deck invalidates them)."""
    d = preview_dir(job_dir)
    if not d.is_dir() or not pptx.is_file():
        return []
    files = sorted(d.glob("slide-*.png"))
    if not files or min(f.stat().st_mtime for f in files) < pptx.stat().st_mtime:
        return []
    return files


def render_previews(job_dir: Path, pptx: Path) -> list[Path]:
    """Render (or reuse) PNG previews of every slide. Raises PreviewError."""
    eng = engine()
    if not eng:
        raise PreviewError("Không có PowerPoint hoặc LibreOffice để tạo ảnh xem trước.")
    with _lock:
        if files := cached_previews(job_dir, pptx):
            return files
        out = preview_dir(job_dir)
        tmp = Path(tempfile.mkdtemp(prefix="preview-", dir=job_dir))
        try:
            (_render_powerpoint if eng == "powerpoint" else _render_libreoffice)(pptx.resolve(), tmp)
            files = sorted(tmp.glob("slide-*.png"))
            if not files:
                raise PreviewError("Trình kết xuất không tạo ra ảnh nào.")
            shutil.rmtree(out, ignore_errors=True)
            tmp.replace(out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return sorted(out.glob("slide-*.png"))


def _render_powerpoint(pptx: Path, out: Path) -> None:
    args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS_SCRIPT),
            "-Pptx", str(pptx), "-OutDir", str(out.resolve()), "-Width", str(PREVIEW_WIDTH)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SEC,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise PreviewError("PowerPoint không phản hồi khi tạo ảnh xem trước.") from exc
    if proc.returncode != 0:
        log.warning("PowerPoint preview failed: %s", (proc.stderr or proc.stdout)[-800:])
        raise PreviewError("PowerPoint không mở được tệp để tạo ảnh xem trước.")


def _render_libreoffice(pptx: Path, out: Path) -> None:
    import pypdfium2 as pdfium

    profile = out / "lo-profile"
    args = [_soffice() or "soffice", f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless",
            "--convert-to", "pdf", "--outdir", str(out), str(pptx)]
    try:
        subprocess.run(args, capture_output=True, timeout=TIMEOUT_SEC, check=True)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise PreviewError("LibreOffice không chuyển được tệp để tạo ảnh xem trước.") from exc
    pdf = out / f"{pptx.stem}.pdf"
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for i, page in enumerate(doc, start=1):
            scale = PREVIEW_WIDTH / page.get_width()
            page.render(scale=scale).to_pil().save(out / f"slide-{i:02d}.png")
    finally:
        doc.close()
    pdf.unlink(missing_ok=True)
    shutil.rmtree(profile, ignore_errors=True)
