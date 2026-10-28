import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.newyorker.auth import (
    SIGN_IN_URL, capture_login, check_saved_login, local_cdp_url, main, verification_summary,
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
        self.browser.contexts = [self.login_context]
        self.browser.new_context.return_value = self.restored_context
        state = {
            "cookies": [{"domain": ".newyorker.com", "value": "SECRET_COOKIE"},
                        {"domain": ".google.com", "value": "GOOGLE_SECRET"}],
            "origins": [],
        }
        self.login_context.storage_state.return_value = state
        self.restored_context.storage_state.return_value = state

    def test_unverified_login_is_never_saved(self) -> None:
        with patch("app.newyorker.auth.verify_browser_login", side_effect=SessionError("not signed in")):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SessionError, "Existing browser:"):
                    capture_login(self.browser, self.path, 100)
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_not_called()
        self.browser.new_context.assert_not_called()

    def test_failed_restoration_preserves_old_session_and_external_context(self) -> None:
        with patch("app.newyorker.auth.verify_browser_login", side_effect=[None, SessionError("not restored")]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SessionError, "Restored session:"):
                    capture_login(self.browser, self.path, 100)
        self.assertEqual(load_session(self.path), self.old_state)
        self.login_context.close.assert_not_called()
        self.restored_context.close.assert_called_once()

    def test_success_requires_two_checks_and_excludes_third_party_state(self) -> None:
        output = io.StringIO()
        with patch("app.newyorker.auth.verify_browser_login") as verify:
            with contextlib.redirect_stdout(output):
                capture_login(self.browser, self.path, 100)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(load_session(self.path), {
            "cookies": [{"domain": ".newyorker.com", "value": "SECRET_COOKIE"}], "origins": [],
        })
        restored_arguments = self.browser.new_context.call_args.kwargs
        self.assertEqual(restored_arguments["storage_state"], load_session(self.path))
        self.assertNotIn("SECRET_COOKIE", output.getvalue())
        self.assertNotIn("GOOGLE_SECRET", output.getvalue())
        self.login_context.storage_state.assert_called_once_with(indexed_db=True)
        self.login_context.close.assert_not_called()
        self.restored_context.close.assert_called_once()

    def test_check_does_not_write_session(self) -> None:
        previous_bytes = self.path.read_bytes()
        with patch("app.newyorker.auth.verify_browser_login") as verify:
            with contextlib.redirect_stdout(io.StringIO()):
                check_saved_login(self.browser, self.path, 100)
        verify.assert_called_once_with(self.restored_context, 100)
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.restored_context.close.assert_called_once()
        self.login_context.close.assert_not_called()
        self.browser.close.assert_not_called()

    def test_commands_only_attach_to_existing_chrome_and_leave_it_open(self) -> None:
        for command in ("login", "capture", "check"):
            with self.subTest(command=command), \
                    patch("app.config.get_settings") as settings, \
                    patch("playwright.sync_api.sync_playwright") as playwright, \
                    patch("app.newyorker.auth.verify_browser_login"), \
                    contextlib.redirect_stdout(io.StringIO()):
                settings.return_value.session_state_path = self.path
                chromium = playwright.return_value.__enter__.return_value.chromium
                chromium.connect_over_cdp.return_value = self.browser
                self.assertEqual(main([command, "--cdp-url", "http://localhost:9223",
                                       "--timeout-seconds", "5"]), 0)
                chromium.connect_over_cdp.assert_called_once_with("http://localhost:9223", timeout=5000)
                chromium.launch.assert_not_called()
                chromium.launch_persistent_context.assert_not_called()
                self.browser.close.assert_not_called()
                self.login_context.close.assert_not_called()

    def test_connection_failure_preserves_session_and_does_not_launch_browser(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        previous_bytes = self.path.read_bytes()
        error_output = io.StringIO()
        with patch("app.config.get_settings") as settings, \
                patch("playwright.sync_api.sync_playwright") as playwright, \
                contextlib.redirect_stderr(error_output):
            settings.return_value.session_state_path = self.path
            chromium = playwright.return_value.__enter__.return_value.chromium
            chromium.connect_over_cdp.side_effect = PlaywrightError("SECRET_CONNECTION_DETAILS")
            self.assertEqual(main(["login"]), 1)
            chromium.launch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertIn("remote debugging", error_output.getvalue())
        self.assertNotIn("SECRET", error_output.getvalue())

    def test_browser_channel_option_is_removed(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["login", "--browser-channel", "chrome"])
        self.assertEqual(error.exception.code, 2)

    def test_open_uses_installed_chrome_and_reuses_private_profile_without_playwright(self) -> None:
        executable = self.path.parent / "Google Chrome"
        executable.touch()
        profile = self.path.parent / "login-browser-profile"
        before = self.path.read_bytes()
        output = io.StringIO()
        with patch("app.config.get_settings") as settings, \
                patch("app.newyorker.auth.subprocess.Popen") as start, \
                patch("playwright.sync_api.sync_playwright") as playwright, \
                contextlib.redirect_stdout(output):
            settings.return_value.session_state_path = self.path
            for port in (9222, 9223):
                self.assertEqual(main(["open", "--chrome-path", str(executable), "--port", str(port)]), 0)
                start.assert_called_with(
                    [str(executable), "--remote-debugging-address=127.0.0.1",
                     f"--remote-debugging-port={port}", f"--user-data-dir={profile.resolve()}",
                     "--new-window", SIGN_IN_URL],
                    stdin=-3, stdout=-3, stderr=-3, start_new_session=True,
                )
                marker = profile / "existing-profile-marker"
                if port == 9222:
                    marker.write_text("existing profile")
                else:
                    self.assertEqual(marker.read_text(), "existing profile")
            playwright.assert_not_called()
        self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("capture --cdp-url http://127.0.0.1:9223", output.getvalue())

    def test_open_missing_executable_preserves_session_and_reports_error(self) -> None:
        before = self.path.read_bytes()
        output = io.StringIO()
        with patch("app.config.get_settings") as settings, \
                patch("app.newyorker.auth.subprocess.Popen") as start, \
                contextlib.redirect_stderr(output):
            settings.return_value.session_state_path = self.path
            self.assertEqual(main(["open", "--chrome-path", str(self.path.parent / "missing")]), 1)
            start.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("--chrome-path", output.getvalue())

    def test_open_rejects_invalid_debugging_ports(self) -> None:
        for port in ("0", "65536", "invalid"):
            with self.subTest(port=port), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["open", "--port", port])
                self.assertEqual(error.exception.code, 2)


    def test_capture_preserves_external_context_and_filters_saved_state(self) -> None:
        self.browser.contexts = [self.login_context]
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
