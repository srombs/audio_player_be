import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.newyorker.archive import ArchiveError, issue_url, issue_cover_url, parse_archive, parse_issue_navigation, read_index, sync_issues

ARCHIVE = '''<html><a href="/magazine/2026/10/12">Explore the issue</a>
<a href="/magazine/2026/08/24"><span>August 24 &amp; 31, 2026</span></a>
<a href="https://www.newyorker.com/magazine/2026/08/24?tracking=ignored">August 24 &amp; 31, 2026</a>
<a href="https://evil.test/magazine/2026/10/19">October 19, 2026</a>
<a href="/news/example">Article</a></html>'''


def page(day, previous):
    return f'<title>The New Yorker {day} | The New Yorker</title><a href="{previous}"><span>Previous</span></a>'


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    def test_archive_covers_match_issue_links_not_nearby_images(self):
        cover = 'https://media.newyorker.com/photos/example/master/cover.jpg'
        html = '<img src="https://media.newyorker.com/photos/unrelated/master/other.jpg">' + ARCHIVE
        html += f'<a href="/magazine/2026/08/24"><picture><img src="{cover}"></picture></a>'
        html += '<a href="/magazine/2026/10/12"><img src="https://evil.test/cover.jpg"></a>'
        issues = parse_archive(html)
        self.assertEqual(issues[1].cover_image_url, cover)
        self.assertIsNone(issues[0].cover_image_url)

    def test_issue_cover_prefers_matching_portrait_over_social_crop(self):
        crop = 'https://media.newyorker.com/photos/example/16:9/cover.jpg'
        portrait = 'https://media.newyorker.com/photos/example/master/cover.jpg'
        html = f'<meta property="og:image" content="{crop}"><img src="https://media.newyorker.com/photos/unrelated/master/story.jpg"><img src="{portrait}">'
        self.assertEqual(issue_cover_url(html), portrait)
        self.assertEqual(issue_cover_url(f'<meta property="og:image" content="{crop}">'), crop)
        self.assertIsNone(issue_cover_url('<meta property="og:image" content="https://evil.test/cover.jpg">'))

    async def test_resync_preserves_cover_when_upstream_omits_it(self):
        cover = 'https://media.newyorker.com/photos/example/master/cover.jpg'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'private'/'issues.json'
            service = AsyncMock()
            service.get_article.return_value = ARCHIVE + f'<a href="/magazine/2026/08/24"><img src="{cover}"></a>'
            await sync_issues(service, path)
            service.get_article.return_value = ARCHIVE
            result = await sync_issues(service, path)
            self.assertEqual(result.issues[1].cover_image_url, cover)

    def test_archive_links_are_sorted_deduplicated_and_keep_double_issue_label(self):
        issues = parse_archive(ARCHIVE)
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0].issue_date.isoformat(), "2026-10-12")
        self.assertEqual(issues[1].label, "August 24 & 31, 2026")
        self.assertEqual(issues[1].url, "https://www.newyorker.com/magazine/2026/08/24")

    def test_issue_destinations_and_dates_are_validated(self):
        for value in ("https://evil.test/magazine/2026/10/12", "http://www.newyorker.com/magazine/2026/10/12",
                      "/magazine/2026/02/30", "https://secret@www.newyorker.com/magazine/2026/10/12",
                      "https://www.newyorker.com:8443/magazine/2026/10/12", "/magazine/2026/10/12/article"):
            with self.subTest(value=value):
                self.assertIsNone(issue_url(value))

    def test_previous_navigation_is_explicit_and_always_moves_backwards(self):
        current = "https://www.newyorker.com/magazine/2026/10/12"
        issue, previous = parse_issue_navigation(page("October 12, 2026", "/magazine/2026/10/05"), current)
        self.assertEqual(issue.label, "October 12, 2026")
        self.assertTrue(previous.endswith("2026/10/05"))
        for html in ('<title>Sign In</title>', '<title>The New Yorker October 12, 2026</title>',
                     page("October 12, 2026", "/magazine/2026/10/19"),
                     page("October 12, 2026", "https://evil.test/magazine/2026/10/05")):
            with self.subTest(html=html), self.assertRaises(ArchiveError):
                parse_issue_navigation(html, current)
        with self.assertRaises(ArchiveError):
            parse_archive('<html>Sign In</html>')

    async def test_history_checkpoint_resumes_and_preserves_progress_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "issues.json"
            service = AsyncMock()
            service.get_article.side_effect = [ARCHIVE, page("August 24 & 31, 2026", "/magazine/2026/08/17")]
            first = await sync_issues(service, path, history=True, max_pages=1, delay_seconds=0)
            self.assertFalse(first.history_complete)
            self.assertTrue(first.history_cursor.endswith("2026/08/17"))
            service.get_article.side_effect = [ARCHIVE, page("August 17, 2026", "/magazine/2026/08/10")]
            second = await sync_issues(service, path, history=True, max_pages=1, delay_seconds=0)
            self.assertEqual(len(second.issues), 3)
            self.assertTrue(second.history_cursor.endswith("2026/08/10"))
            service.get_article.side_effect = [ARCHIVE, '<title>Unexpected page</title>']
            with self.assertRaises(ArchiveError):
                await sync_issues(service, path, history=True, max_pages=1, delay_seconds=0)
            self.assertTrue(read_index(path).history_cursor.endswith("2026/08/10"))
            self.assertEqual(len(read_index(path).issues), 3)
            self.assertNotIn("cookies", json.loads(path.read_text()))

    async def test_listing_sync_only_fetches_archive_and_never_claims_full_history(self):
        with tempfile.TemporaryDirectory() as directory:
            service = AsyncMock()
            service.get_article.return_value = ARCHIVE
            result = await sync_issues(service, Path(directory) / "private" / "issues.json")
            self.assertEqual(len(result.issues), 2)
            self.assertFalse(result.history_complete)
            service.get_article.assert_awaited_once_with("https://www.newyorker.com/archive")

    async def test_invalid_cached_destinations_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "issues.json"
            service = AsyncMock()
            service.get_article.return_value = ARCHIVE
            await sync_issues(service, path)
            data = json.loads(path.read_text())
            data["history_cursor"] = "https://evil.test/magazine/2026/08/17"
            path.write_text(json.dumps(data))
            with self.assertRaises(ArchiveError):
                read_index(path)

    async def test_new_year_gap_invalidates_previous_history_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "issues.json"
            service = AsyncMock()
            service.get_article.return_value = '<a href="/magazine/1925/02/21">February 21, 1925</a>'
            await sync_issues(service, path)
            data = json.loads(path.read_text())
            data["history_complete"] = True
            data["history_cursor"] = None
            path.write_text(json.dumps(data))
            service.get_article.return_value = ARCHIVE
            index = await sync_issues(service, path)
            self.assertFalse(index.history_complete)
            self.assertTrue(index.history_cursor.endswith("2026/08/24"))
            self.assertEqual(len(index.issues), 3)

    def test_first_issue_establishes_history_completion(self):
        issue, previous = parse_issue_navigation('<title>The New Yorker February 21, 1925 | The New Yorker</title>',
                                                  "https://www.newyorker.com/magazine/1925/02/21")
        self.assertEqual(issue.issue_date.year, 1925)
        self.assertIsNone(previous)
