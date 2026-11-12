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

    outcomes: list[dict] = []
    for article in saved.articles:
        outcome = {"url": article.url, "title": article.title, "outcome": "not_attempted",
                   "preserved_previous": article.audio_available is not None or article.duration_ms is not None}
        if outcome["preserved_previous"]:
            outcome["previous_metadata"] = {"audio_available": article.audio_available, "duration_ms": article.duration_ms}
        outcomes.append(outcome)

    stopped: dict = {}
    for position, article in enumerate(saved.articles):
        outcome = outcomes[position]
        outcome["outcome"] = "unresolved"
        try:
            html = await service.get_article(article.url)
            duration = extract_narration_duration(extract_preloaded_state(html), article.url)
            articles = list(saved.articles)
            articles[position] = article.model_copy(update={"duration_ms": duration, "audio_available": duration is not None})
            result = saved.model_copy(update={"articles": articles})
            save_session(path, result.model_dump(mode="json"))
        except SessionAccessDenied as error:
            outcome["reason"] = "Subscriber access is unavailable; duration is unresolved."
            stopped = {"error": "newyorker_session_" + error.status, "action": error.action}
            break
        except ParseError:
            outcome["reason"] = "Primary narration metadata is missing or invalid; duration is unresolved."
            continue
        except (UpstreamRequestFailed, UnsafeUpstreamURL):
            outcome["reason"] = "Article retrieval failed; duration is unresolved. Check session status and retry."
            continue
        except SessionError:
            outcome["reason"] = "Could not save duration progress; the previous article list was preserved."
            stopped = {"error": "duration_population_failed", "action": "Check local storage and retry."}
            break
        except (KeyboardInterrupt, asyncio.CancelledError):
            outcome["reason"] = "Duration population was interrupted; duration is unresolved."
            stopped = {"error": "duration_population_interrupted"}
            break

        saved = result
        outcomes[position] = {"url": article.url, "title": article.title,
                              "outcome": "narrated" if duration is not None else "non_narrated",
                              "preserved_previous": False}

    counts = {name: sum(item["outcome"] == name for item in outcomes)
              for name in ("narrated", "non_narrated", "unresolved", "not_attempted")}
    report = {"issue_date": issue_date.isoformat(), "total_articles": len(outcomes),
              "attempted": len(outcomes) - counts["not_attempted"], **counts, "articles": outcomes,
              "duration_source": "relatedAudio.durationInMs", "media_requests": 0}
    if counts["unresolved"] or counts["not_attempted"]:
        report["error"] = "duration_population_failed"
    report.update(stopped)
    return report


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
        if "error" in report:
            print(json.dumps(report), file=sys.stderr)
            return 130 if report["error"] == "duration_population_interrupted" else 2
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
