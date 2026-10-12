"""Application-level GPU usage ledger, budgets and kill switch.

These limits protect against runaway usage from *this* app, but they are not a
substitute for the provider's own spending limit: the ledger lives on the app's
disk (Replit deployments reset it on redeploy) and GPU cost is an estimate.
Always also set a Workspace budget / spend limit in the Modal dashboard.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .base import ImageGenerationError

_lock = threading.RLock()


def _path() -> Path:
    return config.settings.illustrations_dir / "usage.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load() -> dict:
    p = _path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("days", {})
    data.setdefault("months", {})
    data.setdefault("kill_switch", False)
    return data


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file small: only the last ~60 days and 13 months.
    data["days"] = dict(sorted(data["days"].items())[-60:])
    data["months"] = dict(sorted(data["months"].items())[-13:])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _bucket(data: dict, kind: str, key: str) -> dict:
    return data[kind].setdefault(key, {"images": 0, "gpu_seconds": 0.0, "est_usd": 0.0, "batches": 0})


def estimate_cost_usd(n_images: int) -> float:
    s = config.settings
    if n_images <= 0:
        return 0.0
    seconds = n_images * s.image_est_seconds_per_image + s.image_est_batch_overhead_sec
    return round(seconds * s.image_gpu_price_per_sec, 4)


def kill_switch_active() -> bool:
    if config.settings.image_kill_switch:
        return True
    with _lock:
        return bool(_load().get("kill_switch"))


def set_kill_switch(active: bool) -> None:
    with _lock:
        data = _load()
        data["kill_switch"] = bool(active)
        data["kill_switch_changed_at"] = time.time()
        _save(data)


@dataclass
class Availability:
    available: bool
    reason: str = ""


def availability() -> Availability:
    """Whether new GPU work may start right now (used by /api/config and the pipeline)."""
    s = config.settings
    if not s.ai_images_enabled:
        return Availability(False, "Tính năng ảnh minh họa AI chưa được bật.")
    if kill_switch_active():
        return Availability(False, "Quản trị viên đã tạm dừng tạo ảnh AI.")
    with _lock:
        data = _load()
    day = data["days"].get(_today(), {})
    month = data["months"].get(_month(), {})
    if day.get("images", 0) >= s.image_daily_limit:
        return Availability(False, "Đã đạt giới hạn số ảnh AI trong ngày.")
    if month.get("images", 0) >= s.image_monthly_limit:
        return Availability(False, "Đã đạt giới hạn số ảnh AI trong tháng.")
    if month.get("est_usd", 0.0) >= s.image_monthly_budget_usd:
        return Availability(False, "Đã dùng hết ngân sách GPU của tháng.")
    return Availability(True)


def reserve(n_images: int) -> float:
    """Check every limit for a batch of ``n_images`` *before* any GPU work and record it.

    Raises ImageGenerationError (code ``limit``) when the batch would exceed a limit.
    Returns the estimated batch cost in USD.
    """
    s = config.settings
    if n_images <= 0:
        return 0.0
    av = availability()
    if not av.available:
        raise ImageGenerationError(av.reason, "limit")
    est = estimate_cost_usd(n_images)
    with _lock:
        data = _load()
        day = _bucket(data, "days", _today())
        month = _bucket(data, "months", _month())
        if day["images"] + n_images > s.image_daily_limit:
            raise ImageGenerationError(
                f"Cần {n_images} ảnh nhưng hạn mức ngày chỉ còn {max(0, s.image_daily_limit - day['images'])}.", "limit")
        if month["images"] + n_images > s.image_monthly_limit:
            raise ImageGenerationError(
                f"Cần {n_images} ảnh nhưng hạn mức tháng chỉ còn {max(0, s.image_monthly_limit - month['images'])}.",
                "limit")
        if month["est_usd"] + est > s.image_monthly_budget_usd:
            raise ImageGenerationError(
                f"Chi phí GPU ước tính ${est:.3f} vượt ngân sách tháng còn lại "
                f"${max(0.0, s.image_monthly_budget_usd - month['est_usd']):.3f}.", "limit")
        for b in (day, month):
            b["images"] += n_images
            b["est_usd"] = round(b["est_usd"] + est, 4)
            b["batches"] += 1
        _save(data)
    return est


def record_gpu_seconds(seconds: float) -> None:
    """Add measured inference time (reported by the worker) to the ledger."""
    if seconds <= 0:
        return
    with _lock:
        data = _load()
        for b in (_bucket(data, "days", _today()), _bucket(data, "months", _month())):
            b["gpu_seconds"] = round(b["gpu_seconds"] + seconds, 2)
        _save(data)


def report() -> dict:
    s = config.settings
    with _lock:
        data = _load()
    return {
        "enabled": s.ai_images_enabled,
        "kill_switch": kill_switch_active(),
        "today": data["days"].get(_today(), {}),
        "this_month": data["months"].get(_month(), {}),
        "limits": {
            "per_presentation": s.image_max_per_presentation,
            "daily_images": s.image_daily_limit,
            "monthly_images": s.image_monthly_limit,
            "monthly_budget_usd": s.image_monthly_budget_usd,
        },
        "cost_model": {
            "gpu_price_per_sec": s.image_gpu_price_per_sec,
            "est_seconds_per_image": s.image_est_seconds_per_image,
            "est_batch_overhead_sec": s.image_est_batch_overhead_sec,
            "note": "Ước tính phía ứng dụng. Số liệu chính xác xem tại Modal dashboard → Usage.",
        },
        "history": {"days": data["days"], "months": data["months"]},
    }
