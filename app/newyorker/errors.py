"""Upstream integration errors with messages safe for logs and API responses."""


class AuthenticationRequired(Exception):
    """The subscriber session cannot authorize an operation."""


class UpstreamRequestFailed(Exception):
    """An upstream request failed or returned an unsupported response."""


class UnsafeUpstreamURL(ValueError):
    """An upstream destination is outside the integration's allowlist."""


class SessionAccessDenied(Exception):
    """Article access blocked by backend subscriber state, independent of app auth."""

    def __init__(self, status: str, action: str) -> None:
        super().__init__("New Yorker subscriber session is unavailable.")
        self.status = status
        self.action = action
