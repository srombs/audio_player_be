"""Bounded requests to the narration player/CDN, without subscriber credentials."""

import re
from typing import Callable
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from app.models.audio import AudioSource, RelatedAudio
from app.newyorker.errors import UnsafeUpstreamURL, UpstreamRequestFailed

EMBED_HOST = "embed-audio.cnevids.com"
AUDIO_HOST = "audio-resources.cnevids.com"


def validate_media_url(url: str, *, audio: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == (AUDIO_HOST if audio else EMBED_HOST)
                 and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                 and not parsed.fragment and not parsed.query)
        if audio:
            valid = valid and parsed.path.startswith("/audio/narrated/") and parsed.path.endswith(".mp3")
        else:
            valid = valid and parsed.path.startswith("/permalink/narrated/")
    except ValueError:
        valid = False
    if not valid:
        raise UnsafeUpstreamURL("Unsupported narration player or audio destination.")
    return url


def mp3_prefix(data: bytes) -> bool:
    if data.startswith(b"ID3") and len(data) >= 10:
        return data[3] in (2, 3, 4) and data[4] != 255 and all(byte < 128 for byte in data[6:10])
    if len(data) < 4:
        return False
    a, b, c, _ = data[:4]
    return a == 255 and b & 224 == 224 and b & 24 != 8 and b & 6 != 0 and c >> 4 not in (0, 15) and c & 12 != 12


class MediaClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 15,
                 observer: Callable[[dict], None] | None = None):
        self._observer = observer
        self._http = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False, trust_env=False,
                                       transport=transport, headers={"User-Agent": "NewYorkerAudioBackend/0.1"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._http.aclose()

    async def _read(self, url: str, *, audio: bool = False, audio_redirect: bool = False) -> tuple[str, int, httpx.Headers, bytes]:
        try:
            for _ in range(6):
                validate_media_url(url, audio=audio)
                # A separate client never receives the saved subscriber jar. Also
                # discard any player/CDN cookies before every request/redirect.
                self._http.cookies.clear()
                headers = {"Accept-Encoding": "identity"}
                if audio:
                    headers["Range"] = "bytes=0-4095"
                async with self._http.stream("GET", url, headers=headers) as response:
                    if self._observer:
                        self._observer({"url": url, "status": response.status_code,
                                        "content_type": response.headers.get("content-type"),
                                        "location": response.headers.get("location")})
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise UpstreamRequestFailed("Narration redirect has no destination.")
                        destination = urljoin(url, location)
                        if audio_redirect and urlsplit(destination).hostname == AUDIO_HOST:
                            validate_media_url(destination, audio=True)
                            # The observed permalink redirects straight to the MP3.
                            # Close this response and verify the CDN separately with
                            # a bounded Range request, rather than downloading it.
                            return destination, response.status_code, response.headers, b""
                        url = destination
                        continue
                    if response.status_code not in (200, 206) or not audio and response.status_code != 200:
                        raise UpstreamRequestFailed("Narration metadata or source is unavailable.")
                    data = bytearray()
                    limit = 4096 if audio else 2_000_000
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if audio:
                            data.extend(chunk[:limit - len(data)])
                            if len(data) >= limit:
                                break
                        else:
                            data.extend(chunk)
                            if len(data) > limit:
                                raise UpstreamRequestFailed("Narration metadata exceeds the size limit.")
                    return url, response.status_code, response.headers, bytes(data)
            raise UpstreamRequestFailed("Narration returned too many redirects.")
        except httpx.RequestError as error:
            raise UpstreamRequestFailed("Narration request failed upstream.") from error

    async def get_embed(self, url: str) -> str:
        content_type, text = await self.get_player_document(url)
        if "text/html" not in content_type.lower():
            raise UpstreamRequestFailed("Narration player did not return HTML.")
        return text

    async def get_audio_redirect(self, url: str) -> str:
        destination, status, _, _ = await self._read(url, audio_redirect=True)
        if status not in (301, 302, 303, 307, 308):
            raise UpstreamRequestFailed("Narration permalink did not redirect to a supported audio source.")
        return validate_media_url(destination, audio=True)

    async def get_player_document(self, url: str) -> tuple[str, str]:
        """Capture bounded player metadata even if the response is JSON, not HTML."""
        _, _, headers, data = await self._read(url)
        return headers.get("content-type", ""), data.decode("utf-8", errors="replace")

    async def verify_source(self, url: str, related: RelatedAudio) -> AudioSource:
        final_url, status, headers, data = await self._read(url, audio=True)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("audio/mpeg", "audio/mp3", "application/octet-stream") or not mp3_prefix(data):
            raise UpstreamRequestFailed("Narration source did not return recognizable MP3 bytes.")
        if status == 206:
            match = re.fullmatch(r"bytes 0-(\d+)/(\d+|\*)", headers.get("content-range", ""))
            if (not match or int(match[1]) >= 4096 or int(match[1]) != len(data) - 1
                    or match[2] != "*" and int(match[2]) <= int(match[1])):
                raise UpstreamRequestFailed("Narration source returned an invalid byte range.")
        return AudioSource(audio_id=related.audio_id, duration_ms=related.duration_ms, audio_url=final_url,
                           content_type=content_type, verified_at=datetime.now(timezone.utc))
