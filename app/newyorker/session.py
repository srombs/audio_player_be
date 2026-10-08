"""Private, atomic storage of New Yorker browser state."""

import json
import os
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SessionError(Exception):
    """A safe-to-display error that contains no session contents."""


def is_newyorker_host(host: str) -> bool:
    host = host.lower().lstrip(".")
    return host == "newyorker.com" or host.endswith(".newyorker.com")


def newyorker_state(state: dict[str, Any]) -> dict[str, Any]:
    """Exclude Google and other third-party sessions from the saved artifact."""
    return deepcopy({
        "cookies": [
            cookie for cookie in state.get("cookies", [])
            if is_newyorker_host(cookie.get("domain", ""))
        ],
        "origins": [
            origin for origin in state.get("origins", [])
            if urlsplit(origin.get("origin", "")).scheme == "https"
            and is_newyorker_host(urlsplit(origin.get("origin", "")).hostname or "")
        ],
    })


def prepare_session_directory(path: Path) -> None:
    """Create a private directory; never chmod an arbitrary existing directory."""
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise SessionError(
            "Session directory must be private (mode 700) and not a symlink. "
            "Use a dedicated directory and run chmod 700 on it."
        )
    if path.is_symlink():
        raise SessionError("Session file must not be a symlink.")


def save_session(path: Path, state: dict[str, Any]) -> None:
    """Replace the session only after a complete, private write succeeds."""
    prepare_session_directory(path)
    temporary_path: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError) as error:
        raise SessionError("Could not save session; the previous file was preserved.") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_session(path: Path) -> dict[str, Any]:
    """Read private browser state without printing any cookie or account data."""
    try:
        if path.is_symlink():
            raise SessionError("Session file must not be a symlink.")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SessionError("Session file must have private permissions (mode 600).")
        with path.open(encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError as error:
        raise SessionError("No saved session exists. Run the login command first.") from error
    except (OSError, ValueError) as error:
        raise SessionError("Could not read saved session. Run login to replace it.") from error

    if (
        not isinstance(state, dict)
        or not isinstance(state.get("cookies"), list)
        or not isinstance(state.get("origins"), list)
        or any(not isinstance(item, dict) for item in state["cookies"] + state["origins"])
    ):
        raise SessionError("Saved session is malformed. Run login to replace it.")
    return state
