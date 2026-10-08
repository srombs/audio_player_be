"""Authenticated issue discovery from archive links and observed Previous navigation."""

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

from app.config import get_settings
from app.models.issue import Issue, IssueIndex
from app.newyorker.client import SITE_HOSTS
from app.newyorker.images import safe_image_url
from app.newyorker.errors import SessionAccessDenied, UpstreamRequestFailed, UnsafeUpstreamURL
from app.newyorker.session import SessionError, save_session
from app.newyorker.session_service import SessionService

ARCHIVE_URL = "https://www.newyorker.com/archive"


class ArchiveError(Exception):
    """Safe archive configuration or layout error; never includes upstream HTML."""


def issue_url(value: str, base: str = ARCHIVE_URL) -> tuple[str, date] | None:
    try:
        parsed = urlsplit(urljoin(base, value))
        match = re.fullmatch(r"/magazine/(\d{4})/(\d{2})/(\d{2})/?", parsed.path)
        if (parsed.scheme != "https" or parsed.hostname not in SITE_HOSTS or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or not match):
            return None
        issue_date = date(*(int(part) for part in match.groups()))
        return ("https://www.newyorker.com" + parsed.path.rstrip("/"), issue_date)
    except ValueError:
        return None


class IssueLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []
        self._link: dict | None = None
        self._in_title = False
        self._title: list[str] = []
        self.title = ""
        self.covers: dict[str, str] = {}
        self.images: list[str] = []
        self.og_image: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            self._link = {"href": attributes.get("href", ""), "text": [],
                          "label": attributes.get("aria-label", ""), "rel": attributes.get("rel", "")}
        if tag == "title":
            self._in_title = True
        if tag == "meta" and attributes.get("property") == "og:image":
            self.og_image = safe_image_url(attributes.get("content", ""))
        if tag == "img":
            image = safe_image_url(attributes.get("src", "")) or safe_image_url(attributes.get("data-src", ""))
            if image:
                self.images.append(image)
                target = issue_url(self._link["href"]) if self._link else None
                if target:
                    self.covers.setdefault(target[0], image)

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"].append(data)
        if self._in_title:
            self._title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            label = " ".join(" ".join(self._link["text"]).split()) or self._link["label"]
            self.links.append((self._link["href"], label, self._link["rel"]))
            self._link = None
        if tag == "title":
            self._in_title = False
            self.title = " ".join(" ".join(self._title).split())


def default_label(issue_date: date) -> str:
    return f"{issue_date:%B} {issue_date.day}, {issue_date.year}"


def date_label(label: str) -> bool:
    return bool(re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", label)
                and re.search(r"\b\d{4}\b", label))


def issue_cover_url(html: str) -> str | None:
    parser = IssueLinks()
    parser.feed(html)
    if not parser.og_image:
        return None
    # Prefer the rendered portrait of the same photo over its cropped social
    # preview. Never choose an unrelated article image from the issue page.
    photo = re.match(r"/photos/([^/]+)/", urlsplit(parser.og_image).path)
    if photo:
        for image in parser.images:
            candidate = re.match(r"/photos/([^/]+)/", urlsplit(image).path)
            if candidate and candidate[1] == photo[1]:
                return image
    return parser.og_image


def parse_archive(html: str) -> list[Issue]:
    parser = IssueLinks()
    parser.feed(html)
    issues: dict[str, Issue] = {}
    for href, label, _ in parser.links:
        target = issue_url(href)
        if target is None:
            continue
        url, issue_date = target
        found = Issue(url=url, issue_date=issue_date, label=label if date_label(label) else default_label(issue_date),
                      cover_image_url=parser.covers.get(url))
        if url not in issues or date_label(label):
            issues[url] = found
    if not issues:
        raise ArchiveError("Archive page contains no recognized magazine issue links. Inspect the page layout.")
    return sorted(issues.values(), key=lambda issue: issue.issue_date, reverse=True)


def parse_issue_navigation(html: str, current: str) -> tuple[Issue, str | None]:
    target = issue_url(current)
    if target is None:
        raise ArchiveError("History cursor is not a valid New Yorker issue URL.")
    url, issue_date = target
    parser = IssueLinks()
    parser.feed(html)
    label = parser.title.removesuffix(" | The New Yorker").removeprefix("The New Yorker ")
    if not date_label(label):
        raise ArchiveError("Issue page has no recognizable dated title. History completion cannot be established.")
    # This is the publisher's known first issue; any other missing Previous link
    # remains an incomplete layout, not proof that the archive is exhausted.
    if issue_date == date(1925, 2, 21):
        return Issue(url=url, issue_date=issue_date, label=label, cover_image_url=issue_cover_url(html)), None
    previous: set[str] = set()
    for href, text, rel in parser.links:
        if text.casefold().strip() not in ("previous", "previous issue") and "prev" not in rel.split():
            continue
        earlier = issue_url(href, url)
        if earlier is None or earlier[1] >= issue_date:
            raise ArchiveError("Previous issue link is invalid or does not move backward in time.")
        previous.add(earlier[0])
    if len(previous) != 1:
        raise ArchiveError("Issue page lacks one unambiguous Previous issue link. Saved history remains incomplete.")
    return Issue(url=url, issue_date=issue_date, label=label, cover_image_url=issue_cover_url(html)), previous.pop()


def read_index(path: Path) -> IssueIndex | None:
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.stat().st_size > 10_000_000:
            raise ValueError
        index = IssueIndex.model_validate_json(path.read_text())
        for issue in index.issues:
            if issue_url(issue.url) != (issue.url, issue.issue_date):
                raise ValueError
        if index.history_cursor is not None and issue_url(index.history_cursor) is None:
            raise ValueError
        return index
    except (ValueError, OSError) as error:
        raise ArchiveError("Saved issue index is invalid; preserve it and choose a fresh output path.") from error


def write_index(path: Path, index: IssueIndex) -> None:
    save_session(path, index.model_dump(mode="json"))


async def sync_issues(service: SessionService, path: Path, *, history: bool = False,
                      max_pages: int = 10000, delay_seconds: float = 1,
                      progress: Callable[[IssueIndex], None] | None = None) -> IssueIndex:
    if max_pages < 1 or delay_seconds < 0:
        raise ArchiveError("History page limit must be positive and delay must be nonnegative.")
    index = read_index(path)
    html = await service.get_article(ARCHIVE_URL)
    latest = parse_archive(html)
    links = IssueLinks()
    links.feed(html)
    if any(urlsplit(urljoin(ARCHIVE_URL, href)).path.rstrip("/") == "/magazine"
           and text.casefold() == "explore the issue" for href, text, _ in links.links):
        from app.newyorker.parser import parse_issue_identity
        newest = parse_issue_identity(await service.get_article("https://www.newyorker.com/magazine"),
                                      "https://www.newyorker.com/magazine")
        latest = sorted({item.url: item for item in [*latest, newest]}.values(), key=lambda item: item.issue_date, reverse=True)
    known = {issue.url: issue for issue in index.issues} if index else {}
    # Preserve richer historical labels (including combined issue dates).
    for issue in latest:
        old = known.get(issue.url)
        known[issue.url] = issue.model_copy(update={
            "label": old.label if old and issue.label == default_label(issue.issue_date) else issue.label,
            "cover_image_url": issue.cover_image_url or (old.cover_image_url if old else None),
        })
    cursor = index.history_cursor if index and index.history_cursor else latest[-1].url
    complete = index.history_complete if index else False
    # A new calendar year may no longer expose issues published since the last
    # sync. Walk backward from the new page to avoid marking that gap complete.
    if index and index.issues and latest[-1].issue_date > max(issue.issue_date for issue in index.issues):
        cursor = latest[-1].url
        complete = False

    def checkpoint() -> IssueIndex:
        result = IssueIndex(updated_at=datetime.now(timezone.utc), issues=sorted(known.values(), key=lambda item: item.issue_date, reverse=True),
                            history_cursor=None if complete else cursor, history_complete=complete)
        write_index(path, result)
        if progress is not None:
            progress(result)
        return result

    result = checkpoint()
    if not history or complete:
        return result
    for _ in range(max_pages):
        await asyncio.sleep(delay_seconds)
        html = await service.get_article(cursor)
        issue, previous = parse_issue_navigation(html, cursor)
        if issue.cover_image_url is None and issue.url in known:
            issue = issue.model_copy(update={"cover_image_url": known[issue.url].cover_image_url})
        known[issue.url] = issue
        cursor = previous
        complete = previous is None
        result = checkpoint()
        if complete:
            break
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save a subscriber-authenticated magazine issue index.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--history", action="store_true", help="Resume older issues using actual Previous links.")
    sync.add_argument("--max-pages", type=int, default=10000)
    sync.add_argument("--delay-seconds", type=float, default=1)
    sync.add_argument("--output", type=Path)
    listing = commands.add_parser("list")
    listing.add_argument("--year", type=int)
    listing.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        if args.command == "list":
            index = read_index(args.input or settings.issue_index_path.expanduser())
            if index is None:
                raise ArchiveError("No local issue index exists. Run archive sync first.")
            issues = [issue for issue in index.issues if args.year is None or issue.issue_date.year == args.year]
            print(json.dumps({"history_complete": index.history_complete, "issues": [issue.model_dump(mode="json") for issue in issues]}))
            return 0
        checkpoints = 0
        def progress(index: IssueIndex) -> None:
            nonlocal checkpoints
            checkpoints += 1
            if args.history and (checkpoints == 1 or checkpoints % 25 == 0 or index.history_complete):
                print(f"Saved {len(index.issues)} issues; history complete: {index.history_complete}.", file=sys.stderr, flush=True)
        async def run():
            service = SessionService(settings)
            try:
                return await sync_issues(service, (args.output or settings.issue_index_path).expanduser(),
                                         history=args.history, max_pages=args.max_pages, delay_seconds=args.delay_seconds, progress=progress)
            finally:
                await service.aclose()
        index = asyncio.run(run())
        print(json.dumps({"issues": len(index.issues), "history_complete": index.history_complete,
                          "history_cursor": index.history_cursor, "updated_at": index.updated_at.isoformat()}))
        return 0
    except SessionAccessDenied as error:
        print(json.dumps({"error": "newyorker_session_" + error.status, "action": error.action}), file=sys.stderr)
        return 2
    except (ArchiveError, SessionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        print("Archive retrieval failed. Saved progress is preserved; retry after checking session and network.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Archive sync cancelled. Saved progress can be resumed with --history.", file=sys.stderr)
        return 130
    except Exception:
        print("Archive sync could not finish. Saved progress was preserved.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
