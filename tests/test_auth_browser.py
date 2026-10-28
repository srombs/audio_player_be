"""Opt-in offline Chromium checks of the rendered account verifier."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.newyorker.auth import capture_login, check_saved_login, verify_browser_login
from app.newyorker.session import SessionError, load_session

SIGNED_IN = '<label>E-mail<input type="email" value="subscriber@example.test"></label><a href="#">Sign out</a>'
PUBLIC_PROFILE = '<label>E-mail<input type="email" value=""></label><a href="#">Sign out</a>'


@unittest.skipUnless(os.environ.get("NY_AUDIO_BROWSER_TESTS") == "1", "Opt-in: set NY_AUDIO_BROWSER_TESTS=1")
class BrowserAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def context_with_html(self, html: str):
        context = self.browser.new_context()
        context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        self.addCleanup(context.close)
        return context

    def test_public_sign_out_link_is_insufficient(self) -> None:
        with self.assertRaises(SessionError):
            verify_browser_login(self.context_with_html(PUBLIC_PROFILE), 300)

    def test_populated_profile_and_sign_out_pass(self) -> None:
        verify_browser_login(self.context_with_html(SIGNED_IN), 1000)

    def test_read_only_label_and_email_text_pass(self) -> None:
        html = '<section><h2>Credentials</h2><dl><dt>E-mail</dt><dd>subscriber@example.test</dd></dl></section><a href="#">Sign out</a>'
        verify_browser_login(self.context_with_html(html), 1000)

    def test_inline_labelled_email_text_passes(self) -> None:
        html = '<section><p>Email address: subscriber@example.test</p></section><button>Sign out</button>'
        verify_browser_login(self.context_with_html(html), 1000)

    def test_unrelated_contact_email_is_not_account_identity(self) -> None:
        html = PUBLIC_PROFILE + '<footer><p>E-mail: help@example.test</p><a href="mailto:help@example.test">help@example.test</a></footer>'
        with self.assertRaises(SessionError):
            verify_browser_login(self.context_with_html(html), 300)

    def test_hidden_email_text_is_not_account_identity(self) -> None:
        html = PUBLIC_PROFILE + '<p style="display:none">E-mail: subscriber@example.test</p>'
        with self.assertRaises(SessionError):
            verify_browser_login(self.context_with_html(html), 300)

    def test_email_without_sign_out_is_insufficient(self) -> None:
        with self.assertRaises(SessionError):
            verify_browser_login(self.context_with_html('<input type="email" value="subscriber@example.test">'), 300)

    def test_login_redirect_with_email_is_not_an_account_profile(self) -> None:
        context = self.browser.new_context()
        self.addCleanup(context.close)

        def handler(route):
            if route.request.url.endswith("/account/profile"):
                route.fulfill(status=302, headers={"location": "/account/sign-in"})
            else:
                route.fulfill(status=200, content_type="text/html", body=SIGNED_IN)

        context.route("**/*", handler)
        with self.assertRaises(SessionError):
            verify_browser_login(context, 300)

    def test_capture_and_reopen_against_cookie_gated_profile(self) -> None:
        def new_context(**kwargs):
            context = self.browser.new_context(**kwargs)

            def handler(route):
                signed_in = "ny-test-session=synthetic" in route.request.headers.get("cookie", "")
                route.fulfill(status=200, content_type="text/html",
                              body=SIGNED_IN if signed_in else PUBLIC_PROFILE)

            context.route("**/*", handler)
            return context

        browser_proxy = Mock()
        browser_proxy.new_context.side_effect = new_context
        existing_context = new_context()
        self.addCleanup(existing_context.close)
        browser_proxy.contexts = [existing_context]
        existing_context.add_cookies([
            {"name": "ny-test-session", "value": "synthetic", "domain": ".newyorker.com", "path": "/"},
            {"name": "google-test-session", "value": "synthetic", "domain": ".google.com", "path": "/"},
        ])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with contextlib.redirect_stdout(io.StringIO()):
                capture_login(browser_proxy, path, 1000)
                before = path.read_bytes()
                check_saved_login(browser_proxy, path, 1000)
            self.assertEqual(path.read_bytes(), before)
            state = load_session(path)
            self.assertEqual([cookie["name"] for cookie in state["cookies"]], ["ny-test-session"])


if __name__ == "__main__":
    unittest.main()
