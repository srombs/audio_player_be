"""Resolve the observed narration permalink redirect and verify direct playback."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from app.config import get_settings
from app.models.article import ArticleAudio
from app.models.audio import AudioSource, RelatedAudio
from app.newyorker.errors import SessionAccessDenied, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.media_client import MediaClient
from app.newyorker.parser import ParseError, article_url, extract_article, extract_preloaded_state, extract_related_audio
from app.newyorker.session import SessionError, save_session
from app.newyorker.session_service import SessionService


def matching_source(url: str, related: RelatedAudio) -> None:
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    if not filename.startswith(related.audio_id + "-"):
        raise UpstreamRequestFailed("Narration source identity differs from the article audio ID.")


async def resolve_audio(related: RelatedAudio, media: MediaClient) -> AudioSource:
    source_url = await media.get_audio_redirect(related.embed_url)
    matching_source(source_url, related)
    source = await media.verify_source(source_url, related)
    matching_source(source.audio_url, related)
    return source


async def resolve_article_audio(service: SessionService, media: MediaClient, url: str,
                                *, capture_path: Path | None = None) -> ArticleAudio:
    target = article_url(url, url)
    if target is None:
        raise UnsafeUpstreamURL("Expected an HTTPS New Yorker article URL.")
    html = await service.get_article(target)
    if capture_path:
        save_session(capture_path, {"url": target, "html": html})
    state = extract_preloaded_state(html)
    article = extract_article(state, target)
    related = extract_related_audio(state, target)
    audio = await resolve_audio(related, media) if related else None
    # Recheck session freshness before returning a result after player/CDN I/O.
    status = await service.status()
    if status.status != "verified":
        raise SessionAccessDenied(status.status, status.action)
    return ArticleAudio(article=article, audio=audio)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and verify narrated audio using the subscriber session.")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("url", help="HTTPS New Yorker article URL")
    args = parser.parse_args(argv)

    async def run():
        service = SessionService(get_settings())
        try:
            async with MediaClient() as media:
                return await resolve_article_audio(service, media, args.url,
                                                   capture_path=Path("data/audio-article-private.json"))
        finally:
            await service.aclose()
    try:
        result = asyncio.run(run())
        save_session(Path("data/audio-resolution-last-result.json"), result.model_dump(mode="json"))
        print(result.model_dump_json())
        return 0
    except SessionAccessDenied as error:
        print(json.dumps({"error": "newyorker_session_" + error.status, "action": error.action}), file=sys.stderr)
        return 2
    except (UnsafeUpstreamURL, UpstreamRequestFailed, ParseError, SessionError) as error:
        print(json.dumps({"error": "audio_resolution_failed", "reason": str(error)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Audio resolution cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print(json.dumps({"error": "audio_resolution_failed", "reason": "Unexpected resolution failure."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
