"""Public New Yorker image destinations allowed in normalized metadata."""

from urllib.parse import urlsplit, urlunsplit


def safe_image_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname == "media.newyorker.com" and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None and not parsed.query):
            return urlunsplit(parsed._replace(fragment=""))
    except ValueError:
        pass
    return None
