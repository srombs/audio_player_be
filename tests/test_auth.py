import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.newyorker.auth import (
    capture_login, check_saved_login, interactive_login, local_cdp_url, verification_summary,
)
from app.newyorker.session import SessionError, load_session, save_session


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "session.json"
        self.old_state = {"cookies": [], "origins": []}
        save_session(self.path, self.old_state)
        self.browser = Mock()
        self.login_context = Mock()
        self.restored_context = Mock()
        self.browser.new_context.side_effect = [self.login_context, self.restored_context]
        state = {
            "cookies": [{"domain": ".newyorker.com", "value": "SECRET_COOKIE"},
                        {"domain": ".google.com", "value": "GOOGLE_SECRET"}],
            "origins": [],
        }
        self.login_context.storage_state.return_value = state
        self.restored_context.storage_state.return_value = state

    def test_cancel_preserves_existing_session_and_closes_context(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            completed = interactive_login(self.browser, self.path, 100, prompt=lambda _: "cancel")
        self.assertFalse(completed)
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_called_once()
        self.login_context.storage_state.assert_not_called()

    def test_interrupt_preserves_session_and_closes_context(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                interactive_login(self.browser, self.path, 100,
                                  prompt=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_called_once()

    def test_unverified_login_is_never_saved(self) -> None:
        with patch("app.newyorker.auth.verify_browser_login", side_effect=SessionError("not signed in")):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SessionError, "Existing browser:"):
                    interactive_login(self.browser, self.path, 100, prompt=lambda _: "")
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_called_once()

    def test_failed_restoration_preserves_old_session_and_closes_both_contexts(self) -> None:
        with patch("app.newyorker.auth.verify_browser_login", side_effect=[None, SessionError("not restored")]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SessionError, "Restored session:"):
                    interactive_login(self.browser, self.path, 100, prompt=lambda _: "")
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_called_once()
        self.restored_context.close.assert_called_once()

    def test_success_requires_two_checks_and_excludes_third_party_state(self) -> None:
        output = io.StringIO()
        with patch("app.newyorker.auth.verify_browser_login") as verify:
            with contextlib.redirect_stdout(output):
                completed = interactive_login(self.browser, self.path, 100, prompt=lambda _: "")
        self.assertTrue(completed)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(load_session(self.path), {
            "cookies": [{"domain": ".newyorker.com", "value": "SECRET_COOKIE"}], "origins": [],
        })
        restored_arguments = self.browser.new_context.call_args_list[1].kwargs
        self.assertEqual(restored_arguments["storage_state"], load_session(self.path))
        self.assertNotIn("SECRET_COOKIE", output.getvalue())
        self.assertNotIn("GOOGLE_SECRET", output.getvalue())
        self.login_context.storage_state.assert_called_once_with(indexed_db=True)
        self.login_context.close.assert_called_once()
        self.restored_context.close.assert_called_once()

    def test_check_does_not_write_session(self) -> None:
        previous_bytes = self.path.read_bytes()
        with patch("app.newyorker.auth.verify_browser_login") as verify:
            with contextlib.redirect_stdout(io.StringIO()):
                check_saved_login(self.browser, self.path, 100)
        verify.assert_called_once_with(self.login_context, 100)
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.login_context.close.assert_called_once()


    def test_capture_preserves_external_context_and_filters_saved_state(self) -> None:
        self.browser.contexts = [self.login_context]
        self.browser.new_context.side_effect = [self.restored_context]
        with patch("app.newyorker.auth.verify_browser_login") as verify:
            with contextlib.redirect_stdout(io.StringIO()):
                capture_login(self.browser, self.path, 100)
        self.assertEqual(verify.call_count, 2)
        self.login_context.close.assert_not_called()
        self.browser.close.assert_not_called()
        self.restored_context.close.assert_called_once()
        self.assertEqual([cookie["domain"] for cookie in load_session(self.path)["cookies"]], [".newyorker.com"])

    def test_capture_restoration_failure_preserves_session_and_external_context(self) -> None:
        self.browser.contexts = [self.login_context]
        self.browser.new_context.side_effect = [self.restored_context]
        with patch("app.newyorker.auth.verify_browser_login", side_effect=[None, SessionError("not restored")]):
            with self.assertRaises(SessionError):
                capture_login(self.browser, self.path, 100)
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_not_called()
        self.restored_context.close.assert_called_once()

    def test_capture_requires_existing_default_context(self) -> None:
        self.browser.contexts = []
        with self.assertRaises(SessionError):
            capture_login(self.browser, self.path, 100)
        self.assertEqual(load_session(self.path), self.old_state)

    def test_cdp_endpoint_must_be_local_without_credentials(self) -> None:
        for value in ("http://127.0.0.1:9222", "http://localhost:9222/", "http://[::1]:9222"):
            self.assertEqual(local_cdp_url(value), value)
        for value in ("http://example.com:9222", "http://0.0.0.0:9222", "http://localhost",
                      "http://secret@localhost:9222", "http://localhost:9222?token=secret",
                      "http://localhost:9222/path", "http://localhost:99999", "http://localhost:0",
                      "ws://localhost:9222", "http://127.0.0.1:9222#secret"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                local_cdp_url(value)

    def test_verification_summary_only_exposes_allowlisted_booleans(self) -> None:
        summary = verification_summary({
            "onAccountProfile": True, "hasSignOut": True,
            "hasAccountEmailInput": False, "hasAccountEmailText": True,
            "email": "SECRET@example.test", "cookie": "SECRET_COOKIE",
        })
        self.assertIn("labelled email text=yes", summary)
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("@", summary)


if __name__ == "__main__":
    unittest.main()
