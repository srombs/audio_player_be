import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models.article import ArticleSummary, IssueArticles
from app.models.issue import Issue, IssueIndex
from app.newyorker.archive import write_index
from app.newyorker.errors import SessionAccessDenied, UpstreamRequestFailed
from app.newyorker.session import save_session

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
LATEST = Issue(issue_date=date(2026, 10, 12), label='October 12, 2026', url='https://www.newyorker.com/magazine/2026/10/12')
OLDER = Issue(issue_date=date(2025, 12, 29), label='December 29, 2025 & January 5, 2026', url='https://www.newyorker.com/magazine/2025/12/29')
HTML = (Path(__file__).parent / 'fixtures' / 'issue_toc.html').read_text()


@pytest.fixture
def browsing(tmp_path):
    root = tmp_path / 'private'
    settings = Settings(issue_index_path=root/'issues.json', issue_articles_path=root/'articles', issue_page_capture_path=root/'pages')
    write_index(settings.issue_index_path, IssueIndex(updated_at=NOW, issues=[OLDER, LATEST], history_cursor=OLDER.url))
    cached = IssueArticles(issue=LATEST, fetched_at=NOW, articles=[ArticleSummary(title='Example story', url=LATEST.url+'/example-story', section='Fiction', byline='By Example Author')])
    save_session(settings.issue_articles_path/'2026-10-12.json', cached.model_dump(mode='json'))
    service = AsyncMock()
    service.settings = settings
    service.status.return_value = SimpleNamespace(status='verified', action='none')
    service.get_article.return_value = HTML
    with patch('app.main.SessionService', return_value=service):
        with TestClient(app) as client:
            yield client, service, cached


def test_magazine_list_sorted_filtered_and_excludes_sync_details(browsing):
    client, service, _ = browsing
    response = client.get('/issues')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()
    assert [item['issue_date'] for item in data['issues']] == ['2026-10-12', '2025-12-29']
    assert set(data) == {'updated_at', 'history_complete', 'issues'}
    assert len(client.get('/issues?year=2026').json()['issues']) == 1
    assert client.get('/issues?year=2025').json()['issues'][0]['label'] == OLDER.label
    assert client.get('/issues?year=2024').json()['issues'] == []
    assert client.get('/issues?year=wrong').status_code == 422
    service.get_article.assert_not_awaited()


def test_saved_articles_stay_grouped_by_issue_and_need_no_upstream_fetch(browsing):
    client, service, cached = browsing
    response = client.get('/issues/2026-10-12/articles')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == cached.model_dump(mode='json')
    service.get_article.assert_not_awaited()


def test_cover_url_is_returned_in_lists_and_updates_cached_article_issue_metadata(browsing):
    client, service, _ = browsing
    cover = 'https://media.newyorker.com/photos/example/master/cover.jpg'
    updated = LATEST.model_copy(update={'cover_image_url': cover})
    write_index(service.settings.issue_index_path, IssueIndex(updated_at=NOW, issues=[updated]))
    assert client.get('/issues').json()['issues'][0]['cover_image_url'] == cover
    assert client.get('/issues/2026-10-12/articles').json()['issue']['cover_image_url'] == cover
    service.get_article.assert_not_awaited()


def test_known_issue_without_saved_articles_is_fetched_and_saved_once(browsing):
    client, service, _ = browsing
    path = service.settings.issue_articles_path/'2026-10-12.json'
    path.unlink()
    response = client.get('/issues/2026-10-12/articles')
    assert response.status_code == 200
    assert response.json()['issue']['issue_date'] == '2026-10-12'
    assert len(response.json()['articles']) == 3
    assert 'SECRET' not in response.text
    assert path.exists()
    assert client.get('/issues/2026-10-12/articles').status_code == 200
    service.get_article.assert_awaited_once_with(LATEST.url)


@pytest.mark.parametrize('status,action,http_status', [('missing','reconnect',503), ('reconnect_required','reconnect',503),
                                                     ('unverifiable','retry',503), ('not_subscribed','check_subscription',403)])
def test_lists_require_verified_subscriber_even_when_cached(browsing, status, action, http_status):
    client, service, _ = browsing
    service.status.return_value = SimpleNamespace(status=status, action=action)
    for url in ('/issues', '/issues/2026-10-12/articles'):
        response = client.get(url)
        assert response.status_code == http_status
        assert response.json()['detail'] == {'code': 'newyorker_session_'+status, 'action': action}
        assert response.headers['cache-control'] == 'no-store'
    service.get_article.assert_not_awaited()


def test_unknown_issue_and_invalid_date_do_not_fetch(browsing):
    client, service, _ = browsing
    assert client.get('/issues/2026-01-01/articles').status_code == 404
    assert client.get('/issues/not-a-date/articles').status_code == 422
    assert client.get('/issues/2026-99-01/articles').status_code == 422
    service.get_article.assert_not_awaited()


def test_missing_index_is_not_misreported_as_an_empty_list(browsing):
    client, service, _ = browsing
    service.settings.issue_index_path.unlink()
    response = client.get('/issues')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'issue_index_missing'


def test_wrong_issue_cache_and_symlink_fail_without_disclosing_file_contents(browsing, tmp_path):
    client, service, cached = browsing
    path = service.settings.issue_articles_path/'2026-10-12.json'
    data = cached.model_dump(mode='json')
    data['issue'] = OLDER.model_dump(mode='json')
    path.write_text(json.dumps(data))
    assert client.get('/issues/2026-10-12/articles').status_code == 503
    path.unlink()
    secret = tmp_path/'secret.json'
    secret.write_text('SECRET_ACCOUNT')
    path.symlink_to(secret)
    response = client.get('/issues/2026-10-12/articles')
    assert response.status_code == 503
    assert 'SECRET_ACCOUNT' not in response.text
    service.get_article.assert_not_awaited()


@pytest.mark.parametrize('error,http_status,code', [(UpstreamRequestFailed('private failure'),502,'issue_upstream_unavailable'),
                                                  (SessionAccessDenied('reconnect_required','reconnect'),503,'newyorker_session_reconnect_required')])
def test_fetch_failure_returns_safe_error_without_empty_articles(browsing, error, http_status, code):
    client, service, _ = browsing
    (service.settings.issue_articles_path/'2026-10-12.json').unlink()
    service.get_article.side_effect = error
    response = client.get('/issues/2026-10-12/articles')
    assert response.status_code == http_status
    assert response.json()['detail']['code'] == code
    assert 'private failure' not in response.text
