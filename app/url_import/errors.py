"""User-safe error codes for URL import, with Vietnamese messages.

Raw library messages (yt-dlp, httpx) are never shown to users: they can contain
signed media URLs or internal details. They are mapped to these codes instead.
"""

from __future__ import annotations

UPLOAD_HINT = "Bạn có thể tải tệp MP4 lên trực tiếp thay thế."
# YouTube often refuses downloads from datacenter IPs (cloud hosting). The legitimate path for the video's
# owner is the MP4 download in YouTube Studio, then a (chunked, so size is no problem) direct upload.
YOUTUBE_BLOCKED_HINT = ("YouTube thường chặn tải video từ máy chủ đám mây. Nếu bạn là chủ video, hãy tải bản MP4 trong "
                        "YouTube Studio (Nội dung → ⋮ → Tải xuống) rồi dùng mục \"Tải tệp lên\": tệp lớn được gửi "
                        "theo từng phần nên không bị giới hạn thời gian.")
YOUTUBE_BLOCKED_CODES = frozenset({"bot_check", "login_required", "rate_limited", "network_error", "download_failed",
                                   "extraction_failed"})


def fallback_hint(platform: str | None, code: str | None) -> str:
    """What the user can do instead when an import fails."""
    if platform == "youtube" and code in YOUTUBE_BLOCKED_CODES:
        return YOUTUBE_BLOCKED_HINT
    return UPLOAD_HINT


MESSAGES: dict[str, str] = {
    "invalid_url": "Đường dẫn không hợp lệ. Hãy dán một liên kết video YouTube hoặc Facebook đầy đủ.",
    "unsupported_platform": "Chỉ hỗ trợ liên kết video YouTube và Facebook.",
    "unsupported_url": "Liên kết này không phải là một video đơn lẻ được hỗ trợ (ví dụ: danh sách phát, kênh, trang cá nhân).",
    "blocked_address": "Liên kết trỏ tới địa chỉ mạng không được phép.",
    "too_many_redirects": "Liên kết chuyển hướng quá nhiều lần.",
    "unavailable": "Video không tồn tại hoặc đã bị gỡ.",
    "private": "Video ở chế độ riêng tư hoặc bị hạn chế, không thể truy cập.",
    "login_required": "Video yêu cầu đăng nhập để xem. Hệ thống không truy cập nội dung cần đăng nhập.",
    "age_restricted": "Video bị giới hạn độ tuổi nên cần đăng nhập. Hệ thống không truy cập nội dung này.",
    "geo_restricted": "Video bị giới hạn theo khu vực, máy chủ không truy cập được.",
    "live_not_supported": "Không hỗ trợ video đang phát trực tiếp hoặc sắp phát.",
    "drm": "Video được bảo vệ bản quyền (DRM), không thể xử lý.",
    "retrieval_disabled": "Tự động tải video từ nền tảng này đang tắt vì chưa xác lập được quyền tải hợp lệ theo điều khoản của nền tảng.",
    "not_permitted": "Không có quyền tải video này qua phương thức chính thức (token hiện tại không đọc được video).",
    "too_long": "Video dài hơn giới hạn cho phép.",
    "too_large": "Video vượt quá dung lượng tải xuống cho phép.",
    "rate_limited": "Nền tảng đang giới hạn truy cập từ máy chủ. Vui lòng thử lại sau.",
    "bot_check": "Nền tảng yêu cầu xác minh chống bot đối với máy chủ. Hệ thống không vượt qua bước này.",
    "timeout": "Hết thời gian chờ khi kết nối tới nền tảng.",
    "network_error": "Không kết nối được tới nền tảng video.",
    "extraction_failed": "Không lấy được thông tin video từ nền tảng.",
    "download_failed": "Tải video thất bại.",
    "no_audio": "Video không có âm thanh nên không thể chuyển thành văn bản.",
    "rights_not_confirmed": "Bạn cần xác nhận mình sở hữu hoặc có quyền xử lý nội dung này.",
    "busy": "Hệ thống đang bận, vui lòng thử lại sau ít phút.",
    "insufficient_storage": "Máy chủ không đủ dung lượng trống để tải video.",
    "disabled": "Tính năng nhập video từ URL đang tắt.",
}


class UrlImportError(Exception):
    """An error whose ``code`` maps to a user-facing Vietnamese message."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code if code in MESSAGES else "extraction_failed"
        self.detail = detail  # extra user-safe text, e.g. "Video dài 12 phút, giới hạn 10 phút."
        super().__init__(self.message)

    @property
    def message(self) -> str:
        base = MESSAGES[self.code]
        return f"{base} {self.detail}" if self.detail else base
