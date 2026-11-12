"""Populate publisher narration durations for one explicitly selected saved issue."""

import argparse
import asyncio
import json
import sys
from datetime import date

from app.config import get_settings
from app.newyorker.archive import ArchiveError, read_index
from app.newyorker.errors import SessionAccessDenied, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.issue import read_issue_articles
from app.newyorker.parser import ParseError, extract_narration_duration, extract_preloaded_state
from app.newyorker.session import SessionError, save_session
from app.newyorker.session_service import SessionService


async def populate_durations(service: SessionService, issue_date: date) -> dict:
    settings = service.settings
    try:
        index = read_index(settings.issue_index_path.expanduser())
    except ArchiveError as error:
        raise ParseError("Local issue catalog is invalid. Preserve it and run archive sync to a fresh output path.") from error
    issue = next((item for item in index.issues if item.issue_date == issue_date), None) if index else None
    if issue is None:
        raise ParseError("Selected issue is not in the local catalog. Run archive and issue sync first.")
    path = settings.issue_articles_path.expanduser() / f"{issue_date.isoformat()}.json"
    try:
        saved = read_issue_articles(path, issue)
    except ParseError as error:
        raise ParseError(f"{error} Repair the saved list before retrying issue sync.") from error
    if saved is None or not saved.articles:
        raise ParseError("No valid article list is saved for the selected issue. Run issue sync first.")

    articles = []
    for article in saved.articles:
        html = await service.get_article(article.url)
        duration = extract_narration_duration(extract_preloaded_state(html), article.url)
        articles.append(article.model_copy(update={"duration_ms": duration, "audio_available": duration is not None}))

    result = saved.model_copy(update={"articles": articles})
    save_session(path, result.model_dump(mode="json"))
    narrated = sum(item.audio_available is True for item in articles)
    return {"issue_date": issue_date.isoformat(), "total_articles": len(articles),
            "narrated": narrated, "non_narrated": len(articles) - narrated,
            "duration_source": "relatedAudio.durationInMs", "media_requests": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate narration durations from fresh article-page metadata.")
    commands = parser.add_subparsers(dest="command", required=True)
    populate = commands.add_parser("populate")
    populate.add_argument("--date", required=True, type=date.fromisoformat, help="One saved issue date, YYYY-MM-DD.")
    args = parser.parse_args(argv)

    async def run():
        service = SessionService(get_settings())
        try:
            return await populate_durations(service, args.date)
        finally:
            await service.aclose()

    try:
        report = asyncio.run(run())
        print(json.dumps(report))
        return 0
    except SessionAccessDenied as error:
        print(json.dumps({"error": "newyorker_session_" + error.status, "action": error.action}), file=sys.stderr)
        return 2
    except (ArchiveError, ParseError, SessionError) as error:
        print(json.dumps({"error": "duration_population_failed", "reason": str(error)}), file=sys.stderr)
        return 2
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        print(json.dumps({"error": "duration_population_failed", "reason": "Article retrieval failed. Check session status and retry."}),
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Duration population cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print(json.dumps({"error": "duration_population_failed", "reason": "Unexpected duration population failure."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
