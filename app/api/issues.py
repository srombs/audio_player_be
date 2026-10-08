"""Magazine and per-issue article lists through the shared subscriber gate."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_session_service
from app.models.article import IssueArticles
from app.models.issue import IssueList
from app.newyorker.archive import ArchiveError, read_index
from app.newyorker.errors import SessionAccessDenied, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.issue import read_issue_articles, sync_issue
from app.newyorker.parser import ParseError
from app.newyorker.session import SessionError
from app.newyorker.session_service import SessionService

router = APIRouter(prefix="/issues", tags=["issues"])


async def verified_service(service: SessionService = Depends(get_session_service)) -> SessionService:
    status = await service.status()
    if status.status != "verified":
        raise SessionAccessDenied(status.status, status.action)
    return service


def browse_error(status: int, code: str, action: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "action": action},
                         headers={"Cache-Control": "no-store"})


async def saved_index(service: SessionService):
    try:
        index = await run_in_threadpool(read_index, service.settings.issue_index_path.expanduser())
    except ArchiveError:
        raise browse_error(503, "issue_index_invalid", "sync_issue_index") from None
    if index is None:
        raise browse_error(503, "issue_index_missing", "sync_issue_index")
    return index


@router.get("", response_model=IssueList)
async def issues(response: Response, year: int | None = Query(default=None, ge=1925, le=9999),
                 service: SessionService = Depends(verified_service)) -> IssueList:
    response.headers["Cache-Control"] = "no-store"
    index = await saved_index(service)
    entries = sorted((item for item in index.issues if year is None or item.issue_date.year == year),
                     key=lambda item: item.issue_date, reverse=True)
    return IssueList(updated_at=index.updated_at, history_complete=index.history_complete, issues=entries)


@router.get("/{issue_date}/articles", response_model=IssueArticles)
async def issue_articles(issue_date: date, response: Response,
                         service: SessionService = Depends(verified_service)) -> IssueArticles:
    response.headers["Cache-Control"] = "no-store"
    index = await saved_index(service)
    issue = next((item for item in index.issues if item.issue_date == issue_date), None)
    if issue is None:
        raise browse_error(404, "issue_not_found", "refresh_issue_list")
    settings = service.settings
    try:
        result = await run_in_threadpool(read_issue_articles,
                                        settings.issue_articles_path.expanduser() / f"{issue_date.isoformat()}.json", issue)
        if result is not None:
            return result.model_copy(update={"issue": issue})
        return await sync_issue(service, article_directory=settings.issue_articles_path.expanduser(),
                                capture_directory=settings.issue_page_capture_path.expanduser(),
                                index_path=settings.issue_index_path.expanduser(), url=issue.url)
    except (ParseError, ArchiveError, SessionError):
        raise browse_error(503, "issue_articles_unavailable", "sync_issue_articles") from None
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        raise browse_error(502, "issue_upstream_unavailable", "retry") from None
