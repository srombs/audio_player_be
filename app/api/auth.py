"""Public subscriber-session status without account values or probe diagnostics."""

from fastapi import APIRouter, Depends, Request, Response

from app.newyorker.session_service import AuthStatus, SessionService

router = APIRouter(prefix="/auth", tags=["auth"])


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service


@router.get("/status", response_model=AuthStatus)
async def auth_status(response: Response, service: SessionService = Depends(get_session_service)) -> AuthStatus:
    response.headers["Cache-Control"] = "no-store"
    return await service.status()
