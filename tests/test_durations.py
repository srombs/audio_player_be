import asyncio
import base64
import importlib
import json
import os
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.models.issue import Issue, IssueIndex
from app.newyorker.archive import write_index
from app.newyorker.audio_cache import cache_path
from app.newyorker.http_auth import Probe, VerificationSpec
from app.newyorker.session import save_session

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
ISSUE = Issue(issue_date=date(2026, 10, 12), label="October 12, 2026",
              url="https://www.newyorker.com/magazine/2026/10/12")
URL = ISSUE.url + "/example-story"


def article_page(url, related):
    state = {"transformed": {"head.canonicalUrl": url,
                              "article": {"relatedAudio": related, "body": "PRIVATE_BODY"},
                              "recommendations": [{"relatedAudio": {"durationInMs": 999999}}]}}
    return "<script>window.__PRELOADED_STATE__=" + json.dumps(state) + ";</script>"


@pytest.fixture
def duration_env(tmp_path, monkeypatch):
    root = tmp_path / "private"
    settings = Settings(issue_index_path=root/"issues.json", issue_articles_path=root/"articles",
                        session_state_path=root/"session.json", http_verification_path=root/"probes.json",
                        audio_cache_path=root/"audio", issue_page_capture_path=root/"pages")
    for name in ("issue_index_path", "issue_articles_path", "session_state_path", "http_verification_path",
                 "audio_cache_path", "issue_page_capture_path"):
        monkeypatch.setenv("NY_AUDIO_" + name.upper(), str(getattr(settings, name)))
    get_settings.cache_clear()
    email = "synthetic@example.test"
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    save_session(settings.session_state_path, {
        "cookies": [{"name": "CN_token_id", "value": f"e30.{claims}.synthetic", "domain": ".newyorker.com",
                     "path": "/", "expires": -1, "secure": True}], "origins": []})
    spec = VerificationSpec(Probe("https://www.newyorker.com/api/identity", "/email"),
                            Probe("https://www.newyorker.com/api/subscription", "/isActiveSubscriber"))
    save_session(settings.http_verification_path, {"version": 1, **asdict(spec)})
    write_index(settings.issue_index_path, IssueIndex(updated_at=NOW, issues=[ISSUE]))
    saved = {"version": 1, "issue": ISSUE.model_dump(mode="json"), "fetched_at": NOW.isoformat(),
             "articles": [{"title": "Example story", "url": URL, "section": "Fiction", "byline": "By Example"}]}
    path = settings.issue_articles_path / "2026-10-12.json"
    save_session(path, saved)
    pages = {URL: article_page(URL, {"durationInMs": 1440000})}
    requests = []

    def handler(request):
        requests.append(str(request.url))
        signed_in = "CN_token_id=" in request.headers.get("cookie", "")
        if str(request.url) == "https://www.newyorker.com/api/identity":
            return httpx.Response(200, json={"email": email} if signed_in else {})
        if str(request.url) == "https://www.newyorker.com/api/subscription":
            return httpx.Response(200, json={"isActiveSubscriber": signed_in})
        assert signed_in, "Article pages require the verified subscriber session"
        assert str(request.url) in pages, "Unexpected article, table-of-contents, or media request"
        page = pages[str(request.url)]
        if callable(page):
            page = page()
        if isinstance(page, BaseException):
            raise page
        return page if isinstance(page, httpx.Response) else httpx.Response(200, text=page)

    original_init = httpx.AsyncClient.__init__

    def offline_init(client, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline_init)
    yield settings, path, saved, pages, requests
    get_settings.cache_clear()


def populate(*args):
    command = importlib.import_module("app.newyorker.durations")
    return command.main(["populate", *args])


@pytest.mark.parametrize("selection", [["--latest"], ["--url", ISSUE.url]])
def test_enriched_duration_survives_issue_sync_and_is_returned_by_browsing(duration_env, capsys, selection):
    settings, _, _, pages, requests = duration_env
    assert populate("--date", "2026-10-12") == 0
    capsys.readouterr()
    html = (Path(__file__).parent / "fixtures" / "issue_toc.html").read_text()
    html = (html.replace("synthetic-story", "example-story")
            .replace("<button>Listen</button>", "")
            .replace("An Example &amp; a Story", "Updated headline")
            .replace("By Example Author", "By Updated Author"))
    target = "https://www.newyorker.com/magazine" if selection == ["--latest"] else ISSUE.url
    pages[target] = html
    requests.clear()

    command = importlib.import_module("app.newyorker.issue")
    assert command.main(["sync", *selection]) == 0
    capsys.readouterr()
    with TestClient(app) as client:
        response = client.get("/issues/2026-10-12/articles")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 1
    assert data["fetched_at"] != "2026-11-11T00:00:00Z"
    assert len(data["articles"]) == 3
    article = data["articles"][0]
    assert article["url"] == URL
    assert article["title"] == "Updated headline"
    assert article["byline"] == "By Updated Author"
    assert article["section"] == "Reporting & Essays"
    assert (article["audio_available"], article["duration_ms"]) == (True, 1440000)
    assert all(a["duration_ms"] is None for a in data["articles"][1:])
    assert [url for url in requests if "/api/" not in url] == [target]
    assert not settings.audio_cache_path.exists()


def test_sync_preserves_confirmed_absence_but_refreshes_hints_order_and_membership(duration_env, capsys):
    _, path, saved, pages, requests = duration_env
    no_audio, unknown, old_hint, removed, added = [ISSUE.url + "/" + slug for slug in (
        "no-audio", "unknown", "old-hint", "removed", "added")]
    saved["articles"] += [
        {"title": "No audio", "url": no_audio},
        {"title": "Unknown", "url": unknown},
        {"title": "Old Listen hint", "url": old_hint, "audio_available": True},
        {"title": "Same headline", "url": removed}]
    save_session(path, saved)
    pages[no_audio] = article_page(no_audio, None)
    pages[unknown] = article_page(unknown, {})
    pages[old_hint] = article_page(old_hint, {})
    pages[removed] = article_page(removed, {"durationInMs": 60000})
    assert populate("--date", "2026-10-12") == 2
    report = json.loads(capsys.readouterr().err)
    assert (report["narrated"], report["non_narrated"], report["unresolved"]) == (2, 1, 2)
    pages[ISSUE.url] = f"""
        <title>The New Yorker October 12, 2026 | The New Yorker</title>
        <main><h2>Updated section</h2>
        <article><a href="{no_audio}"><h3>Updated no audio</h3></a><button>Listen</button></article>
        <article><a href="{added}"><h3>Same headline</h3></a><button>Listen</button></article>
        <article><a href="{unknown}"><h3>Still unknown</h3></a></article>
        <article><a href="{old_hint}"><h3>No longer badged</h3></a></article>
        <article><a href="https://newyorker.com:443/magazine/2026/10/12/example-story/?source=test#top">
        <h3>Updated narrated story</h3></a></article></main>
    """
    requests.clear()

    command = importlib.import_module("app.newyorker.issue")
    assert command.main(["sync", "--url", ISSUE.url]) == 0
    capsys.readouterr()
    with TestClient(app) as client:
        response = client.get("/issues/2026-10-12/articles")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 1
    articles = data["articles"]
    assert [a["url"] for a in articles] == [no_audio, added, unknown, old_hint, URL]
    assert [a["title"] for a in articles] == [
        "Updated no audio", "Same headline", "Still unknown", "No longer badged", "Updated narrated story"]
    assert all(a["section"] == "Updated section" for a in articles)
    assert [(a["audio_available"], a["duration_ms"]) for a in articles] == [
        (False, None), (True, None), (None, None), (None, None), (True, 1440000)]
    assert [url for url in requests if "/api/" not in url] == [ISSUE.url]


@pytest.mark.parametrize("failure", ["upstream", "rejected", "layout", "wrong_issue", "write"])
def test_failed_issue_refresh_preserves_enriched_list_and_api_output(duration_env, capsys, monkeypatch, failure):
    settings, path, saved, pages, requests = duration_env
    no_audio = ISSUE.url + "/no-audio"
    saved["articles"].append({"title": "No audio", "url": no_audio})
    save_session(path, saved)
    pages[no_audio] = article_page(no_audio, None)
    assert populate("--date", "2026-10-12") == 0
    capsys.readouterr()
    with TestClient(app) as client:
        before = client.get("/issues/2026-10-12/articles").json()
    list_before = path.read_bytes()
    index_before = settings.issue_index_path.read_bytes()
    if failure in ("upstream", "rejected"):
        pages[ISSUE.url] = httpx.Response(500 if failure == "upstream" else 401, text="PRIVATE_FAILURE")
    else:
        html = (Path(__file__).parent / "fixtures" / "issue_toc.html").read_text()
        if failure == "layout":
            html = "<title>The New Yorker October 12, 2026 | The New Yorker</title><main>Unknown layout</main>"
        elif failure == "wrong_issue":
            html = html.replace("October 12, 2026", "October 5, 2026")
        pages[ISSUE.url] = html
    if failure == "write":
        replace = os.replace

        def fail_list_write(source, destination):
            if destination == path:
                raise OSError("PRIVATE_FILESYSTEM_FAILURE")
            replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_list_write)
    requests.clear()

    command = importlib.import_module("app.newyorker.issue")
    assert command.main(["sync", "--url", ISSUE.url]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "PRIVATE" not in captured.err
    assert path.read_bytes() == list_before
    assert settings.issue_index_path.read_bytes() == index_before
    with TestClient(app) as client:
        response = client.get("/issues/2026-10-12/articles")
    assert response.status_code == 200
    assert response.json() == before
    assert [url for url in requests if "/api/" not in url] == [ISSUE.url]


def test_command_populates_legacy_saved_issue_and_api_returns_duration_without_playback(duration_env, capsys):
    settings, path, saved, _, requests = duration_env
    index_before = settings.issue_index_path.read_bytes()
    assert populate("--date", "2026-10-12") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["issue_date"] == "2026-10-12"
    assert report["total_articles"] == 1
    assert report["narrated"] == 1
    assert report["non_narrated"] == 0
    assert report["duration_source"] == "relatedAudio.durationInMs"
    assert report["media_requests"] == 0
    assert settings.issue_index_path.read_bytes() == index_before
    assert not settings.audio_cache_path.exists()
    assert not settings.issue_page_capture_path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    with TestClient(app) as client:
        response = client.get("/issues/2026-10-12/articles")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == 1
        assert data["fetched_at"] == "2026-11-11T00:00:00Z"
        assert data["articles"][0]["duration_ms"] == 1440000
        assert data["articles"][0]["audio_available"] is True
        for key, value in saved["articles"][0].items():
            assert data["articles"][0][key] == value
        assert "PRIVATE_BODY" not in response.text
    assert requests.count(URL) == 1


def test_every_run_refreshes_all_articles_in_order_including_previous_results_and_caches(duration_env, capsys):
    settings, path, saved, pages, requests = duration_env
    previously_narrated = ISSUE.url + "/previously-narrated"
    previously_unnarrated = ISSUE.url + "/previously-unnarrated"
    saved["articles"] += [
        {"title": "Previously narrated", "url": previously_narrated, "audio_available": True, "duration_ms": 120000},
        {"title": "Previously unnarrated", "url": previously_unnarrated, "audio_available": False}]
    save_session(path, saved)
    pages[previously_narrated] = article_page(previously_narrated, None)
    pages[previously_unnarrated] = article_page(previously_unnarrated, {"durationInMs": 240000})
    # A real persistent playback cache must never stand in for fresh page metadata.
    save_session(cache_path(settings.audio_cache_path, URL), {
        "article": {"id": "example", "title": "Example story", "url": URL}, "audio": None})
    other_issue = settings.issue_articles_path / "2026-10-05.json"
    other_issue.write_text("ANOTHER_ISSUE_MUST_NOT_CHANGE")

    assert populate("--date", "2026-10-12") == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["total_articles"], report["narrated"], report["non_narrated"]) == (3, 2, 1)
    with TestClient(app) as client:
        data = client.get("/issues/2026-10-12/articles").json()
    assert [a["url"] for a in data["articles"]] == [URL, previously_narrated, previously_unnarrated]
    assert [(a["audio_available"], a["duration_ms"]) for a in data["articles"]] == [
        (True, 1440000), (False, None), (True, 240000)]

    pages[URL] = article_page(URL, {"durationInMs": 1500000})
    pages[previously_narrated] = article_page(previously_narrated, {"durationInMs": 60000})
    pages[previously_unnarrated] = article_page(previously_unnarrated, None)
    assert populate("--date", "2026-10-12") == 0
    capsys.readouterr()
    with TestClient(app) as client:
        data = client.get("/issues/2026-10-12/articles").json()
    assert [(a["audio_available"], a["duration_ms"]) for a in data["articles"]] == [
        (True, 1500000), (True, 60000), (False, None)]
    article_requests = [url for url in requests if "/api/" not in url]
    assert article_requests == [URL, previously_narrated, previously_unnarrated] * 2
    assert other_issue.read_text() == "ANOTHER_ISSUE_MUST_NOT_CHANGE"


@pytest.mark.parametrize("failure", [httpx.Response(500, text="PRIVATE_UPSTREAM_FAILURE"),
                                   httpx.ReadTimeout("PRIVATE_CONNECTION_FAILURE")])
def test_mixed_sweep_preserves_failed_values_and_retries_every_article(duration_env, capsys, failure):
    _, path, saved, pages, requests = duration_env
    urls = [URL] + [ISSUE.url + suffix for suffix in ("/old-duration", "/no-audio", "/unknown", "/fresh-no-audio")]
    saved["articles"] += [
        {"title": "Old duration", "url": urls[1], "audio_available": True, "duration_ms": 120000},
        {"title": "No audio", "url": urls[2], "audio_available": False},
        {"title": "Unknown", "url": urls[3]},
        {"title": "Fresh no audio", "url": urls[4], "audio_available": True, "duration_ms": 60000}]
    save_session(path, saved)
    pages[urls[1]] = failure
    pages[urls[2]] = article_page(urls[2], {"durationInMs": False})
    pages[urls[3]] = article_page(urls[3], {})
    pages[urls[4]] = article_page(urls[4], None)

    assert populate("--date", "2026-10-12") == 2
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert not captured.out
    assert (report["total_articles"], report["attempted"], report["narrated"], report["non_narrated"],
            report["unresolved"], report["not_attempted"]) == (5, 5, 1, 1, 3, 0)
    assert report["duration_source"] == "relatedAudio.durationInMs"
    assert report["media_requests"] == 0
    assert "PRIVATE" not in captured.err
    failures = [item for item in report["articles"] if item["outcome"] == "unresolved"]
    assert [item["url"] for item in failures] == urls[1:4]
    assert [item["preserved_previous"] for item in failures] == [True, True, False]
    assert failures[0]["previous_metadata"] == {"audio_available": True, "duration_ms": 120000}
    assert failures[1]["previous_metadata"] == {"audio_available": False, "duration_ms": None}
    with TestClient(app) as client:
        data = client.get("/issues/2026-10-12/articles").json()
    assert [a["url"] for a in data["articles"]] == urls
    assert [(a["audio_available"], a["duration_ms"]) for a in data["articles"]] == [
        (True, 1440000), (True, 120000), (False, None), (None, None), (False, None)]

    for url in urls:
        pages[url] = article_page(url, {"durationInMs": 240000})
    assert populate("--date", "2026-10-12") == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["attempted"], report["narrated"], report["unresolved"], report["not_attempted"]) == (5, 5, 0, 0)
    assert [url for url in requests if "/api/" not in url] == urls * 2


def test_access_rejection_preserves_visible_progress_and_stops_requests(duration_env, capsys):
    _, path, saved, pages, requests = duration_env
    rejected = ISSUE.url + "/rejected"
    untouched = ISSUE.url + "/not-attempted"
    saved["articles"] += [
        {"title": "Rejected", "url": rejected, "audio_available": True, "duration_ms": 120000},
        {"title": "Not attempted", "url": untouched, "audio_available": False}]
    save_session(path, saved)

    def reject_after_checking_progress():
        with TestClient(app) as client:
            articles = client.get("/issues/2026-10-12/articles").json()["articles"]
        assert articles[0]["duration_ms"] == 1440000
        return httpx.Response(401, text="PRIVATE_REJECTION")

    pages[rejected] = reject_after_checking_progress
    assert populate("--date", "2026-10-12") == 2
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert not captured.out
    assert report["error"] == "newyorker_session_reconnect_required"
    assert report["action"] == "reconnect"
    assert (report["total_articles"], report["attempted"], report["narrated"], report["non_narrated"],
            report["unresolved"], report["not_attempted"]) == (3, 2, 1, 0, 1, 1)
    assert [item["outcome"] for item in report["articles"]] == ["narrated", "unresolved", "not_attempted"]
    assert report["articles"][1]["previous_metadata"]["duration_ms"] == 120000
    assert report["articles"][2]["previous_metadata"]["audio_available"] is False
    assert "PRIVATE" not in captured.err
    assert [url for url in requests if "/api/" not in url] == [URL, rejected]
    with TestClient(app) as client:
        articles = client.get("/issues/2026-10-12/articles").json()["articles"]
    assert [a["duration_ms"] for a in articles] == [1440000, 120000, None]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_interrupted_sweep_reports_and_keeps_completed_progress(duration_env, capsys, interruption):
    _, path, saved, pages, requests = duration_env
    interrupted = ISSUE.url + "/interrupted"
    untouched = ISSUE.url + "/not-attempted"
    saved["articles"] += [
        {"title": "Interrupted", "url": interrupted, "audio_available": True, "duration_ms": 120000},
        {"title": "Not attempted", "url": untouched}]
    save_session(path, saved)
    pages[interrupted] = interruption()

    assert populate("--date", "2026-10-12") == 130
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert not captured.out
    assert report["error"] == "duration_population_interrupted"
    assert (report["total_articles"], report["attempted"], report["narrated"], report["unresolved"],
            report["not_attempted"]) == (3, 2, 1, 1, 1)
    assert [url for url in requests if "/api/" not in url] == [URL, interrupted]
    with TestClient(app) as client:
        articles = client.get("/issues/2026-10-12/articles").json()["articles"]
    assert [a["duration_ms"] for a in articles] == [1440000, 120000, None]


def test_failed_progress_write_preserves_last_saved_list_and_stops(duration_env, capsys, monkeypatch):
    _, path, saved, pages, requests = duration_env
    failed = ISSUE.url + "/write-failed"
    untouched = ISSUE.url + "/not-attempted"
    saved["articles"] += [
        {"title": "Write failed", "url": failed, "audio_available": False},
        {"title": "Not attempted", "url": untouched}]
    save_session(path, saved)
    pages[failed] = article_page(failed, {"durationInMs": 240000})
    replace = os.replace
    last_saved = []

    def fail_second_article_write(source, destination):
        if destination == path:
            if last_saved:
                raise OSError("PRIVATE_FILESYSTEM_FAILURE")
            replace(source, destination)
            last_saved.append(path.read_bytes())
        else:
            replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second_article_write)
    assert populate("--date", "2026-10-12") == 2
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert not captured.out
    assert (report["total_articles"], report["attempted"], report["narrated"], report["unresolved"],
            report["not_attempted"]) == (3, 2, 1, 1, 1)
    assert "save" in report["articles"][1]["reason"]
    assert "PRIVATE" not in captured.err
    assert path.read_bytes() == last_saved[0]
    assert not list(path.parent.glob("*.tmp"))
    assert [url for url in requests if "/api/" not in url] == [URL, failed]
    with TestClient(app) as client:
        articles = client.get("/issues/2026-10-12/articles").json()["articles"]
    assert [(a["audio_available"], a["duration_ms"]) for a in articles] == [
        (True, 1440000), (False, None), (None, None)]


@pytest.mark.parametrize("invalid", [True, False, "1440000", 1.5, 1440000.0, 0, -1, None])
def test_invalid_publisher_durations_are_unresolved_and_never_confirm_no_narration(duration_env, capsys, invalid):
    _, path, _, pages, _ = duration_env
    pages[URL] = article_page(URL, {"durationInMs": invalid})
    before = path.read_bytes()
    assert populate("--date", "2026-10-12") != 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "unresolved" in captured.err
    assert path.read_bytes() == before
    with TestClient(app) as client:
        article = client.get("/issues/2026-10-12/articles").json()["articles"][0]
    assert article["audio_available"] is None
    assert article.get("duration_ms") is None


@pytest.mark.parametrize("problem", ["missing_field", "malformed", "missing_duration", "wrong_primary", "only_recommendations"])
def test_unresolved_or_unrelated_metadata_never_uses_recommendations(duration_env, capsys, problem):
    _, path, _, pages, _ = duration_env
    if problem == "missing_field":
        pages[URL] = '<script>window.__PRELOADED_STATE__=' + json.dumps({
            "transformed": {"head.canonicalUrl": URL, "article": {},
                            "recommendations": [{"relatedAudio": None}]}}) + ';</script>'
    elif problem == "malformed":
        pages[URL] = article_page(URL, [])
    elif problem == "missing_duration":
        pages[URL] = article_page(URL, {})
    elif problem == "wrong_primary":
        pages[URL] = article_page(ISSUE.url + "/a-different-story", None)
    else:
        pages[URL] = '<script>window.__PRELOADED_STATE__=' + json.dumps({
            "transformed": {"head.canonicalUrl": URL, "recommendations": [
                {"relatedAudio": {"durationInMs": 1440000}}]}}) + ';</script>'
    before = path.read_bytes()
    assert populate("--date", "2026-10-12") != 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "duration_population_failed" in captured.err
    assert path.read_bytes() == before


@pytest.mark.parametrize("problem", ["missing_index", "invalid_index", "unknown_issue", "missing_list", "invalid_json",
                                     "wrong_issue", "unsafe_article", "empty_list", "symlink", "contradictory_duration"])
def test_invalid_local_inputs_fail_before_requests_or_data_mutation(duration_env, capsys, tmp_path, problem):
    settings, path, saved, _, requests = duration_env
    if problem == "missing_index":
        settings.issue_index_path.unlink()
    elif problem == "invalid_index":
        settings.issue_index_path.write_text("PRIVATE_INVALID_INDEX")
    elif problem == "unknown_issue":
        write_index(settings.issue_index_path, IssueIndex(updated_at=NOW, issues=[]))
    elif problem == "missing_list":
        path.unlink()
    elif problem == "invalid_json":
        path.write_text("PRIVATE_INVALID_LIST")
    elif problem == "symlink":
        path.unlink()
        target = tmp_path / "target.json"
        target.write_text("PRIVATE_TARGET")
        path.symlink_to(target)
    else:
        if problem == "wrong_issue":
            saved["issue"]["issue_date"] = "2026-10-05"
        elif problem == "unsafe_article":
            saved["articles"][0]["url"] = "https://evil.test/secret"
        elif problem == "empty_list":
            saved["articles"] = []
        else:
            saved["articles"][0].update(audio_available=False, duration_ms=120000)
        save_session(path, saved)
    before = {p: p.read_bytes() for p in settings.issue_index_path.parent.rglob("*") if p.is_file()}
    assert populate("--date", "2026-10-12") != 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "PRIVATE" not in captured.err
    assert "sync" in captured.err
    assert requests == []
    after = {p: p.read_bytes() for p in settings.issue_index_path.parent.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("args", [[], ["--date", "not-a-date"], ["--date", "2026-10-12", "2026-10-05"]])
def test_command_requires_one_explicit_issue_date(duration_env, args):
    _, _, _, _, requests = duration_env
    with pytest.raises(SystemExit) as error:
        populate(*args)
    assert error.value.code != 0
    assert requests == []


def test_browsing_legacy_lists_and_table_of_contents_cache_misses_never_discovers_durations(duration_env):
    _, path, _, pages, requests = duration_env
    with TestClient(app) as client:
        response = client.get("/issues/2026-10-12/articles")
        assert response.status_code == 200
        assert response.json()["articles"][0].get("duration_ms") is None
        assert not any("/api/" not in url for url in requests)
        path.unlink()
        pages[ISSUE.url] = (Path(__file__).parent / "fixtures" / "issue_toc.html").read_text()
        response = client.get("/issues/2026-10-12/articles")
        assert response.status_code == 200
        assert len(response.json()["articles"]) == 3
        assert all(a.get("duration_ms") is None for a in response.json()["articles"])
    assert [url for url in requests if "/api/" not in url] == [ISSUE.url]


@pytest.mark.parametrize("problem,error", [("missing_session", "newyorker_session_missing"),
                                          ("missing_rules", "newyorker_session_unverifiable"),
                                          ("rejected_article", "newyorker_session_reconnect_required"),
                                          ("article_failure", "duration_population_failed")])
def test_command_uses_subscriber_gate_and_preserves_saved_list_on_access_or_fetch_failure(duration_env, capsys, problem, error):
    settings, path, _, pages, requests = duration_env
    if problem == "missing_session":
        settings.session_state_path.unlink()
    elif problem == "missing_rules":
        settings.http_verification_path.unlink()
    else:
        pages[URL] = httpx.Response(401 if problem == "rejected_article" else 500, text="PRIVATE_UPSTREAM_FAILURE")
    before = path.read_bytes()
    assert populate("--date", "2026-10-12") != 0
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["error"] == error
    assert "PRIVATE" not in captured.err
    assert path.read_bytes() == before
    if problem in ("missing_session", "missing_rules"):
        assert requests == []


@pytest.mark.parametrize("duration", [True, "120000", 120000.0, 0, -1])
def test_browsing_rejects_invalid_saved_duration_instead_of_returning_a_wrong_type(duration_env, duration):
    _, path, saved, _, requests = duration_env
    saved["articles"][0].update(audio_available=True, duration_ms=duration)
    save_session(path, saved)
    with TestClient(app) as client:
        response = client.get("/issues/2026-10-12/articles")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "issue_articles_unavailable"
    assert not any("/api/" not in url for url in requests)
