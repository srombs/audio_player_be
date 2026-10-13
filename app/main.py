"""FastAPI service entry point."""

from contextlib import asynccontextmanager
import asyncio
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth import router as auth_router
from app.api.audio import router as audio_router
from app.api.issues import router as issues_router
from app.config import get_settings
from app.newyorker.errors import SessionAccessDenied
from app.newyorker.session_service import SessionService


@asynccontextmanager
async def lifespan(application: FastAPI):
    service = SessionService(get_settings())
    application.state.session_service = service
    application.state.audio_cache_lock = asyncio.Lock()
    try:
        yield
    finally:
        await service.aclose()


app = FastAPI(title="New Yorker Audio Backend", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(audio_router)
app.include_router(issues_router)


@app.exception_handler(SessionAccessDenied)
async def session_access_denied(request: Request, error: SessionAccessDenied) -> JSONResponse:
    return JSONResponse(status_code=403 if error.status == "not_subscribed" else 503,
                        content={"detail": {"code": "newyorker_session_" + error.status, "action": error.action}},
                        headers={"Cache-Control": "no-store"})


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()
