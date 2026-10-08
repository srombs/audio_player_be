"""Capture a subscriber article privately for audio schema discovery."""

import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.newyorker.parser import article_url, extract_preloaded_state, extract_related_audio
from app.newyorker.media_client import MediaClient
from app.newyorker.session import save_session
from app.newyorker.session_service import SessionService
from app.newyorker.errors import UnsafeUpstreamURL, UpstreamRequestFailed

DEFAULT_URL = "https://www.newyorker.com/magazine/2026/10/12/why-is-illness-so-hard-to-write-about"


async def capture(url: str) -> None:
    target = article_url(url, DEFAULT_URL)
    if target is None:
        raise ValueError("Expected a New Yorker article URL.")
    service = SessionService(get_settings())
    try:
        html = await service.get_article(target)
        save_session(Path("data/audio-article-private.json"), {"url": target, "html": html})
        state = extract_preloaded_state(html)
        related = extract_related_audio(state, target)
        embeds = {related.embed_url} if related else set()
        captures = []
        diagnostics = []
        failures = []
        async with MediaClient(observer=diagnostics.append) as media:
            for embed in sorted(embeds)[:5]:
                try:
                    source_url = await media.get_audio_redirect(embed)
                    captures.append({"url": embed, "source_url": source_url})
                except (UnsafeUpstreamURL, UpstreamRequestFailed) as error:
                    failures.append({"url": embed, "reason": str(error)})
                finally:
                    save_session(Path("data/audio-player-diagnostics-private.json"), {"responses": diagnostics, "failures": failures})
        save_session(Path("data/audio-embeds-private.json"), {"embeds": captures})
        print(json.dumps({"status": "captured" if not failures else "player_capture_incomplete",
                          "audio_redirects": len(captures), "player_failures": len(failures),
                          "reason": failures[0]["reason"] if failures else None,
                          "saved_to": "data/audio-article-private.json"}))
    finally:
        await service.aclose()


def main() -> int:
    try:
        asyncio.run(capture(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
        return 0
    except Exception as error:
        print(json.dumps({"status": "capture_failed", "reason": getattr(error, "status", type(error).__name__)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
