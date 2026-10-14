"""Google OAuth + Drive conversion + Slides notes, against an in-memory fake Google (no network)."""

import base64
import dataclasses
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import config, library, main, pipeline
from app.google_slides import GoogleAuthError, GoogleError, auth
from app.google_slides import client as gclient
from app.google_slides import export as gexport
from app.pptx_engine import build_pptx
from app.reading import speaker_notes_text
from app.transcribe import Segment, Transcript
from tests.fake_google import FakeGoogle
from tests.sample_outline import sample_presentation

CLIENT_JSON = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-test",
                             "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                             "token_uri": "https://oauth2.googleapis.com/token"}}
REDIRECT = "http://127.0.0.1:8000/api/google/oauth/callback"


@pytest.fixture
def google(monkeypatch, tmp_path):
    """Configured OAuth client, isolated token storage and a fake Google backend."""
    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
    s = dataclasses.replace(config.settings, data_dir=tmp_path / "data", google_oauth_client_file=secret,
                            google_slides_enabled=True)
    for mod in (config, auth, gclient):
        monkeypatch.setattr(mod, "settings", s)
    fake = FakeGoogle()
    monkeypatch.setattr(auth, "http_factory", fake.client)
    monkeypatch.setattr(gclient, "http_factory", fake.client)
    monkeypatch.setattr(gclient.time, "sleep", lambda s: None)
    return fake


def _connect(fake: FakeGoogle) -> None:
    url = auth.start_authorization(REDIRECT)
    state = parse_qs(urlsplit(url).query)["state"][0]
    auth.finish_authorization(state, "code-1")


def _api(fake):
    return lambda: gclient.GoogleApi(http=fake.client(), sleep=lambda s: None)


# ---------------------------------------------------------------------- OAuth

def test_authorization_url_uses_pkce_least_privilege_and_loopback(google):
    url = auth.start_authorization(REDIRECT)
    q = parse_qs(urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert q["redirect_uri"] == [REDIRECT] and q["client_id"] == ["123-abc.apps.googleusercontent.com"]
    assert "client_secret" not in url and "GOCSPX" not in url


def test_redirect_must_be_loopback():
    assert auth.redirect_uri_for("http://localhost:8000/") == "http://localhost:8000/api/google/oauth/callback"
    with pytest.raises(GoogleAuthError):
        auth.redirect_uri_for("http://192.168.1.5:8000/")
    with pytest.raises(GoogleAuthError):
        auth.redirect_uri_for("https://example.com/")


def test_public_host_with_desktop_client_uses_manual_paste_flow(google):
    redirect, manual = auth.plan_redirect("https://me--app-web.modal.run/")
    assert manual and redirect == auth.MANUAL_REDIRECT_URI
    assert auth.plan_redirect("http://127.0.0.1:8000/") == (REDIRECT, False)
    state = parse_qs(urlsplit(auth.start_authorization(redirect)).query)["state"][0]
    with pytest.raises(GoogleAuthError) as exc:
        auth.finish_from_url("http://127.0.0.1:53682/api/google/oauth/callback")
    assert exc.value.code == "bad_paste"
    status = auth.finish_from_url(f"  {redirect}?state={state}&code=code-1&scope=x  ")
    assert status["connected"] and google.token_posts[0]["redirect_uri"] == auth.MANUAL_REDIRECT_URI


def test_public_host_with_web_client_redirects_directly(google, tmp_path):
    cb = "https://me--app-web.modal.run/api/google/oauth/callback"
    web = {"web": {**CLIENT_JSON["installed"], "redirect_uris": [cb]}}
    auth.settings.google_oauth_client_file.write_text(json.dumps(web), encoding="utf-8")
    assert auth.plan_redirect("https://me--app-web.modal.run/") == (cb, False)
    assert auth.plan_redirect("http://me--app-web.modal.run/") == (cb, False)  # TLS ended at the proxy
    with pytest.raises(GoogleAuthError) as exc:
        auth.plan_redirect("https://other.example/")
    assert exc.value.code == "redirect_not_registered"


def test_code_exchange_sends_verifier_and_stores_tokens(google):
    url = auth.start_authorization(REDIRECT)
    q = parse_qs(urlsplit(url).query)
    status = auth.finish_authorization(q["state"][0], "code-1")
    sent = google.token_posts[0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"][0] and sent["redirect_uri"] == REDIRECT
    assert status["connected"] and status["account"]["email"] == "test@example.com"
    assert "rt-SECRET" not in json.dumps(status)
    assert (auth.settings.google_dir / "token.json").is_file()
    assert auth.load_token()["refresh_token"] == "rt-SECRET-123"


def test_state_is_single_use(google):
    url = auth.start_authorization(REDIRECT)
    state = parse_qs(urlsplit(url).query)["state"][0]
    auth.finish_authorization(state, "code-1")
    with pytest.raises(GoogleAuthError, match="hết hạn|không hợp lệ"):
        auth.finish_authorization(state, "code-1")
    with pytest.raises(GoogleAuthError):
        auth.finish_authorization("forged", "code-1")


def test_unchecked_drive_permission_is_reported(google):
    google.scope = "openid"
    url = auth.start_authorization(REDIRECT)
    with pytest.raises(GoogleAuthError) as exc:
        auth.finish_authorization(parse_qs(urlsplit(url).query)["state"][0], "c")
    assert exc.value.code == "scope_denied" and auth.load_token() is None


def test_refresh_and_revoked_access(google):
    _connect(google)
    tok = auth.load_token()
    tok["expires_at"] = time.time() - 10
    auth._save_token(tok)
    assert auth.access_token() == "at-2"  # refreshed
    google.refresh_error = "invalid_grant"
    tok = auth.load_token()
    tok["expires_at"] = 0
    auth._save_token(tok)
    with pytest.raises(GoogleAuthError) as exc:
        auth.access_token()
    assert exc.value.code == "revoked"
    st = auth.status()
    assert not st["connected"] and st["last_error"] == "revoked"


def test_disconnect_revokes_and_deletes(google):
    _connect(google)
    auth.disconnect()
    assert google.revoked == ["rt-SECRET-123"] and not auth.status()["connected"]


# ---------------------------------------------------------------------- export

@pytest.fixture
def deck(tmp_path):
    pres = sample_presentation()
    for s in pres.slides:
        s.brief_explanation = f"Giải thích ngắn cho {s.title}."
        s.reading_notes.detailed_explanation = f"Đoạn một về {s.title}.\n\nĐoạn hai."
        s.reading_notes.key_takeaway = "Điều cần nhớ."
    out = tmp_path / "deck.pptx"
    build_pptx(pres, out, "professional", [], layout_mode="reading")
    return pres, out


def test_export_converts_validates_and_repairs_missing_notes(google, deck):
    pres, pptx = deck
    _connect(google)
    google.drop_notes = {1, 4}  # conversion lost the notes of two slides
    meta = gexport.export_presentation(pptx, pres, "a" * 32, expected_pictures=0, api_factory=_api(google))
    fid = meta["presentation_id"]
    assert meta["url"] == f"https://docs.google.com/presentation/d/{fid}/edit"
    upload = google.files[fid]
    assert upload["mimeType"] == "application/vnd.google-apps.presentation"
    assert upload["appProperties"]["v2s_job"] == "a" * 32
    assert meta["validation"]["errors"] == [] and meta["validation"]["slide_count"] == len(pres.slides)
    assert meta["notes"] == {"total": len(pres.slides), "verified": len(pres.slides), "fixed": 2}
    # Notes read back equal the expected narration, aligned slide by slide.
    for gs, s in zip(google.decks[fid]["slides"], pres.slides):
        assert " ".join(gs["notes"].split()) == " ".join(speaker_notes_text(s, "vi").split())
    assert [m["slide_id"] for m in meta["slides"]] == [s.slide_id for s in pres.slides]


def test_export_is_idempotent_and_never_overwrites(google, deck):
    pres, pptx = deck
    _connect(google)
    first = gexport.export_presentation(pptx, pres, "b" * 32, api_factory=_api(google))
    uploads = google.count("PUT", "upload_id")
    again = gexport.export_presentation(pptx, pres, "b" * 32, previous=first, api_factory=_api(google))
    assert again["presentation_id"] == first["presentation_id"] and again["reused"]
    # A crash after upload (no metadata saved): the appProperties search finds it instead of uploading again.
    crash = gexport.export_presentation(pptx, pres, "b" * 32, previous=None, api_factory=_api(google))
    assert crash["presentation_id"] == first["presentation_id"]
    assert google.count("PUT", "upload_id") == uploads
    copy = gexport.export_presentation(pptx, pres, "b" * 32, previous=first, new_copy=True, api_factory=_api(google))
    assert copy["presentation_id"] != first["presentation_id"] and len(google.files) == 2


def test_rate_limits_are_retried(google, deck):
    pres, pptx = deck
    _connect(google)
    google.fail_queue = {"slides.googleapis.com/v1/presentations/": [429, 503]}
    meta = gexport.export_presentation(pptx, pres, "c" * 32, api_factory=_api(google))
    assert meta["notes"]["verified"] == len(pres.slides)


def test_disabled_api_gives_actionable_message(google, deck):
    pres, pptx = deck
    _connect(google)
    google.disabled_api = True
    with pytest.raises(GoogleError, match="bật"):
        gexport.export_presentation(pptx, pres, "d" * 32, api_factory=_api(google))


def test_validation_flags_wrong_slide_count(google, deck):
    pres, pptx = deck
    _connect(google)
    meta = gexport.export_presentation(pptx, pres, "e" * 32, api_factory=_api(google))
    doc = google.doc(meta["presentation_id"])
    doc["slides"] = doc["slides"][:-1]
    assert gexport.validate(doc, pres, None)["errors"]


def test_check_sync_detects_manual_changes(google, deck):
    pres, pptx = deck
    _connect(google)
    meta = gexport.export_presentation(pptx, pres, "f" * 32, api_factory=_api(google))
    slides = google.decks[meta["presentation_id"]]["slides"]
    assert not gexport.check_sync(meta, api_factory=_api(google))["reordered"]
    slides[0], slides[1] = slides[1], slides[0]
    del slides[3]
    res = gexport.check_sync(meta, api_factory=_api(google))
    assert res["reordered"] and res["missing"] == [pres.slides[3].slide_id]
    google.files[meta["presentation_id"]]["trashed"] = True
    assert gexport.check_sync(meta, api_factory=_api(google))["exists"] is False


def test_thumbnail_is_cached_without_exposing_url(google, deck, tmp_path):
    pres, pptx = deck
    _connect(google)
    meta = gexport.export_presentation(pptx, pres, "9" * 32, api_factory=_api(google))
    out = gexport.fetch_thumbnail(meta, 0, tmp_path / "t.png", api_factory=_api(google))
    assert out.read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------------- API + pipeline

@pytest.fixture
def api_client(google, monkeypatch):
    def fake_transcribe(audio, language=None, progress=None):
        segs = [Segment(i * 2.0, i * 2.0 + 2, f"Câu thử nghiệm số {i} về quản lý thời gian hiệu quả.") for i in range(8)]
        return Transcript(segs, "vi", 16.0)

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    with TestClient(main.app) as c:
        yield c


def _wait(client, job_id, cond=lambda j: j["status"] in ("done", "failed", "cancelled"), timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if cond(job):
            return job
        time.sleep(0.3)
    raise AssertionError("timeout")


def _upload(client, mp4, **extra):
    with open(mp4, "rb") as fh:
        return client.post("/api/jobs", files={"file": ("v.mp4", fh, "video/mp4")},
                           data={"language": "vi", "slide_count": "6", **extra})


def test_status_and_connect_endpoints(api_client, google):
    st = api_client.get("/api/google/status").json()
    assert st["configured"] and not st["connected"] and "token" not in json.dumps(st).lower().replace("_", "")
    # A Desktop client on a non-loopback host (Modal): manual flow through an unused loopback port.
    r = api_client.post("/api/google/connect")
    assert r.status_code == 200 and r.json()["manual"] is True
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A53682%2F" in r.json()["auth_url"]
    local = {"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"}
    r = api_client.post("/api/google/connect", headers=local)
    assert r.status_code == 200, r.text
    assert "code_challenge=" in r.json()["auth_url"] and r.json()["manual"] is False
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fapi%2Fgoogle%2Foauth%2Fcallback" in r.json()["auth_url"]
    assert api_client.post("/api/google/connect", headers={"Origin": "https://evil.example"}).status_code == 403
    bad = api_client.get("/api/google/oauth/callback", params={"state": "nope", "code": "x"})
    assert bad.status_code == 400 and "Kết nối lại" in bad.text


def test_google_option_requires_connection(api_client, sample_mp4):
    r = _upload(api_client, sample_mp4, google_slides="true")
    assert r.status_code == 409 and "Google" in r.json()["detail"]


def test_job_creates_google_slides_and_reading_data(api_client, google, sample_mp4):
    _connect(google)
    google.drop_notes = {0}
    job_id = _upload(api_client, sample_mp4, google_slides="true", layout_mode="reading").json()["job_id"]
    job = _wait(api_client, job_id)
    assert job["status"] == "done", job
    assert "exporting_google" in job["stage_times"]
    g = job["google"]
    assert g["state"] == "done" and g["url"].startswith("https://docs.google.com/presentation/d/")
    assert g["notes"]["verified"] == g["notes"]["total"] == job["result"]["slide_count"]
    assert library.get(job_id)["google"]["presentation_id"]

    data = api_client.get(f"/api/jobs/{job_id}/reading").json()
    assert data["slide_count"] == len(data["slides"]) == job["result"]["slide_count"]
    assert data["google_url"] == g["url"]
    for s in data["slides"]:
        rn = s["reading_notes"]
        assert s["title"] and (rn["detailed_explanation"] or rn["summary"])  # every slide is explained

    # Re-export without changes reuses the same presentation; a new copy is separate.
    assert api_client.post(f"/api/jobs/{job_id}/google", json={}).status_code == 202
    job = _wait(api_client, job_id, lambda j: (j["google"] or {}).get("state") in ("done", "failed"))
    assert job["google"]["url"] == g["url"]
    assert len(google.files) == 1

    check = api_client.post(f"/api/jobs/{job_id}/google/check").json()
    assert check["exists"] and check["in_sync"]
    thumb = api_client.get(f"/api/jobs/{job_id}/google/thumbnail/1.png")
    assert thumb.status_code == 200 and thumb.content.startswith(b"\x89PNG")


def test_google_failure_keeps_pptx(api_client, google, sample_mp4):
    _connect(google)
    google.disabled_api = True
    job = _wait(api_client, _upload(api_client, sample_mp4, google_slides="true").json()["job_id"])
    assert job["status"] == "done" and job["google"]["state"] == "failed"
    assert "Google Slides" in " ".join(job["result"]["notices"])
    assert api_client.get(f"/api/jobs/{job['id']}/download").status_code == 200
    # Fixed configuration -> retry from the result screen succeeds.
    google.disabled_api = False
    assert api_client.post(f"/api/jobs/{job['id']}/google", json={}).status_code == 202
    job = _wait(api_client, job["id"], lambda j: (j["google"] or {}).get("state") in ("done",))
    assert job["google"]["url"]


def test_revoked_access_is_reported_on_export(api_client, google, sample_mp4):
    _connect(google)
    tok = auth.load_token()
    tok["expires_at"] = 0
    auth._save_token(tok)
    google.refresh_error = "invalid_grant"
    job = _wait(api_client, _upload(api_client, sample_mp4, google_slides="true").json()["job_id"])
    assert job["status"] == "done" and job["google"]["state"] == "failed" and job["google"]["code"] == "revoked"
    assert not api_client.get("/api/google/status").json()["connected"]
