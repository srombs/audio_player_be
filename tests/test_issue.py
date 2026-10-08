import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

from app.models.issue import Issue
from app.newyorker.archive import read_index, sync_issues
from app.newyorker.issue import LATEST_URL, sync_issue
from app.newyorker.parser import ParseError, article_url, parse_issue_articles, parse_issue_identity
from app.newyorker.errors import SessionAccessDenied

HTML = (Path(__file__).parent / "fixtures" / "issue_toc.html").read_text()
ISSUE = Issue(issue_date=date(2026, 10, 12), label="October 12, 2026", url="https://www.newyorker.com/magazine/2026/10/12")


class IssueTests(unittest.IsolatedAsyncioTestCase):
    def test_cards_extract_metadata_and_ignore_navigation_images_and_foreign_links(self):
        entries = parse_issue_articles(HTML, ISSUE)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].title, "An Example & a Story")
        self.assertEqual(entries[0].section, "Reporting & Essays")
        self.assertEqual(entries[0].rubric, "Personal History")
        self.assertEqual(entries[0].byline, "By Example Author")
        self.assertEqual(entries[0].description, "A synthetic description.")
        self.assertIsNotNone(entries[0].image_url)
        self.assertTrue(entries[0].audio_available)
        self.assertEqual(entries[1].byline, "By Another Author and Second Author")
        self.assertIsNone(entries[1].audio_available)
        self.assertEqual(entries[2].section, "Poems")
        self.assertNotIn("SECRET", json.dumps([item.model_dump() for item in entries]))

    def test_alias_identity_and_combined_issue_dates(self):
        self.assertEqual(parse_issue_identity(HTML, LATEST_URL), ISSUE)
        combined = parse_issue_identity('<title>The New Yorker August 24 &amp; 31, 2026 | The New Yorker</title>', LATEST_URL)
        self.assertEqual(combined.issue_date, date(2026, 8, 24))
        cross_year = parse_issue_identity('<title>The New Yorker December 29, 2025 &amp; January 5, 2026 | The New Yorker</title>', LATEST_URL)
        self.assertEqual(cross_year.issue_date, date(2025, 12, 29))

    def test_mismatched_dates_login_pages_and_unsupported_layouts_fail_explicitly(self):
        for html in ('<title>Sign In</title>', '<title>The New Yorker October 5, 2026 | The New Yorker</title>',
                     '<title>The New Yorker October 12, 2026 | The New Yorker</title><nav><a href="/news/comment/promo">Only navigation</a></nav>'):
            with self.subTest(html=html), self.assertRaises(ParseError):
                parse_issue_articles(html, ISSUE)

    def test_article_url_checks_host_and_editorial_path_and_strips_queries(self):
        self.assertEqual(article_url('/news/comment/story?token=SECRET#anchor', ISSUE.url), 'https://www.newyorker.com/news/comment/story')
        for path in ('/gallery/example-cartoons', '/puzzles-and-games-dept/crossword/2026/10/12'):
            self.assertEqual(article_url(path, ISSUE.url), 'https://www.newyorker.com' + path)
        for value in ('https://evil.test/news/comment/story', 'http://www.newyorker.com/news/comment/story',
                      'https://secret@www.newyorker.com/news/comment/story', '/magazine/2026/10/12',
                      '/account/profile', '/tag/example', '/contributors/author'):
            with self.subTest(value=value):
                self.assertIsNone(article_url(value, ISSUE.url))

    async def test_sync_latest_saves_article_list_and_private_capture_and_updates_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'private'
            service = AsyncMock()
            service.get_article.return_value = HTML
            result = await sync_issue(service, article_directory=root/'articles', capture_directory=root/'pages', index_path=root/'issues.json')
            service.get_article.assert_awaited_once_with(LATEST_URL)
            self.assertEqual(len(result.articles), 3)
            output = json.loads((root/'articles'/'2026-10-12.json').read_text())
            self.assertNotIn('SECRET', json.dumps(output))
            self.assertTrue((root/'pages'/'latest-page-private.json').exists())
            self.assertEqual(read_index(root/'issues.json').issues[0], ISSUE)
            # A subsequent changed page must preserve the successful parsed list.
            before = (root/'articles'/'2026-10-12.json').read_text()
            service.get_article.return_value = '<title>Unexpected layout</title>'
            with self.assertRaises(ParseError):
                await sync_issue(service, article_directory=root/'articles', capture_directory=root/'pages', index_path=root/'issues.json')
            self.assertEqual((root/'articles'/'2026-10-12.json').read_text(), before)

    async def test_auth_failure_stops_before_capture_or_article_file_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'private'
            service = AsyncMock()
            service.get_article.side_effect = SessionAccessDenied('reconnect_required', 'reconnect')
            with self.assertRaises(SessionAccessDenied):
                await sync_issue(service, article_directory=root/'articles', capture_directory=root/'pages', index_path=root/'issues.json')
            self.assertFalse(root.exists())

    async def test_archive_alias_hero_includes_the_current_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            service = AsyncMock()
            service.get_article.side_effect = ['<a href="/magazine">Explore the issue</a><a href="/magazine/2026/10/05">October 5, 2026</a>', HTML]
            result = await sync_issues(service, Path(directory)/'private'/'issues.json')
            self.assertEqual(result.issues[0], ISSUE)
            self.assertEqual(len(result.issues), 2)
            self.assertEqual(service.get_article.await_args_list[1].args[0], LATEST_URL)
