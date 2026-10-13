"""Resolve narration on demand when the app selects an article for playback."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_session_service
from app.models.article import ArticleAudio
from app.newyorker.audio import resolve_article_audio
from app.newyorker.audio_cache import read_audio, write_audio
from app.newyorker.errors import SessionAccessDenied, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.media_client import MediaClient
from app.newyorker.parser import ParseError, article_url
from app.newyorker.session import SessionError
from app.newyorker.session_service import SessionService

router = APIRouter(prefix="/articles", tags=["audio"])


class AudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    refresh: bool = Field(default=False, strict=True)

    @field_validator("url")
    @classmethod
    def validate_article_url(cls, value: str) -> str:
        target = article_url(value, value)
        if target is None or any(ord(character) < 32 for character in value):
            raise ValueError("Expected an HTTPS New Yorker article URL.")
        return target


@router.post("/audio", response_model=ArticleAudio,
             summary="Resolve a selected article's narration for direct playback")
async def article_audio(body: AudioRequest, request: Request, response: Response,
                        service: SessionService = Depends(get_session_service)) -> ArticleAudio:
    response.headers["Cache-Control"] = "no-store"
    try:
        # Serialize cache fills in this worker so overlapping requests reuse the
        # first successful resolution. Disk data also survives process restarts.
        async with request.app.state.audio_cache_lock:
            status = await service.status()
            if status.status != "verified":
                raise SessionAccessDenied(status.status, status.action)
            directory = service.settings.audio_cache_path
            if not body.refresh:
                cached = await run_in_threadpool(read_audio, directory, body.url)
                if cached is not None:
                    # A disk read may overlap a credential replacement.
                    status = await service.status()
                    if status.status != "verified":
                        raise SessionAccessDenied(status.status, status.action)
                    return cached
            async with MediaClient() as media:
                result = await resolve_article_audio(service, media, body.url)
            await run_in_threadpool(write_audio, directory, body.url, result)
            return result
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        raise HTTPException(status_code=502,
                            detail={"code": "audio_upstream_unavailable", "action": "retry"},
                            headers={"Cache-Control": "no-store"}) from None
    except (ParseError, SessionError):
        raise HTTPException(status_code=502,
                            detail={"code": "audio_resolution_failed", "action": "retry"},
                            headers={"Cache-Control": "no-store"}) from None
