"""Interactive New Yorker login; subscriber entitlement is verified in step 3."""

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable
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

SIGN_IN_URL = "https://www.newyorker.com/account/sign-in"
ACCOUNT_URL = "https://www.newyorker.com/account/profile"

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


def interactive_login(
    browser: "Browser",
    path: Path,
    timeout_ms: int,
    prompt: Callable[[str], str] = input,
) -> bool:
    """Save only after login and a separate-context restoration both succeed."""
    prepare_session_directory(path)
    context = browser.new_context(accept_downloads=False)
    try:
        page = context.new_page()
        page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        print("Complete New Yorker sign-in in the browser. No credentials are entered by this command.")
        print("If Google rejects this browser, use New Yorker email sign-in or the capture command.")
        print("Select Keep me signed in if available. Leave the browser open.")
        if prompt("Press Enter when signed in, or type cancel: ").strip().lower() != "":
            print("Login cancelled; the previous session was preserved.")
            return False

        verify_and_save_login(browser, context, path, timeout_ms)
        return True
    finally:
        context.close()


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Establish or check a local New Yorker browser session.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("login", "Sign in manually and securely replace saved browser state."),
        ("check", "Reopen saved state and verify browser login without saving changes."),
        ("capture", "Capture a manual login from an existing local Chrome debugging session."),
    ):
        command = commands.add_parser(name, help=help_text)
        if name == "capture":
            command.add_argument("--cdp-url", type=local_cdp_url, default="http://127.0.0.1:9222",
                                 help="Loopback Chrome debugging endpoint (default: http://127.0.0.1:9222).")
        else:
            command.add_argument("--browser-channel", choices=("chrome", "msedge"), default=None,
                                 help="Use installed Chrome/Edge instead of Playwright Chromium.")
        command.add_argument("--timeout-seconds", type=positive_seconds, default=30,
                             help="Timeout for navigation and account checks, not manual login (default: 30).")
    arguments = parser.parse_args(argv)

    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
        from app.config import get_settings
    except ImportError:
        print("Dependencies are missing. Run uv sync, then uv run playwright install chromium.", file=sys.stderr)
        return 1

    try:
        path = get_settings().session_state_path.expanduser()
        if arguments.command in ("login", "capture"):
            prepare_session_directory(path)
        else:
            load_session(path)  # Fail before opening a browser if state is missing or malformed.
        with sync_playwright() as playwright:
            if arguments.command == "capture":
                browser = playwright.chromium.connect_over_cdp(
                    arguments.cdp_url, timeout=arguments.timeout_seconds * 1000
                )
                capture_login(browser, path, arguments.timeout_seconds * 1000)
                # Stop Playwright to disconnect. Do not close externally launched
                # Chrome or the user's default browser context.
                return 0
            browser = playwright.chromium.launch(headless=False, channel=arguments.browser_channel)
            try:
                if arguments.command == "login":
                    completed = interactive_login(browser, path, arguments.timeout_seconds * 1000)
                    return 0 if completed else 130
                check_saved_login(browser, path, arguments.timeout_seconds * 1000)
                return 0
            finally:
                browser.close()
    except (KeyboardInterrupt, EOFError):
        print("Session operation cancelled; saved session was not changed.", file=sys.stderr)
        return 130
    except SessionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except PlaywrightError:
        # Playwright exceptions may contain URLs with tokens; do not print them.
        print("Browser operation failed. Install Chromium or select an installed browser channel. "
              "For capture, make sure local Chrome is running with remote debugging enabled. "
              "Check network access and keep the browser open. Saved session was not changed.", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Session configuration or file operation failed. Check the configured private path.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
