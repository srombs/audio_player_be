"""Fetch an issue's table of contents using the shared subscriber-session service."""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.models.article import IssueArticles
from app.models.issue import IssueIndex
from app.newyorker.archive import ArchiveError, issue_url, read_index, write_index
from app.newyorker.errors import SessionAccessDenied, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.parser import ParseError, parse_issue_articles, parse_issue_identity
from app.newyorker.parser import article_url
from app.models.issue import Issue
from app.newyorker.session import SessionError, prepare_session_directory, save_session
from app.newyorker.session_service import SessionService

LATEST_URL = "https://www.newyorker.com/magazine"


def read_issue_articles(path: Path, expected: Issue) -> IssueArticles | None:
    if path.is_symlink():
        raise ParseError("Saved article list must not be a symlink.")
    try:
        if not path.exists():
            return None
        if path.stat().st_size > 10_000_000:
            raise ValueError
        result = IssueArticles.model_validate_json(path.read_text())
        if result.issue.issue_date != expected.issue_date or result.issue.url != expected.url:
            raise ValueError
        if any(article_url(item.url, expected.url) != item.url for item in result.articles):
            raise ValueError
        return result
    except (OSError, ValueError) as error:
        raise ParseError("Saved article list is invalid. Preserve it and sync the issue again.") from error


def approved_issue_url(value: str) -> str:
    target = issue_url(value)
    if target is None:
        raise argparse.ArgumentTypeError("Expected an HTTPS New Yorker magazine issue URL with a date.")
    return target[0]


async def sync_issue(service: SessionService, *, article_directory: Path, capture_directory: Path,
                     index_path: Path, url: str = LATEST_URL) -> IssueArticles:
    if url != LATEST_URL and issue_url(url) is None:
        raise ParseError("Expected a valid magazine issue URL.")
    # Validate local output state before any upstream fetch or overwrite.
    index = read_index(index_path)
    html = await service.get_article(url)
    prepare_session_directory(index_path)
    name = "latest" if url == LATEST_URL else issue_url(url)[1].isoformat()
    save_session(capture_directory / f"{name}-page-private.json", {"url": url, "html": html})
    issue = parse_issue_identity(html, url)
    previous = next((item for item in index.issues if item.url == issue.url), None) if index else None
    if issue.cover_image_url is None and previous is not None:
        issue = issue.model_copy(update={"cover_image_url": previous.cover_image_url})
    articles = parse_issue_articles(html, issue)
    result = IssueArticles(issue=issue, fetched_at=datetime.now(timezone.utc), articles=articles)
    save_session(article_directory / f"{issue.issue_date.isoformat()}.json", result.model_dump(mode="json"))
    known = {item.url: item for item in index.issues} if index else {}
    new_latest = not index or not index.issues or issue.issue_date > max(item.issue_date for item in index.issues)
    known[issue.url] = issue
    # The current issue alias can fill an omitted archive hero. Keep history
    # incomplete if adding a new latest issue needs its backward chain checked.
    complete = bool(index and index.history_complete and issue.url in {item.url for item in index.issues})
    write_index(index_path, IssueIndex(updated_at=result.fetched_at,
                                      issues=sorted(known.values(), key=lambda item: item.issue_date, reverse=True),
                                      history_complete=complete,
                                      history_cursor=issue.url if new_latest else index.history_cursor if index and index.history_cursor else issue.url if not complete else None))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and list an issue's article metadata.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    selection = sync.add_mutually_exclusive_group()
    selection.add_argument("--latest", action="store_true", help="Check the current magazine page directly (default).")
    selection.add_argument("--url", type=approved_issue_url)
    listing = commands.add_parser("list")
    listing.add_argument("--date", type=date.fromisoformat, help="Canonical issue date, YYYY-MM-DD; default is latest saved issue.")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        directory = settings.issue_articles_path.expanduser()
        if args.command == "list":
            index = read_index(settings.issue_index_path.expanduser())
            selected = args.date or (max(index.issues, key=lambda issue: issue.issue_date).issue_date if index and index.issues else None)
            if selected is None:
                raise ParseError("No issue index exists. Run issue sync first.")
            path = directory / f"{selected.isoformat()}.json"
            if path.is_symlink():
                raise ParseError("Saved article list must not be a symlink.")
            try:
                result = IssueArticles.model_validate_json(path.read_text())
            except (OSError, ValueError) as error:
                raise ParseError("No valid article list is saved for that issue. Run issue sync first.") from error
            print(result.model_dump_json())
            return 0

        async def run():
            service = SessionService(settings)
            try:
                return await sync_issue(service, article_directory=directory, capture_directory=settings.issue_page_capture_path.expanduser(),
                                        index_path=settings.issue_index_path.expanduser(), url=args.url or LATEST_URL)
            finally:
                await service.aclose()
        result = asyncio.run(run())
        print(json.dumps({"issue": result.issue.model_dump(mode="json"), "articles": len(result.articles),
                          "with_listen_badge": sum(item.audio_available is True for item in result.articles),
                          "saved_to": str(directory / f"{result.issue.issue_date.isoformat()}.json")}))
        return 0
    except SessionAccessDenied as error:
        print(json.dumps({"error": "newyorker_session_" + error.status, "action": error.action}), file=sys.stderr)
        return 2
    except (ArchiveError, ParseError, SessionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        print("Issue retrieval failed. Check session status and retry; saved lists were preserved.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Issue sync cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print("Issue sync could not finish. Inspect the private page capture if one was saved.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
