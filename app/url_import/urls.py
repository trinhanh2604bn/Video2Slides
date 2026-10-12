"""Strict URL parsing for YouTube / Facebook video links, plus SSRF guards.

Only an explicit allowlist of hosts and path shapes is accepted, so this is
never a generic downloader. Every outbound request made by the URL feature
(oEmbed, Graph API, short-link resolution, yt-dlp, media download) goes through
:func:`check_outbound`, which re-checks the host allowlist and that DNS resolves
only to public addresses.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

from .errors import UrlImportError

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 5

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
YOUTU_BE_HOSTS = {"youtu.be"}
FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
FB_WATCH_HOSTS = {"fb.watch", "www.fb.watch"}

# Hosts the server may contact for each platform (exact host or subdomain of).
API_HOSTS = {
    "youtube": ("www.youtube.com", "youtube.com", "m.youtube.com", "www.googleapis.com"),
    "facebook": ("graph.facebook.com", "www.facebook.com", "facebook.com", "m.facebook.com",
                 "web.facebook.com", "fb.watch"),
}
# Media/CDN suffixes used by yt-dlp extraction and the actual media download.
MEDIA_SUFFIXES = {
    "youtube": ("youtube.com", "googlevideo.com", "ytimg.com", "googleapis.com", "google.com"),
    "facebook": ("facebook.com", "fbcdn.net", "fb.watch"),
}
THUMBNAIL_SUFFIXES = ("ytimg.com", "fbcdn.net", "googleusercontent.com")

YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
FB_ID_RE = re.compile(r"^\d{5,25}$")
_CTRL_RE = re.compile(r"[\x00-\x20\x7f]")


@dataclass(frozen=True)
class ParsedUrl:
    platform: str          # "youtube" | "facebook"
    kind: str              # "video" | "short" | "reel" | "watch" | "share"
    video_id: str | None   # None until a short link is resolved
    canonical_url: str     # what we pass to oEmbed / yt-dlp
    needs_resolve: bool = False


def host_matches(host: str, suffixes: tuple[str, ...] | set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _split(raw: str):
    if not isinstance(raw, str):
        raise UrlImportError("invalid_url")
    url = raw.strip()
    if not url or len(url) > MAX_URL_LENGTH or _CTRL_RE.search(url):
        raise UrlImportError("invalid_url")
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlImportError("invalid_url") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlImportError("invalid_url")
    if parts.username or parts.password or "@" in parts.netloc:
        raise UrlImportError("invalid_url")
    if port not in (None, 80, 443):
        raise UrlImportError("invalid_url")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlImportError("invalid_url")
    return parts, host


def parse_video_url(raw: str) -> ParsedUrl:
    """Validate a user-supplied URL and return its canonical single-video form."""
    parts, host = _split(raw)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    segs = [s for s in path.split("/") if s]
    query = parse_qs(parts.query)

    if host in YOUTUBE_HOSTS:
        vid, kind = None, "video"
        if segs[:1] == ["watch"] and len(segs) == 1:
            vid = (query.get("v") or [None])[0]
        elif len(segs) == 2 and segs[0] in ("shorts", "embed", "live"):
            vid, kind = segs[1], ("short" if segs[0] == "shorts" else "video")
        if not vid or not YT_ID_RE.match(vid):
            raise UrlImportError("unsupported_url")
        # Any `list=` parameter is dropped: playlists are never processed.
        return ParsedUrl("youtube", kind, vid, f"https://www.youtube.com/watch?v={vid}")

    if host in YOUTU_BE_HOSTS:
        if len(segs) != 1 or not YT_ID_RE.match(segs[0]):
            raise UrlImportError("unsupported_url")
        return ParsedUrl("youtube", "video", segs[0], f"https://www.youtube.com/watch?v={segs[0]}")

    if host in FACEBOOK_HOSTS:
        vid, kind = None, "video"
        if segs and segs[0] in ("watch", "video.php") and len(segs) == 1:
            vid, kind = (query.get("v") or [None])[0], "watch"
        elif len(segs) == 2 and segs[0] in ("reel", "reels"):
            vid, kind = segs[1], "reel"
        elif len(segs) in (3, 4) and segs[1] == "videos":
            # /{page}/videos/{id}  or  /{page}/videos/{slug}/{id}
            vid = segs[-1]
        elif len(segs) == 3 and segs[0] == "share" and segs[1] in ("v", "r"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", segs[2]):
                raise UrlImportError("unsupported_url")
            return ParsedUrl("facebook", "share", None,
                             f"https://www.facebook.com/share/{segs[1]}/{segs[2]}/", needs_resolve=True)
        if not vid or not FB_ID_RE.match(vid):
            raise UrlImportError("unsupported_url")
        if kind == "reel":
            return ParsedUrl("facebook", "reel", vid, f"https://www.facebook.com/reel/{vid}")
        return ParsedUrl("facebook", kind, vid, f"https://www.facebook.com/watch/?v={vid}")

    if host in FB_WATCH_HOSTS:
        if len(segs) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{4,20}", segs[0]):
            raise UrlImportError("unsupported_url")
        return ParsedUrl("facebook", "share", None, f"https://fb.watch/{segs[0]}/", needs_resolve=True)

    raise UrlImportError("unsupported_platform")


# ---------------------------------------------------------------------- SSRF guard

def _ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def resolve_public(host: str) -> list[str]:
    """Resolve ``host`` and require that every address is a public one."""
    try:
        ipaddress.ip_address(host)
        raise UrlImportError("blocked_address")  # IP literals are never allowlisted hosts
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        raise UrlImportError("network_error") from None
    addrs = {info[4][0].split("%")[0] for info in infos}
    if not addrs:
        raise UrlImportError("network_error")
    for a in addrs:
        try:
            ok = _ip_allowed(ipaddress.ip_address(a))
        except ValueError:
            ok = False
        if not ok:
            raise UrlImportError("blocked_address")
    return sorted(addrs)


def check_outbound(url: str, allowed_suffixes: tuple[str, ...]) -> None:
    """Gate for every server-side request: https, allowlisted host, public DNS."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlImportError("blocked_address") from None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        raise UrlImportError("blocked_address")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not host_matches(host, allowed_suffixes):
        raise UrlImportError("blocked_address")
    resolve_public(host)


def safe_thumbnail(url: str | None) -> str | None:
    """Only pass https thumbnails from known platform image CDNs to the browser."""
    if not url or not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or not host_matches(parts.hostname, THUMBNAIL_SUFFIXES):
        return None
    return url


def guarded_get(client: httpx.Client, url: str, allowed: tuple[str, ...], **kw) -> httpx.Response:
    """GET without automatic redirects; each hop is validated by the caller's allowlist."""
    check_outbound(url, allowed)
    return client.get(url, follow_redirects=False, **kw)


def resolve_short_link(parsed: ParsedUrl, client: httpx.Client) -> ParsedUrl:
    """Follow fb.watch / facebook.com/share redirects one hop at a time.

    Each Location must itself parse as a supported Facebook video URL (or be
    another share link), so redirects can never lead to another host.
    """
    url = parsed.canonical_url
    for _ in range(MAX_REDIRECTS):
        resp = guarded_get(client, url, API_HOSTS["facebook"])
        if resp.status_code not in (301, 302, 303, 307, 308):
            if resp.status_code == 404:
                raise UrlImportError("unavailable")
            # No redirect: Facebook usually wants a login for share links it won't expand.
            raise UrlImportError("login_required")
        location = resp.headers.get("location", "")
        target = str(httpx.URL(url).join(location))
        if "/login" in urlsplit(target).path or "/checkpoint" in urlsplit(target).path:
            raise UrlImportError("login_required")
        nxt = parse_video_url(target)  # raises for any non-Facebook / non-video target
        if nxt.platform != "facebook":
            raise UrlImportError("blocked_address")
        if not nxt.needs_resolve:
            return nxt
        url = nxt.canonical_url
    raise UrlImportError("too_many_redirects")
