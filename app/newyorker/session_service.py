"""Own the current subscriber client and short-lived live verification results."""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.newyorker.client import NewYorkerClient
from app.newyorker.errors import AuthenticationRequired, SessionAccessDenied, UpstreamRequestFailed
from app.newyorker.http_auth import read_spec, verify_http_session
from app.newyorker.session import SessionError, load_session


class AuthStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["missing", "verified", "reconnect_required", "not_subscribed", "unverifiable"]
    authenticated: bool | None = None
    subscriber: bool | None = None
    http_session_reusable: bool | None = None
    checked_at: datetime | None = None
    action: Literal["none", "reconnect", "check_subscription", "retry", "contact_operator"]
    reason: str


class SessionService:
    def __init__(self, settings: Settings, *, client_factory: Callable = NewYorkerClient,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self._factory = client_factory
        self._clock = clock
        self._lock = asyncio.Lock()
        self._client: NewYorkerClient | None = None
        self._generation: tuple | None = None
        self._cached: AuthStatus | None = None
        self._checked_monotonic = float("-inf")

    def _fingerprint(self) -> tuple:
        def metadata(path: Path) -> tuple | None:
            try:
                stat = path.expanduser().lstat()
                return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode)
            except FileNotFoundError:
                return None
        return (metadata(self.settings.session_state_path), metadata(self.settings.http_verification_path))

    async def _discard_client(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            self._cached = None
            await self._discard_client()

    def _remember(self, status: AuthStatus) -> AuthStatus:
        self._cached = status
        self._checked_monotonic = self._clock()
        return status

    async def status(self) -> AuthStatus:
        async with self._lock:
            return await self._status_locked()

    async def _status_locked(self) -> AuthStatus:
        try:
            generation = self._fingerprint()
        except OSError:
            await self._discard_client()
            self._cached = None
            return AuthStatus(status="unverifiable", action="contact_operator", reason="session_files_unreadable")
        if (generation == self._generation and self._cached is not None
                and self._clock() - self._checked_monotonic < self.settings.auth_verification_cache_seconds
                and (self._cached.status != "verified" or self._client is not None and self._client._subscriber_verified)):
            return self._cached

        self._cached = None
        await self._discard_client()
        self._generation = generation
        if generation[0] is None:
            return self._remember(AuthStatus(status="missing", authenticated=False, http_session_reusable=False,
                                             action="reconnect", reason="session_missing"))
        if generation[1] is None:
            return self._remember(AuthStatus(status="unverifiable", action="contact_operator", reason="verification_rules_missing"))
        try:
            state = load_session(self.settings.session_state_path.expanduser())
        except SessionError:
            return self._remember(AuthStatus(status="unverifiable", action="contact_operator", reason="session_file_invalid"))
        try:
            spec = read_spec(self.settings.http_verification_path.expanduser())
        except SessionError:
            return self._remember(AuthStatus(status="unverifiable", action="contact_operator", reason="verification_rules_invalid"))

        checked_at = datetime.now(timezone.utc)
        try:
            self._client = self._factory(state, user_agent=spec.user_agent,
                                         timeout_seconds=self.settings.auth_request_timeout_seconds)
            async with self._factory({"cookies": [], "origins": []}, user_agent=spec.user_agent,
                                     timeout_seconds=self.settings.auth_request_timeout_seconds) as anonymous:
                result = await verify_http_session(self._client, anonymous, state, spec)
            if self._fingerprint() != generation:
                await self._discard_client()
                self._generation = None
                return AuthStatus(status="unverifiable", action="retry", reason="session_changed_during_check", checked_at=checked_at)
        except (SessionError, ValueError, TypeError, OSError, UpstreamRequestFailed):
            await self._discard_client()
            return self._remember(AuthStatus(status="unverifiable", action="retry", reason="verification_unavailable", checked_at=checked_at))
        except BaseException:
            # Release the owned client even if the request is cancelled.
            await self._discard_client()
            raise

        checked_at = datetime.now(timezone.utc)
        common = dict(authenticated=result.authenticated, subscriber=result.subscriber,
                      http_session_reusable=result.http_session_reusable, checked_at=checked_at)
        if result.status == "verified" and self._client._subscriber_verified:
            return self._remember(AuthStatus(status="verified", action="none", reason="verified", **common))
        await self._discard_client()
        if result.status == "authentication_required":
            return self._remember(AuthStatus(status="reconnect_required", action="reconnect", reason="session_rejected", **common))
        if result.status == "not_subscribed":
            return self._remember(AuthStatus(status="not_subscribed", action="check_subscription", reason="active_subscription_absent", **common))
        return self._remember(AuthStatus(status="unverifiable", action="retry", reason="verification_unavailable", **common))

    async def get_article(self, url: str) -> str:
        """The shared entry point for future article operations; checks freshness first."""
        async with self._lock:
            status = await self._status_locked()
            if status.status != "verified" or self._client is None:
                raise SessionAccessDenied(status.status, status.action)
            generation = self._generation
            try:
                html = await self._client.get_article(url)
            except AuthenticationRequired:
                await self._discard_client()
                self._remember(AuthStatus(status="reconnect_required", authenticated=False, http_session_reusable=False,
                                          action="reconnect", reason="session_rejected", checked_at=datetime.now(timezone.utc)))
                raise SessionAccessDenied("reconnect_required", "reconnect") from None
            except UpstreamRequestFailed:
                self._cached = None
                await self._discard_client()
                raise
            # Do not return content fetched with a session replaced mid-request.
            try:
                unchanged = self._fingerprint() == generation
            except OSError:
                unchanged = False
            if not unchanged:
                self._cached = None
                self._generation = None
                await self._discard_client()
                raise SessionAccessDenied("unverifiable", "retry")
            return html
