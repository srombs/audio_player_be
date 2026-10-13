"""Persistent narration results keyed by canonical article URL."""

from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from app.models.article import ArticleAudio
from app.newyorker.errors import UnsafeUpstreamURL
from app.newyorker.media_client import validate_media_url
from app.newyorker.session import SessionError, save_session


def cache_path(directory: Path, url: str) -> Path:
    return directory.expanduser() / (sha256(url.encode("utf-8")).hexdigest() + ".json")


def read_audio(directory: Path, url: str) -> ArticleAudio | None:
    path = cache_path(directory, url)
    try:
        if path.is_symlink():
            raise SessionError("Audio cache must not be a symlink.")
        result = ArticleAudio.model_validate_json(path.read_text(encoding="utf-8"))
        if result.article.url != url:
            return None
        if result.audio is not None:
            validate_media_url(result.audio.audio_url, audio=True)
        return result
    except FileNotFoundError:
        return None
    except (ValidationError, UnsafeUpstreamURL, UnicodeError):
        # Resolve afresh rather than returning malformed or mismatched data.
        return None
    except OSError as error:
        raise SessionError("Audio cache could not be read.") from error


def write_audio(directory: Path, url: str, result: ArticleAudio) -> None:
    save_session(cache_path(directory, url), result.model_dump(mode="json"))
