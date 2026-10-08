"""Lightweight HTTP retrieval using scoped Playwright session cookies."""

import time
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.newyorker.errors import AuthenticationRequired, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.session import SessionError, is_newyorker_host

SITE_HOSTS = frozenset({"www.newyorker.com", "newyorker.com"})
PROBE_HOSTS = SITE_HOSTS | {"id.condenast.com", "access.conde.io"}
TOKEN_COOKIES = frozenset({"CN_token_access"})


def validate_upstream_url(url: str, *, probe: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        allowed = PROBE_HOSTS if probe else SITE_HOSTS
        valid = (
            parsed.scheme == "https" and parsed.hostname in allowed
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise UnsafeUpstreamURL("Upstream URL must use HTTPS and an approved host.")
    return url


def browser_cookie_jar(state: dict[str, Any]) -> CookieJar:
    # Python's default policy permits host-only Netscape cookies on subdomains.
    # DomainStrictNonDomain preserves the browser's host-only restriction.
    jar = CookieJar(policy=DefaultCookiePolicy(
        strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain
    ))
    for item in state.get("cookies", []):
        try:
            domain, name, value, path = (item[key] for key in ("domain", "name", "value", "path"))
            if not all(isinstance(v, str) for v in (domain, name, value, path)):
                raise ValueError
            if not is_newyorker_host(domain):
                continue
            if not name or not path.startswith("/") or any(c in name + value for c in "\r\n"):
                raise ValueError
            expiration = item.get("expires", -1)
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
                raise ValueError
            expires = None if expiration == -1 else int(expiration)
            if expires is not None and expires <= time.time():
                continue
            secure = item.get("secure", False)
            if not isinstance(secure, bool):
                raise ValueError
            jar.set_cookie(Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=domain, domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."), path=path, path_specified=True,
                secure=secure, expires=expires, discard=expires is None,
                comment=None, comment_url=None,
                rest={"HttpOnly": item.get("httpOnly", False), "SameSite": item.get("sameSite", "Lax")},
            ))
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise SessionError("Saved session contains malformed cookie metadata. Capture login again.") from error
    return jar


class NewYorkerClient:
    def __init__(
        self, state: dict[str, Any], *, timeout_seconds: float = 30,
        user_agent: str = "NewYorkerAudioBackend/0.1", transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            cookies=browser_cookie_jar(state), timeout=timeout_seconds,
            headers={"User-Agent": user_agent}, follow_redirects=False,
            trust_env=False, transport=transport,
        )
        self._subscriber_verified = False

    async def __aenter__(self) -> "NewYorkerClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._subscriber_verified = False
        await self._http.aclose()

    def _bearer(self, name: str) -> str:
        if name not in TOKEN_COOKIES:
            raise AuthenticationRequired("Unsupported identity token source.")
        values = {
            cookie.value for cookie in self._http.cookies.jar
            if cookie.name == name and cookie.path == "/"
            and is_newyorker_host(cookie.domain) and not cookie.is_expired()
        }
        if len(values) != 1:
            raise AuthenticationRequired("Identity token is missing, expired, or ambiguous. Reconnect New Yorker.")
        return values.pop()

    async def get(self, url: str, *, token_cookie: str | None = None) -> httpx.Response:
        """Retrieve a site page or a discovered, first-party verification probe."""
        return await self._request("GET", url, token_cookie=token_cookie)

    async def post_subscription_lookup(self, url: str, *, body: dict[str, Any] | None = None,
                                       token_cookie: str | None = None, authorization: str | None = None) -> httpx.Response:
        from app.newyorker.http_auth import read_only_subscription_url
        validate_upstream_url(url, probe=True)
        if not read_only_subscription_url(url):
            raise UnsafeUpstreamURL("Unsupported subscription lookup route.")
        return await self._request("POST", url, body=body, token_cookie=token_cookie, authorization=authorization)

    async def _request(self, method: str, url: str, *, body: dict[str, Any] | None = None,
                       token_cookie: str | None = None, authorization: str | None = None) -> httpx.Response:
        validate_upstream_url(url, probe=True)
        original_host = urlsplit(url).hostname
        if authorization is not None:
            from app.newyorker.http_auth import read_only_subscription_url
            if method != "POST" or not read_only_subscription_url(url) or token_cookie is not None or any(c in authorization for c in "\r\n"):
                raise UnsafeUpstreamURL("Unsupported subscription authorization destination.")
        headers = {"Authorization": authorization} if authorization is not None else ({"Authorization": "Bearer " + self._bearer(token_cookie)} if token_cookie else {})
        try:
            for _ in range(6):
                validate_upstream_url(url, probe=True)
                async with self._http.stream(method, url, headers=headers, **({"json": body} if body is not None else {})) as response:
                    response_body = bytearray()
                    async for chunk in response.aiter_bytes():
                        response_body.extend(chunk)
                        if len(response_body) > 5_000_000:
                            raise UpstreamRequestFailed("Upstream response exceeded the size limit.")
                    # aiter_bytes decodes compression; do not decode it a second time.
                    response_headers = dict(response.headers)
                    response_headers.pop("content-encoding", None)
                    response_headers.pop("content-length", None)
                    result = httpx.Response(response.status_code, headers=response_headers,
                                            content=bytes(response_body), request=response.request)
                if result.status_code not in (301, 302, 303, 307, 308):
                    return result
                location = result.headers.get("location")
                if not location:
                    raise UpstreamRequestFailed("Upstream redirect has no destination.")
                destination = urljoin(url, location)
                validate_upstream_url(destination, probe=True)
                if urlsplit(destination).path.rstrip("/") == "/account/sign-in":
                    raise AuthenticationRequired("Subscriber session was redirected to sign-in. Reconnect New Yorker.")
                if method != "GET":
                    raise UnsafeUpstreamURL("Subscription lookups cannot follow redirects.")
                if token_cookie and urlsplit(destination).hostname != original_host:
                    raise UnsafeUpstreamURL("Identity tokens cannot follow redirects to a different host.")
                url = destination
            raise UpstreamRequestFailed("Upstream returned too many redirects.")
        except httpx.RequestError as error:
            raise UpstreamRequestFailed("Upstream connection failed; authentication could not be checked.") from error

    async def get_article(self, url: str) -> str:
        validate_upstream_url(url)
        if not self._subscriber_verified:
            raise AuthenticationRequired("Verify subscriber access through httpx before retrieving articles.")
        try:
            response = await self.get(url)
        except AuthenticationRequired:
            self._subscriber_verified = False
            raise
        if response.status_code == 401:
            self._subscriber_verified = False
            raise AuthenticationRequired("Subscriber session was rejected. Reconnect New Yorker.")
        if response.status_code >= 400:
            raise UpstreamRequestFailed("Article request failed upstream.")
        return response.text
