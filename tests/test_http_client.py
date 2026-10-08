import time
import unittest

import httpx

from app.newyorker.client import NewYorkerClient, browser_cookie_jar
from app.newyorker.errors import AuthenticationRequired, UnsafeUpstreamURL, UpstreamRequestFailed


def cookie(name, value="synthetic", domain=".newyorker.com", path="/", expires=-1, secure=True):
    return {"name": name, "value": value, "domain": domain, "path": path,
            "expires": expires, "secure": secure, "httpOnly": True, "sameSite": "Lax"}


class HttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_domain_path_expiration_and_duplicates_are_preserved(self):
        state = {"cookies": [cookie("domain"), cookie("host", domain="www.newyorker.com"),
                             cookie("scoped", path="/account"), cookie("expired", expires=time.time() - 1),
                             cookie("foreign", domain=".google.com"),
                             cookie("duplicate", "root"), cookie("duplicate", "account", path="/account")], "origins": []}
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text="ok")
        async with NewYorkerClient(state, transport=httpx.MockTransport(handler)) as client:
            await client.get("https://www.newyorker.com/account/profile")
            await client.get("https://newyorker.com/magazine")
        first = requests[0].headers["cookie"]
        self.assertIn("host=", first)
        self.assertIn("scoped=", first)
        self.assertIn("duplicate=root", first)
        self.assertIn("duplicate=account", first)
        self.assertNotIn("expired=", first)
        self.assertNotIn("foreign=", first)
        second = requests[1].headers["cookie"]
        self.assertIn("domain=", second)
        self.assertNotIn("host=", second)
        self.assertNotIn("scoped=", second)

    async def test_browser_security_and_expiration_metadata_survive_conversion(self):
        expires = int(time.time()) + 1000
        jar = browser_cookie_jar({"cookies": [cookie("session"), cookie("persistent", expires=expires)]})
        session, persistent = list(jar)
        self.assertTrue(session.secure)
        self.assertTrue(session.discard)
        self.assertIsNone(session.expires)
        self.assertTrue(session.get_nonstandard_attr("HttpOnly"))
        self.assertEqual(persistent.expires, expires)

    async def test_bearer_probe_does_not_send_newyorker_cookies_to_identity_host(self):
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer synthetic")
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, json={})
        state = {"cookies": [cookie("CN_token_access")], "origins": []}
        async with NewYorkerClient(state, transport=httpx.MockTransport(handler)) as client:
            await client.get("https://id.condenast.com/userinfo", token_cookie="CN_token_access")

    async def test_bearer_cannot_follow_cross_host_redirect(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://www.newyorker.com/"})
        async with NewYorkerClient({"cookies": [cookie("CN_token_access")]}, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(UnsafeUpstreamURL):
                await client.get("https://id.condenast.com/userinfo", token_cookie="CN_token_access")
        self.assertEqual(len(requests), 1)

    async def test_unsafe_destinations_and_unverified_articles_are_blocked(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200)
        async with NewYorkerClient({"cookies": []}, transport=httpx.MockTransport(handler)) as client:
            for url in ("http://www.newyorker.com/", "https://evil.test/", "https://www.newyorker.com.evil.test/",
                        "https://secret@www.newyorker.com/", "https://127.0.0.1/", "https://www.newyorker.com:8443/"):
                with self.subTest(url=url), self.assertRaises(UnsafeUpstreamURL):
                    await client.get(url)
            with self.assertRaises(AuthenticationRequired):
                await client.get_article("https://www.newyorker.com/magazine/example")
        self.assertEqual(requests, [])

    async def test_same_host_get_redirect_remains_a_bodyless_get(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.content, b"")
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": "/account/profile"}, content=b"redirect response")
            return httpx.Response(200, json={"ok": True})
        async with NewYorkerClient({"cookies": []}, transport=httpx.MockTransport(handler)) as client:
            response = await client.get("https://www.newyorker.com/account")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 2)

    async def test_signin_redirect_invalidates_verified_article_access(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "/account/sign-in"}))
        async with NewYorkerClient({"cookies": []}, transport=transport) as client:
            client._subscriber_verified = True
            with self.assertRaises(AuthenticationRequired):
                await client.get_article("https://www.newyorker.com/magazine/example")
            self.assertFalse(client._subscriber_verified)

    async def test_response_size_is_bounded(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 5_000_001))
        async with NewYorkerClient({"cookies": []}, transport=transport) as client:
            with self.assertRaises(UpstreamRequestFailed):
                await client.get("https://www.newyorker.com/")
