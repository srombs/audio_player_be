import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import Settings
from app.newyorker.audio_cache import cache_path
from app.newyorker.errors import SessionAccessDenied, UpstreamRequestFailed
from app.newyorker.media_client import MediaClient

URL = "https://www.newyorker.com/magazine/2026/10/12/example-story"
EMBED = "https://embed-audio.cnevids.com/permalink/narrated/example"
MP3 = "https://audio-resources.cnevids.com/audio/narrated/example-123.mp3"


@pytest.fixture
def playback(tmp_path):
    primary = {"id": "example-article", "headerProps": {"dangerousHed": "Example story"},
               "body": "PRIVATE_ARTICLE_BODY",
               "relatedAudio": {"name": "Example story", "audioId": "example",
                                "durationInMs": 123000, "embedUrl": EMBED}}
    service = AsyncMock()
    service.settings = Settings(audio_cache_path=tmp_path / "audio")
    service.status.return_value = SimpleNamespace(status="verified", action="none")
    calls = []

    def handler(request):
        calls.append(request)
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        if str(request.url) == EMBED:
            return httpx.Response(302, headers={"location": MP3})
        assert str(request.url) == MP3
        assert request.headers["range"] == "bytes=0-4095"
        return httpx.Response(200, headers={"content-type": "audio/mpeg"},
                              content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 4086)

    def set_page():
        state = {"transformed": {"head.canonicalUrl": URL, "article": primary}}
        service.get_article.return_value = "<script>window.__PRELOADED_STATE__=" + json.dumps(state) + ";</script>"

    set_page()
    with patch("app.main.SessionService", return_value=service), \
         patch("app.api.audio.MediaClient", side_effect=lambda: MediaClient(transport=httpx.MockTransport(handler))):
        with TestClient(app) as client:
            yield client, service, primary, set_page, calls


def test_playback_resolves_selected_article_and_returns_verified_source(playback):
    client, service, _, _, calls = playback
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["article"]["title"] == "Example story"
    assert response.json()["audio"]["audio_url"] == MP3
    assert response.json()["audio"]["duration_ms"] == 123000
    assert response.json()["audio"]["verified_at"]
    assert "PRIVATE_ARTICLE_BODY" not in response.text
    assert "embedUrl" not in response.text
    service.get_article.assert_awaited_once_with(URL)
    assert len(calls) == 2


def test_explicitly_unnarrated_article_returns_null_without_media_requests(playback):
    client, _, primary, set_page, calls = playback
    primary["relatedAudio"] = None
    set_page()
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 200
    assert response.json()["audio"] is None
    assert calls == []


@pytest.mark.parametrize("body", [{}, {"url": "http://www.newyorker.com/culture/example"},
                                  {"url": "https://127.0.0.1/private"},
                                  {"url": "https://www.newyorker.com.evil.test/culture/example"},
                                  {"url": "https://user:secret@www.newyorker.com/culture/example"},
                                  {"url": "https://www.newyorker.com/magazine/2026/10/12"},
                                  {"url": URL, "token": "unexpected"}])
def test_invalid_requests_do_not_fetch(playback, body):
    client, service, _, _, calls = playback
    assert client.post("/articles/audio", json=body).status_code == 422
    service.get_article.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("status,action,http_status", [("missing", "reconnect", 503),
                                                      ("unverifiable", "retry", 503),
                                                      ("not_subscribed", "check_subscription", 403)])
def test_session_denial_preserves_recovery_response(playback, status, action, http_status):
    client, service, _, _, calls = playback
    service.get_article.side_effect = SessionAccessDenied(status, action)
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == http_status
    assert response.json()["detail"] == {"code": "newyorker_session_" + status, "action": action}
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


def test_upstream_failure_does_not_disclose_exception(playback):
    client, service, _, _, _ = playback
    service.get_article.side_effect = UpstreamRequestFailed("PRIVATE_COOKIE_VALUE")
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "audio_upstream_unavailable"
    assert "PRIVATE_COOKIE_VALUE" not in response.text


def test_unknown_narration_metadata_is_an_error_not_no_audio(playback):
    client, _, primary, set_page, _ = playback
    del primary["relatedAudio"]
    set_page()
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "audio_resolution_failed"


def test_repeat_requests_and_restart_reuse_persistent_audio(playback):
    client, service, _, _, calls = playback
    first = client.post("/articles/audio", json={"url": URL})
    path = cache_path(service.settings.audio_cache_path, URL)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    # URL aliases and query parameters use the same canonical cache key.
    second = client.post("/articles/audio", json={"url": URL.replace("www.newyorker", "newyorker") + "?tracking=example"})
    assert second.json() == first.json()
    with TestClient(app) as restarted:
        assert restarted.post("/articles/audio", json={"url": URL}).json() == first.json()
    service.get_article.assert_awaited_once_with(URL)
    assert len(calls) == 2


def test_refresh_resolves_again_and_failed_refresh_preserves_cache(playback):
    client, service, _, _, calls = playback
    first = client.post("/articles/audio", json={"url": URL})
    refreshed = client.post("/articles/audio", json={"url": URL, "refresh": True})
    assert refreshed.status_code == 200
    assert service.get_article.await_count == 2
    assert len(calls) == 4
    service.get_article.side_effect = UpstreamRequestFailed("PRIVATE_FAILURE")
    assert client.post("/articles/audio", json={"url": URL, "refresh": True}).status_code == 502
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 200
    assert response.json() == refreshed.json()
    assert response.json()["audio"]["audio_url"] == first.json()["audio"]["audio_url"]


def test_cached_audio_still_requires_verified_subscription(playback):
    client, service, _, _, calls = playback
    assert client.post("/articles/audio", json={"url": URL}).status_code == 200
    service.status.return_value = SimpleNamespace(status="not_subscribed", action="check_subscription")
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "newyorker_session_not_subscribed"
    assert len(calls) == 2


def test_credential_rejection_during_cache_read_blocks_response(playback):
    client, service, _, _, _ = playback
    assert client.post("/articles/audio", json={"url": URL}).status_code == 200
    service.status.side_effect = [SimpleNamespace(status="verified", action="none"),
                                  SimpleNamespace(status="reconnect_required", action="reconnect")]
    assert client.post("/articles/audio", json={"url": URL}).status_code == 503


@pytest.mark.parametrize("contents", ["not json", "{}", "wrong_article", "unsafe_media"])
def test_invalid_cached_result_is_replaced_by_resolution(playback, contents):
    client, service, _, _, _ = playback
    first = client.post("/articles/audio", json={"url": URL})
    data = first.json()
    if contents == "wrong_article":
        data["article"]["url"] = URL + "-different"
        contents = json.dumps(data)
    elif contents == "unsafe_media":
        data["audio"]["audio_url"] = "https://evil.test/private"
        contents = json.dumps(data)
    cache_path(service.settings.audio_cache_path, URL).write_text(contents)
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 200
    assert response.json()["audio"]["audio_url"] == MP3
    assert service.get_article.await_count == 2


def test_symlink_cache_does_not_read_or_overwrite_target(playback, tmp_path):
    client, service, _, _, _ = playback
    path = cache_path(service.settings.audio_cache_path, URL)
    path.parent.mkdir(mode=0o700)
    private = tmp_path / "private.json"
    private.write_text("PRIVATE_DATA")
    path.symlink_to(private)
    response = client.post("/articles/audio", json={"url": URL})
    assert response.status_code == 502
    assert "PRIVATE_DATA" not in response.text
    assert private.read_text() == "PRIVATE_DATA"
    service.get_article.assert_not_awaited()


def test_explicit_no_audio_is_cached(playback):
    client, service, primary, set_page, _ = playback
    primary["relatedAudio"] = None
    set_page()
    for _ in range(2):
        response = client.post("/articles/audio", json={"url": URL})
        assert response.status_code == 200
        assert response.json()["audio"] is None
    service.get_article.assert_awaited_once_with(URL)
