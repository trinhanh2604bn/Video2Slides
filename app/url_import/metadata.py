"""Metadata from official, documented endpoints only.

* YouTube: public oEmbed (no key) and, when ``YOUTUBE_API_KEY`` is set, the
  YouTube Data API ``videos.list`` for duration, privacy and region limits.
* Facebook: Graph API ``oembed_video`` and the video node, both needing
  ``FACEBOOK_ACCESS_TOKEN``. The video node also reveals whether the token is
  allowed to read the video's ``source`` (the official retrieval path).

Tokens/keys are sent in headers, never in URLs, so they cannot leak through
request logs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings
from .errors import UrlImportError
from .urls import API_HOSTS, ParsedUrl, guarded_get, safe_thumbnail

log = logging.getLogger(__name__)


@dataclass
class Metadata:
    title: str | None = None
    author: str | None = None
    duration_seconds: float | None = None
    thumbnail: str | None = None
    is_live: bool = False
    # Facebook Graph only: the token can read the media source (kept server-side).
    graph_source_readable: bool = False
    notes: list[str] = field(default_factory=list)


def _iso8601_duration(value: str) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(((d * 24 + h) * 60 + mi) * 60 + s)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def youtube_metadata(parsed: ParsedUrl, client: httpx.Client) -> Metadata:
    md = Metadata()
    allowed = API_HOSTS["youtube"]
    resp = guarded_get(client, "https://www.youtube.com/oembed", allowed,
                       params={"url": parsed.canonical_url, "format": "json"})
    if resp.status_code == 200:
        data = _json(resp)
        md.title = str(data.get("title") or "")[:300] or None
        md.author = str(data.get("author_name") or "")[:150] or None
        md.thumbnail = safe_thumbnail(data.get("thumbnail_url"))
    elif resp.status_code in (400, 404):
        raise UrlImportError("unavailable")
    elif resp.status_code in (401, 403):
        # Private, or embedding disabled. Not conclusive on its own.
        md.notes.append("restricted")
    elif resp.status_code == 429:
        raise UrlImportError("rate_limited")

    if settings.youtube_api_key:
        resp = guarded_get(
            client, "https://www.googleapis.com/youtube/v3/videos", allowed,
            params={"part": "snippet,contentDetails,status", "id": parsed.video_id},
            headers={"X-Goog-Api-Key": settings.youtube_api_key},
        )
        if resp.status_code == 200:
            items = _json(resp).get("items") or []
            if not items:
                # Private and deleted videos are both invisible to an API key.
                raise UrlImportError("private" if "restricted" in md.notes else "unavailable")
            item = items[0]
            snippet, details = item.get("snippet") or {}, item.get("contentDetails") or {}
            md.title = md.title or (str(snippet.get("title") or "")[:300] or None)
            md.author = md.author or (str(snippet.get("channelTitle") or "")[:150] or None)
            md.duration_seconds = _iso8601_duration(details.get("duration", ""))
            md.is_live = snippet.get("liveBroadcastContent") in ("live", "upcoming")
            if (details.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
                raise UrlImportError("age_restricted")
            if not md.thumbnail:
                thumbs = snippet.get("thumbnails") or {}
                for key in ("high", "medium", "default"):
                    if (thumbs.get(key) or {}).get("url"):
                        md.thumbnail = safe_thumbnail(thumbs[key]["url"])
                        break
        elif resp.status_code == 429:
            raise UrlImportError("rate_limited")
        else:
            log.warning("YouTube Data API returned HTTP %s", resp.status_code)
    elif "restricted" in md.notes:
        raise UrlImportError("private")
    return md


def _graph_error_code(resp: httpx.Response) -> int | None:
    err = _json(resp).get("error") or {}
    return err.get("code") if isinstance(err, dict) else None


def facebook_metadata(parsed: ParsedUrl, client: httpx.Client, want_source: bool) -> Metadata:
    md = Metadata()
    token = settings.facebook_access_token
    if not token:
        md.notes.append("no_token")
        return md
    allowed = API_HOSTS["facebook"]
    base = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    headers = {"Authorization": f"Bearer {token}"}

    resp = guarded_get(client, f"{base}/oembed_video", allowed,
                       params={"url": parsed.canonical_url, "omitscript": "true"}, headers=headers)
    if resp.status_code == 200:
        md.author = str(_json(resp).get("author_name") or "")[:150] or None
    elif resp.status_code == 429 or _graph_error_code(resp) in (4, 17, 32, 613):
        raise UrlImportError("rate_limited")
    elif resp.status_code in (400, 404):
        # oEmbed only works for public videos; keep going, the node may still be readable.
        md.notes.append("not_public")

    fields = "title,description,length,picture,live_status"
    for with_source in ((True, False) if want_source else (False,)):
        resp = guarded_get(client, f"{base}/{parsed.video_id}", allowed,
                           params={"fields": fields + (",source" if with_source else "")}, headers=headers)
        if resp.status_code == 200:
            data = _json(resp)
            md.title = str(data.get("title") or data.get("description") or "")[:300] or None
            try:
                md.duration_seconds = float(data["length"]) if data.get("length") is not None else None
            except (TypeError, ValueError):
                md.duration_seconds = None
            md.thumbnail = safe_thumbnail(data.get("picture"))
            md.is_live = data.get("live_status") in ("LIVE", "SCHEDULED_LIVE", "SCHEDULED_UNPUBLISHED")
            md.graph_source_readable = with_source and bool(data.get("source"))
            break
        if resp.status_code == 429 or _graph_error_code(resp) in (4, 17, 32, 613):
            raise UrlImportError("rate_limited")
    else:
        md.notes.append("graph_unreadable")

    if "not_public" in md.notes and "graph_unreadable" in md.notes:
        raise UrlImportError("private")
    return md


def graph_source_url(video_id: str, client: httpx.Client) -> str:
    """Fetch a fresh signed media URL through the Graph API (official path)."""
    if not settings.facebook_access_token:
        raise UrlImportError("not_permitted")
    base = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    resp = guarded_get(client, f"{base}/{video_id}", API_HOSTS["facebook"], params={"fields": "source"},
                       headers={"Authorization": f"Bearer {settings.facebook_access_token}"})
    if resp.status_code == 429:
        raise UrlImportError("rate_limited")
    source = _json(resp).get("source") if resp.status_code == 200 else None
    if not source or not isinstance(source, str):
        raise UrlImportError("not_permitted")
    return source
