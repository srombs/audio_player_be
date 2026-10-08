import unittest
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.models.audio import RelatedAudio
from app.newyorker.errors import UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.errors import SessionAccessDenied
from app.newyorker.audio import resolve_article_audio, resolve_audio
from app.newyorker.media_client import MediaClient, mp3_prefix, validate_media_url
from app.newyorker.parser import ParseError, extract_preloaded_state, extract_article, extract_related_audio

EMBED = "https://embed-audio.cnevids.com/permalink/narrated/example"
MP3 = "https://audio-resources.cnevids.com/audio/narrated/example-123.mp3"
RELATED = RelatedAudio(name="Example", audioId="example", durationInMs=123000, embedUrl=EMBED)
PREFIX = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 4086


ARTICLE_URL = 'https://www.newyorker.com/magazine/2026/10/12/example-story'
STATE = {'transformed': {'head.canonicalUrl': ARTICLE_URL, 'article': {
    'id': 'article-example',
    'headerProps': {'dangerousHed': 'An <em>Example</em> &amp; Story', 'dangerousDek': 'A synthetic description.',
                    'contributors': {'author': {'items': [{'name': 'Example Author', 'dangerousBio': 'DO_NOT_EXPORT'}]}}},
    'body': 'DO_NOT_EXPORT',
    'relatedAudio': RELATED.model_dump(by_alias=True),
    'recircRelated': [{'relatedAudio': {'audioId': 'wrong-recommendation'}}]
}}}


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_permalink_redirect_resolves_and_verifies_without_downloading_mp3(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertNotIn('cookie', request.headers)
            self.assertNotIn('authorization', request.headers)
            if str(request.url) == EMBED:
                self.assertNotIn('range', request.headers)
                return httpx.Response(302, headers={'location': MP3, 'content-type': 'application/json'})
            self.assertEqual(str(request.url), MP3)
            self.assertEqual(request.headers['range'], 'bytes=0-4095')
            return httpx.Response(206, headers={'content-type': 'audio/mpeg', 'content-range': 'bytes 0-4095/100000'}, content=PREFIX)
        service = AsyncMock()
        service.get_article.return_value = '<script>window.__PRELOADED_STATE__ = ' + json.dumps(STATE) + ';</script>'
        service.status.return_value = SimpleNamespace(status='verified', action='none')
        async with MediaClient(transport=httpx.MockTransport(handler)) as media:
            result = await resolve_article_audio(service, media, ARTICLE_URL)
        self.assertEqual(result.audio.audio_url, MP3)
        self.assertEqual(result.article.title, 'An Example & Story')
        self.assertNotIn('DO_NOT_EXPORT', result.model_dump_json())
        self.assertEqual(len(calls), 2)
        service.get_article.assert_awaited_once_with(ARTICLE_URL)

    async def test_missing_audio_is_explicit_and_session_rejection_prevents_media_requests(self):
        service = AsyncMock()
        state = deepcopy(STATE)
        state['transformed']['article']['relatedAudio'] = None
        service.get_article.return_value = '<script>window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';</script>'
        service.status.return_value = SimpleNamespace(status='verified', action='none')
        def unexpected_request(request):
            self.fail('No media request should occur')
        async with MediaClient(transport=httpx.MockTransport(unexpected_request)) as media:
            result = await resolve_article_audio(service, media, ARTICLE_URL)
            self.assertIsNone(result.audio)
            service.get_article.side_effect = SessionAccessDenied('reconnect_required', 'reconnect')
            with self.assertRaises(SessionAccessDenied):
                await resolve_article_audio(service, media, ARTICLE_URL)

    async def test_changed_audio_identity_and_unsupported_player_response_fail_explicitly(self):
        for status, headers, body in (
            (302, {'location': MP3.replace('example-', 'unrelated-')}, b''),
            (200, {'content-type': 'text/html'}, b'<html>Unexpected player</html>'),
            (302, {'location': 'https://evil.test/example.mp3'}, b''),
        ):
            with self.subTest(status=status, headers=headers):
                async with MediaClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, content=body))) as media:
                    with self.assertRaises((UpstreamRequestFailed, UnsafeUpstreamURL)):
                        await resolve_audio(RELATED, media)

    async def test_session_replaced_during_resolution_prevents_returning_the_result(self):
        service = AsyncMock()
        service.get_article.return_value = '<script>window.__PRELOADED_STATE__ = ' + json.dumps(STATE) + ';</script>'
        service.status.return_value = SimpleNamespace(status='reconnect_required', action='reconnect')
        media = AsyncMock()
        media.get_audio_redirect.return_value = MP3
        media.verify_source.return_value = SimpleNamespace(audio_url=MP3)
        with self.assertRaises(SessionAccessDenied):
            await resolve_article_audio(service, media, ARTICLE_URL)

    def test_primary_article_and_narration_use_the_observed_schema(self):
        article = extract_article(STATE, ARTICLE_URL)
        self.assertEqual(article.title, 'An Example & Story')
        self.assertEqual(article.authors, ['Example Author'])
        self.assertNotIn('DO_NOT_EXPORT', article.model_dump_json())
        self.assertEqual(extract_related_audio(STATE, ARTICLE_URL), RELATED)
        state = deepcopy(STATE)
        state['transformed']['article']['relatedAudio'] = None
        self.assertIsNone(extract_related_audio(state, ARTICLE_URL))
        del state['transformed']['article']['relatedAudio']
        with self.assertRaises(ParseError):
            extract_related_audio(state, ARTICLE_URL)
        for value in ({}, [], {'name': 'Example', 'durationInMs': True}):
            state['transformed']['article']['relatedAudio'] = value
            with self.subTest(value=value), self.assertRaises(ParseError):
                extract_related_audio(state, ARTICLE_URL)
        with self.assertRaises(ParseError):
            extract_article(STATE, ARTICLE_URL + '-different')

    def test_state_is_json_not_executed_javascript(self):
        self.assertEqual(extract_preloaded_state('<script>window.__PRELOADED_STATE__ = {"transformed":{},"example":"}; x()"}; window.other = 1;</script>'), {"transformed": {}, "example": "}; x()"})
        for html in ('<p>window.__PRELOADED_STATE__ = {};</p>', '<script>window.__PRELOADED_STATE__ = buildState();</script>',
                     '<script>window.__PRELOADED_STATE__ = {} + malicious();</script>',
                     '<script>window.__PRELOADED_STATE__ = {}; window.__PRELOADED_STATE__ = {};</script>'):
            with self.subTest(html=html), self.assertRaises(ParseError):
                extract_preloaded_state(html)

    def test_media_destinations_are_narrow(self):
        self.assertEqual(validate_media_url(EMBED), EMBED)
        self.assertEqual(validate_media_url(MP3, audio=True), MP3)
        for url in ('http://audio-resources.cnevids.com/audio/narrated/example.mp3',
                    'https://evil.test/audio/narrated/example.mp3', MP3+'?secret=token', MP3+'#fragment',
                    MP3.replace('https://', 'https://user:secret@'), MP3.replace('/audio/narrated/', '/unrelated/')):
            with self.subTest(url=url), self.assertRaises(UnsafeUpstreamURL):
                validate_media_url(url, audio=True)

    def test_mp3_magic_rejects_html_and_invalid_frames(self):
        self.assertTrue(mp3_prefix(PREFIX))
        self.assertTrue(mp3_prefix(b'\xff\xfb\x90\x00'))
        for data in (b'', b'<html>sign in</html>', b'ID3garbage!', b'\xff\xff\xff\xff'):
            self.assertFalse(mp3_prefix(data))

    async def test_small_range_verifies_source_without_credentials(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['range'], 'bytes=0-4095')
            self.assertNotIn('cookie', request.headers)
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(206, headers={'content-type': 'audio/mpeg', 'content-range': 'bytes 0-4095/100000'}, content=PREFIX)
        async with MediaClient(transport=httpx.MockTransport(handler)) as media:
            source = await media.verify_source(MP3, RELATED)
        self.assertEqual(source.audio_url, MP3)
        self.assertEqual(source.duration_ms, 123000)
        self.assertEqual(len(requests), 1)

    async def test_player_json_is_captured_and_redirect_diagnostics_are_limited(self):
        observations = []
        def json_handler(request):
            return httpx.Response(200, headers={'content-type': 'application/json', 'set-cookie': 'private=DO_NOT_EXPORT'},
                                  content=b'{"example":true}')
        async with MediaClient(transport=httpx.MockTransport(json_handler), observer=observations.append) as media:
            content_type, text = await media.get_player_document(EMBED)
        self.assertEqual(content_type, 'application/json')
        self.assertEqual(text, '{"example":true}')
        self.assertNotIn('DO_NOT_EXPORT', str(observations))
        def redirect_handler(request):
            return httpx.Response(302, headers={'location': 'https://evil.test/player'})
        async with MediaClient(transport=httpx.MockTransport(redirect_handler), observer=observations.append) as media:
            with self.assertRaises(UnsafeUpstreamURL):
                await media.get_player_document(EMBED)
        self.assertEqual(observations[-1]['status'], 302)
        self.assertEqual(observations[-1]['location'], 'https://evil.test/player')

    async def test_bad_sources_and_external_redirects_never_become_playable(self):
        for status, headers, body, error in (
            (403, {}, b'', UpstreamRequestFailed),
            (200, {'content-type': 'text/html'}, b'<html>Access denied</html>', UpstreamRequestFailed),
            (200, {'content-type': 'audio/mpeg'}, b'not audio', UpstreamRequestFailed),
            (206, {'content-type': 'audio/mpeg', 'content-range': 'bytes 500-4595/100000'}, PREFIX, UpstreamRequestFailed),
            (206, {'content-type': 'audio/mpeg', 'content-range': 'bytes 0-4095/200'}, PREFIX, UpstreamRequestFailed),
            (206, {'content-type': 'audio/mpeg', 'content-range': 'bytes 0-8191/100000'}, PREFIX, UpstreamRequestFailed),
            (302, {'location': 'https://evil.test/audio/narrated/example.mp3'}, b'', UnsafeUpstreamURL),
        ):
            with self.subTest(status=status, headers=headers):
                async with MediaClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, content=body))) as media:
                    with self.assertRaises(error):
                        await media.verify_source(MP3, RELATED)

    async def test_redirect_cookies_are_not_sent_and_full_response_is_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertNotIn('cookie', request.headers)
            if len(calls) == 1:
                return httpx.Response(302, headers={'location': MP3.replace('123', '456'), 'set-cookie': 'tracking=private; Path=/'})
            return httpx.Response(200, headers={'content-type': 'audio/mpeg'}, content=PREFIX + b'\x00' * 10000)
        async with MediaClient(transport=httpx.MockTransport(handler)) as media:
            source = await media.verify_source(MP3, RELATED)
        self.assertTrue(source.audio_url.endswith('456.mp3'))
        self.assertEqual(len(calls), 2)
