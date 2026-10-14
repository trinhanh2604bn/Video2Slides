"""Google OAuth 2.0 for a local (installed) app: loopback redirect + PKCE, least-privilege scope.

* Scope ``drive.file`` only: the app can create files and read/write the files it created,
  nothing else in the user's Drive. Slides API calls on those presentations accept it too.
* Client: an OAuth client of type "Desktop app" downloaded from Google Cloud Console as JSON
  (``GOOGLE_OAUTH_CLIENT_FILE``). Desktop clients allow any ``http://127.0.0.1:<port>`` redirect.
* App opened on a public host (e.g. the Modal web app): a "Web application" client whose
  ``redirect_uris`` list ``https://<host>/api/google/oauth/callback`` redirects back directly; a Desktop
  client falls back to a manual flow — Google redirects to an unused loopback port, the page there fails to
  load, and the user pastes that address back into the app (:func:`finish_from_url`).
* Tokens are kept in a plain JSON file under DATA_DIR (``data/google/token.json``, ignored by
  Git) and never reach the browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..config import settings
from .errors import GoogleAuthError, GoogleError

log = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/drive.file",)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
CALLBACK_PATH = "/api/google/oauth/callback"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MANUAL_REDIRECT_URI = f"http://127.0.0.1:53682{CALLBACK_PATH}"  # nothing listens here; the user copies the URL
STATE_TTL_SEC = 600
REFRESH_MARGIN_SEC = 120

_lock = threading.RLock()
_pending: dict[str, tuple[str, str, float]] = {}  # state -> (code_verifier, redirect_uri, expires_at)


def _default_http() -> httpx.Client:
    return httpx.Client(timeout=30)


# Replaced in tests with a client on an httpx.MockTransport.
http_factory: Callable[[], httpx.Client] = _default_http


# ---------------------------------------------------------------------- client configuration

@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str
    kind: str = "installed"                 # "installed" (Desktop app) or "web"
    redirect_uris: tuple[str, ...] = ()


def load_client() -> ClientConfig | None:
    """The OAuth client from the downloaded JSON (``installed`` or ``web`` section), or None."""
    path = settings.google_oauth_client_file
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    kind = "installed" if data.get("installed") else "web"
    section = data.get(kind) or {}
    cid, secret = str(section.get("client_id") or ""), str(section.get("client_secret") or "")
    if not cid.endswith(".apps.googleusercontent.com"):
        return None
    uris = tuple(str(u).rstrip("/") for u in section.get("redirect_uris") or ())
    return ClientConfig(cid, secret, kind, uris)


def configured() -> bool:
    return settings.google_slides_enabled and load_client() is not None


# ---------------------------------------------------------------------- token storage

def _token_file() -> Path:
    return settings.google_dir / "token.json"


def _state_file() -> Path:
    return settings.google_dir / "state.json"


def load_token() -> dict[str, Any] | None:
    try:
        return json.loads(_token_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("Google token file unreadable; the account must be connected again")
        return None


def _save_token(tok: dict[str, Any]) -> None:
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    tmp.replace(path)


def _set_state(**fields: Any) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _get_state() -> dict[str, Any]:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def forget(reason: str | None = None) -> None:
    """Delete stored tokens locally (and remember why, for the status shown in the UI)."""
    with _lock:
        _token_file().unlink(missing_ok=True)
        _set_state(last_error=reason, account=None)


# ---------------------------------------------------------------------- authorization flow

def redirect_uri_for(base_url: str) -> str:
    """Loopback redirect for the URL the user opened the app with. Rejects non-loopback hosts."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or host not in LOOPBACK_HOSTS:
        raise GoogleAuthError("Đăng nhập Google chỉ hoạt động khi mở ứng dụng tại http://127.0.0.1 "
                              "hoặc http://localhost trên chính máy này.", "not_loopback")
    netloc = f"[{host}]" if host == "::1" else host
    if parts.port:
        netloc += f":{parts.port}"
    return f"http://{netloc}{CALLBACK_PATH}"


def plan_redirect(base_url: str) -> tuple[str, bool]:
    """``(redirect_uri, manual)`` for the URL the app was opened with.

    Loopback: direct redirect. Public host: direct only with a "web" client that lists this callback;
    with a Desktop client, the manual paste flow (``manual=True``).
    """
    try:
        return redirect_uri_for(base_url), False
    except GoogleAuthError:
        pass
    client = load_client()
    if client is None:
        raise GoogleAuthError("Chưa cấu hình OAuth client của Google (xem docs/GOOGLE_SLIDES_SETUP_VI.md).",
                              "not_configured")
    direct = base_url.rstrip("/") + CALLBACK_PATH
    if client.kind == "web":
        if direct in client.redirect_uris:
            return direct, False
        raise GoogleAuthError(f"OAuth client loại Web chưa khai báo Authorized redirect URI {direct} "
                              "trong Google Cloud Console.", "redirect_not_registered")
    return MANUAL_REDIRECT_URI, True


def start_authorization(redirect_uri: str) -> str:
    """Authorization URL for Google's consent screen (PKCE S256 + one-time state)."""
    client = load_client()
    if client is None:
        raise GoogleAuthError("Chưa cấu hình OAuth client của Google (xem docs/GOOGLE_SLIDES_SETUP_VI.md).",
                              "not_configured")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if v[2] < now]:
            _pending.pop(k, None)
        _pending[state] = (verifier, redirect_uri, now + STATE_TTL_SEC)
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",       # we need a refresh token
        "prompt": "consent",            # always return a refresh token, also on reconnect
        "include_granted_scopes": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def finish_authorization(state: str, code: str) -> dict[str, Any]:
    """Exchange the authorization code; stores the tokens and returns :func:`status`."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if pending is None or pending[2] < time.time():
        raise GoogleAuthError("Phiên đăng nhập Google đã hết hạn hoặc không hợp lệ. Hãy bấm Kết nối lại.",
                              "bad_state")
    verifier, redirect_uri, _ = pending
    client = load_client()
    if client is None:
        raise GoogleAuthError("Chưa cấu hình OAuth client của Google.", "not_configured")
    with http_factory() as http:
        resp = http.post(TOKEN_URL, data={
            "code": code, "client_id": client.client_id, "client_secret": client.client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        })
        if resp.status_code != 200:
            raise GoogleAuthError(f"Google từ chối mã đăng nhập ({_oauth_error(resp)}). Hãy thử kết nối lại.",
                                  "exchange_failed")
        data = resp.json()
        granted = set((data.get("scope") or "").split())
        if not set(SCOPES) <= granted:
            raise GoogleAuthError("Bạn chưa cấp quyền tạo tệp trên Google Drive, nên ứng dụng không thể tạo "
                                  "Google Slides. Hãy kết nối lại và đánh dấu quyền đó.", "scope_denied")
        if not data.get("refresh_token"):
            raise GoogleAuthError("Google không trả về refresh token. Hãy gỡ quyền ứng dụng trong tài khoản Google "
                                  "rồi kết nối lại.", "no_refresh_token")
        tok = {
            "refresh_token": data["refresh_token"],
            "access_token": data.get("access_token", ""),
            "expires_at": time.time() + float(data.get("expires_in") or 0),
            "scope": data.get("scope", ""),
            "connected_at": time.time(),
        }
        account = _account(http, tok["access_token"])
    with _lock:
        _save_token(tok)
        _set_state(last_error=None, account=account)
    return status()


def finish_from_url(pasted: str) -> dict[str, Any]:
    """Manual flow: the user pastes the address Google redirected to (it holds ``state`` and ``code``)."""
    query = parse_qs(urlsplit((pasted or "").strip()).query)
    if (query.get("error") or [""])[0]:
        raise GoogleAuthError("Bạn đã hủy kết nối Google hoặc Google báo lỗi khi đăng nhập.", "denied")
    state, code = (query.get("state") or [""])[0], (query.get("code") or [""])[0]
    if not state or not code:
        raise GoogleAuthError("Địa chỉ đã dán không chứa mã đăng nhập Google. Hãy chép toàn bộ địa chỉ trên "
                              "thanh địa chỉ của tab sau khi đăng nhập (bắt đầu bằng http://127.0.0.1:53682/).",
                              "bad_paste")
    return finish_authorization(state, code)


def _account(http: httpx.Client, access_token: str) -> dict[str, str] | None:
    """Best effort: which Google account is connected (Drive about, allowed with drive.file)."""
    try:
        resp = http.get(ABOUT_URL, params={"fields": "user(displayName,emailAddress)"},
                        headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code == 200:
            user = resp.json().get("user") or {}
            return {"email": str(user.get("emailAddress") or ""), "name": str(user.get("displayName") or "")}
    except (httpx.HTTPError, ValueError):
        pass
    return None


def access_token(force_refresh: bool = False) -> str:
    """A valid access token, refreshed when it is about to expire. Raises GoogleAuthError."""
    with _lock:
        tok = load_token()
        if not tok:
            raise GoogleAuthError("Chưa kết nối tài khoản Google.", "not_connected")
        if not force_refresh and tok.get("access_token") and tok.get("expires_at", 0) - REFRESH_MARGIN_SEC > time.time():
            return tok["access_token"]
        client = load_client()
        if client is None:
            raise GoogleAuthError("Chưa cấu hình OAuth client của Google.", "not_configured")
        try:
            with http_factory() as http:
                resp = http.post(TOKEN_URL, data={
                    "client_id": client.client_id, "client_secret": client.client_secret,
                    "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
                })
        except httpx.HTTPError as exc:
            raise GoogleError(f"Không kết nối được máy chủ Google ({exc.__class__.__name__}).", "network") from None
        if resp.status_code in (400, 401) and _oauth_error(resp) in ("invalid_grant", "unauthorized_client",
                                                                      "invalid_client"):
            # Revoked by the user, password change, token expired (unverified apps: 7 days), client deleted.
            forget("revoked")
            raise GoogleAuthError("Quyền truy cập Google đã bị thu hồi hoặc hết hạn. Hãy kết nối lại tài khoản.",
                                  "revoked")
        if resp.status_code != 200:
            raise GoogleError(f"Không làm mới được phiên đăng nhập Google (HTTP {resp.status_code}).",
                              "refresh_failed", resp.status_code)
        data = resp.json()
        tok["access_token"] = data["access_token"]
        tok["expires_at"] = time.time() + float(data.get("expires_in") or 3600)
        if data.get("refresh_token"):
            tok["refresh_token"] = data["refresh_token"]
        _save_token(tok)
        return tok["access_token"]


def disconnect() -> None:
    """Revoke the grant at Google (best effort) and delete local tokens. Files in Drive are kept."""
    tok = load_token()
    if tok:
        try:
            with http_factory() as http:
                http.post(REVOKE_URL, data={"token": tok["refresh_token"]})
        except httpx.HTTPError:
            log.warning("Google token revocation request failed; local tokens are deleted anyway")
    forget(None)


def status() -> dict[str, Any]:
    """Safe-to-show connection status (never includes tokens)."""
    st = _get_state()
    tok = load_token()
    return {
        "enabled": settings.google_slides_enabled,
        "configured": configured(),
        "connected": bool(tok),
        "account": st.get("account") if tok else None,
        "scopes": list(SCOPES),
        "last_error": st.get("last_error"),
    }


def _oauth_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or f"HTTP {resp.status_code}")
    except ValueError:
        return f"HTTP {resp.status_code}"
