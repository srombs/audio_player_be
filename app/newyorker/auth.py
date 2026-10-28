"""Open regular Chrome for manual sign-in, then attach to capture or check login."""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.newyorker.session import (
    SessionError,
    load_session,
    newyorker_state,
    prepare_session_directory,
    save_session,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext

ACCOUNT_URL = "https://www.newyorker.com/account/profile"
SIGN_IN_URL = "https://www.newyorker.com/account/sign-in"
DEFAULT_CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# Public profile HTML contains Sign out even without a session. Require a
# populated, labelled account identity too. Return booleans, never email values.
ACCOUNT_LOGIN_SIGNALS = r"""() => {
    const onAccountProfile = location.protocol === 'https:' &&
        ['www.newyorker.com', 'newyorker.com'].includes(location.hostname) &&
        location.pathname.replace(/\/$/, '') === '/account/profile';
    const visible = element => {
        const style = getComputedStyle(element);
        return element.getClientRects().length > 0 &&
            style.visibility !== 'hidden' && style.display !== 'none';
    };
    const text = element => (element.innerText || element.textContent || '').trim();
    const accountArea = element => !element.closest('footer, nav, [role="contentinfo"], [role="navigation"]');
    const email = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    const emailLabel = /^e-?mail(?:\s+address)?\s*:?$/i;
    const labelledEmail = /^e-?mail(?:\s+address)?(?:\s*:\s*|\s+)([^\s@]+@[^\s@]+\.[^\s@]+)(?:\s|$)/i;
    const hasSignOut = Array.from(document.querySelectorAll('a, button')).some(
        element => visible(element) && /^(sign\s*out|log\s*out)$/i.test(
            (element.getAttribute('aria-label') || element.textContent || '').trim()
        )
    );
    const hasAccountEmailInput = Array.from(document.querySelectorAll('input')).some(
        element => visible(element) && accountArea(element) && (
            element.type === 'email' ||
            /^e-?mail$/i.test(element.name || '') ||
            /^e-?mail$/i.test(element.id || '') ||
            Array.from(element.labels || []).some(
                label => emailLabel.test(text(label))
            )
        ) && email.test(element.value.trim())
    );
    const hasAccountEmailText = Array.from(
        document.querySelectorAll('label, dt, p, span, div, strong, h3, h4')
    ).some(element => {
        if (!visible(element) || !accountArea(element)) return false;
        const value = text(element);
        // Read-only account rows may use text instead of an input. Match an
        // explicit email label and its immediate value, never arbitrary emails
        // in a newsletter, contact link, footer, or page-wide text search.
        if (value.length <= 256 && labelledEmail.test(value)) return true;
        if (!emailLabel.test(value)) return false;
        const sibling = element.nextElementSibling;
        if (sibling && visible(sibling) && email.test(text(sibling))) return true;
        const parent = element.parentElement;
        return !!parent && !['BODY', 'HTML', 'MAIN', 'FOOTER', 'NAV'].includes(parent.tagName) &&
            visible(parent) && text(parent).length <= 256 && labelledEmail.test(text(parent));
    });
    return {
        onAccountProfile, hasSignOut, hasAccountEmailInput, hasAccountEmailText,
        authenticated: onAccountProfile && hasSignOut && (hasAccountEmailInput || hasAccountEmailText)
    };
}"""
ACCOUNT_IS_SIGNED_IN = "() => (" + ACCOUNT_LOGIN_SIGNALS + ")().authenticated"


def verification_summary(signals: dict[str, bool]) -> str:
    """Allowlist boolean diagnostics so no account data can enter an error."""
    return ", ".join(
        f"{label}={'yes' if signals.get(key) is True else 'no'}"
        for label, key in (
            ("account page", "onAccountProfile"),
            ("sign out", "hasSignOut"),
            ("email input", "hasAccountEmailInput"),
            ("labelled email text", "hasAccountEmailText"),
        )
    )


def verify_browser_login(context: "BrowserContext", timeout_ms: int) -> None:
    """Fail closed if the rendered account page does not show an identity."""
    from playwright.sync_api import Error as PlaywrightError

    page = context.new_page()
    try:
        response = page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or response.status >= 400:
            raise SessionError("Account page is unavailable; login could not be verified.")
        page.wait_for_function(ACCOUNT_IS_SIGNED_IN, timeout=timeout_ms)
    except PlaywrightError as error:
        summary = "Page diagnostics unavailable."
        try:
            summary = verification_summary(page.evaluate(ACCOUNT_LOGIN_SIGNALS))
        except PlaywrightError:
            pass
        raise SessionError(
            "Could not verify browser login. Expected a Sign out control and a "
            "populated account email input or labelled email text. " + summary
        ) from error
    finally:
        page.close()


def verify_and_save_login(
    browser: "Browser", context: "BrowserContext", path: Path, timeout_ms: int
) -> None:
    try:
        verify_browser_login(context, timeout_ms)
    except SessionError as error:
        raise SessionError("Existing browser: " + str(error)) from error
    state = newyorker_state(context.storage_state(indexed_db=True))
    restored_context = browser.new_context(storage_state=state, accept_downloads=False)
    try:
        try:
            verify_browser_login(restored_context, timeout_ms)
        except SessionError as error:
            raise SessionError(
                "Restored session: existing browser login passed, but saved New Yorker "
                "state did not restore a verified login. " + str(error)
            ) from error
        state = newyorker_state(restored_context.storage_state(indexed_db=True))
    finally:
        restored_context.close()
    save_session(path, state)
    print("Session saved. Browser login verified in a fresh context.")
    print("Subscriber access and httpx reuse are not yet verified (step 3).")


def capture_login(browser: "Browser", path: Path, timeout_ms: int) -> None:
    """Capture an already completed manual login without closing its browser."""
    prepare_session_directory(path)
    if not browser.contexts:
        raise SessionError("Connected Chrome has no default context. Open the manual login window first.")
    # connect_over_cdp exposes the external browser's default context first.
    # Only temporary verification pages and our restored context are closed.
    verify_and_save_login(browser, browser.contexts[0], path, timeout_ms)


def check_saved_login(browser: "Browser", path: Path, timeout_ms: int) -> None:
    context = browser.new_context(storage_state=load_session(path), accept_downloads=False)
    try:
        verify_browser_login(context, timeout_ms)
        print("Saved browser login verified. Subscriber access remains unverified (step 3).")
    finally:
        context.close()


def positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Timeout must be a positive integer.") from error
    if seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive integer.")
    return seconds


def local_cdp_url(value: str) -> str:
    """Only accept a local debugging endpoint with no embedded credentials."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "::1", "localhost")
            and parsed.port is not None
            and parsed.port > 0
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("CDP URL must be a loopback HTTP URL such as http://127.0.0.1:9222.")
    return value


def debugging_port(value: str) -> int:
    port = positive_seconds(value)
    if port > 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return port


def open_chrome(executable: Path, profile: Path, port: int) -> None:
    """Start installed Chrome directly; attach only after the user signs in."""
    executable = executable.expanduser()
    if not executable.is_file():
        raise SessionError("Installed Chrome was not found. Use --chrome-path to select its executable.")
    prepare_session_directory(profile / "Local State")
    subprocess.Popen(
        [str(executable), "--remote-debugging-address=127.0.0.1",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile.resolve()}",
         "--new-window", SIGN_IN_URL],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print("Requested a regular Chrome window using the persistent login profile.")
    print("Finish signing in manually and leave Chrome open, then run:")
    print(f"uv run python -m app.newyorker.auth capture --cdp-url http://127.0.0.1:{port}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open regular Chrome for sign-in, or attach to capture or check New Yorker login."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    open_command = commands.add_parser("open", help="Open installed Chrome for manual sign-in, without Playwright.")
    open_command.add_argument("--chrome-path", type=Path, default=DEFAULT_CHROME_PATH,
                              help="Installed Chrome executable (default: macOS Google Chrome).")
    open_command.add_argument("--port", type=debugging_port, default=9222,
                              help="Local remote-debugging port (default: 9222).")
    for name, help_text in (
        ("login", "Capture an existing Chrome login (alias for capture); sign in manually first."),
        ("check", "Verify saved state in connected Chrome without saving changes."),
        ("capture", "Capture a manual login from an existing local Chrome debugging session."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--cdp-url", type=local_cdp_url, default="http://127.0.0.1:9222",
                             help="Existing Chrome debugging endpoint (default: http://127.0.0.1:9222).")
        command.add_argument("--timeout-seconds", type=positive_seconds, default=30,
                             help="Timeout for connection, navigation, and account checks (default: 30).")
    arguments = parser.parse_args(argv)

    if arguments.command == "open":
        try:
            from app.config import get_settings

            path = get_settings().session_state_path.expanduser()
            prepare_session_directory(path)
            open_chrome(arguments.chrome_path, path.parent / "login-browser-profile", arguments.port)
            return 0
        except SessionError as error:
            print(str(error), file=sys.stderr)
            return 1
        except (OSError, ValueError):
            print("Could not open Chrome. Check --chrome-path and the private session directory.", file=sys.stderr)
            return 1

    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
        from app.config import get_settings
    except ImportError:
        print("Dependencies are missing. Run uv sync.", file=sys.stderr)
        return 1

    try:
        path = get_settings().session_state_path.expanduser()
        if arguments.command in ("login", "capture"):
            prepare_session_directory(path)
        else:
            load_session(path)  # Fail before connecting if state is missing or malformed.
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                arguments.cdp_url, timeout=arguments.timeout_seconds * 1000
            )
            if arguments.command in ("login", "capture"):
                capture_login(browser, path, arguments.timeout_seconds * 1000)
            else:
                check_saved_login(browser, path, arguments.timeout_seconds * 1000)
            # Stop Playwright to disconnect. Never close externally launched Chrome.
            return 0
    except (KeyboardInterrupt, EOFError):
        print("Session operation cancelled; saved session was not changed.", file=sys.stderr)
        return 130
    except SessionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except PlaywrightError:
        # Playwright exceptions may contain URLs with tokens; do not print them.
        print("Could not connect to or verify the existing Chrome session. "
              "Open Chrome with remote debugging enabled at --cdp-url, sign in manually, "
              "and leave the window open. Check network access. "
              "Saved session was not changed.", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Session configuration or file operation failed. Check the configured private path.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
