import asyncio
import base64
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.config import Settings
from app.main import app
from app.newyorker.client import NewYorkerClient
from app.newyorker.errors import SessionAccessDenied
from app.newyorker.http_auth import Probe, VerificationSpec
from app.newyorker.session import save_session
from app.newyorker.session_service import SessionService

EMAIL = "synthetic@example.test"
claims = base64.urlsafe_b64encode(json.dumps({"email": EMAIL}).encode()).decode().rstrip("=")
STATE = {"cookies": [{"name": "CN_token_id", "value": f"e30.{claims}.synthetic", "domain": ".newyorker.com",
                      "path": "/", "expires": -1, "secure": True}], "origins": []}
SPEC = VerificationSpec(Probe("https://www.newyorker.com/api/identity", "/email"),
                        Probe("https://www.newyorker.com/api/subscription", "/isActiveSubscriber"))


class Harness:
    def __init__(self, directory):
        private = Path(directory) / "private"
        self.settings = Settings(session_state_path=private / "session.json", http_verification_path=private / "probes.json",
                                 auth_verification_cache_seconds=60)
        self.mode = "active"
        self.now = 0
        self.requests = []
        self.clients = []
        save_session(self.settings.session_state_path, STATE)
        save_session(self.settings.http_verification_path, {"version": 1, **asdict(SPEC)})
        self.service = SessionService(self.settings, client_factory=self.factory, clock=lambda: self.now)

    def handler(self, request):
        self.requests.append(request.url.path)
        signed_in = "CN_token_id=" in request.headers.get("cookie", "")
        if self.mode == "network":
            raise httpx.ConnectError("SECRET_DETAIL", request=request)
        if self.mode == "rejected" and signed_in:
            return httpx.Response(401, json={})
        if request.url.path == "/api/identity":
            return httpx.Response(200, json={"email": EMAIL} if signed_in else {})
        if request.url.path == "/api/subscription":
            if self.mode == "forbidden" and signed_in:
                return httpx.Response(403, json={})
            return httpx.Response(200, json={"isActiveSubscriber": signed_in and self.mode != "inactive"})
        if self.mode == "article_redirect":
            return httpx.Response(302, headers={"location": "/account/sign-in"})
        if self.mode == "replace_during_article":
            save_session(self.settings.session_state_path, STATE)
        if self.mode == "article_forbidden":
            return httpx.Response(403, json={})
        return httpx.Response(200, text="article body")

    def factory(self, state, **kwargs):
        client = NewYorkerClient(state, transport=httpx.MockTransport(self.handler), **kwargs)
        self.clients.append(client)
        return client


class SessionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.h = Harness(self.directory.name)

    async def asyncTearDown(self):
        await self.h.service.aclose()
        self.directory.cleanup()

    async def test_cache_expires_and_client_is_replaced_and_closed(self):
        initial = await self.h.service.status()
        self.assertEqual(initial.status, "verified")
        first = self.h.clients[0]
        request_count = len(self.h.requests)
        self.h.now = 59
        self.assertEqual(await self.h.service.status(), initial)
        self.assertEqual(len(self.h.requests), request_count)
        self.h.now = 60
        self.assertEqual((await self.h.service.status()).status, "verified")
        self.assertGreater(len(self.h.requests), request_count)
        self.assertTrue(first._http.is_closed)
        self.assertFalse(first._subscriber_verified)

    async def test_concurrent_status_requests_share_one_verification(self):
        results = await asyncio.gather(*(self.h.service.status() for _ in range(10)))
        self.assertTrue(all(result.status == "verified" for result in results))
        self.assertEqual(len(self.h.requests), 4)
        self.assertEqual(len(self.h.clients), 2)
        self.assertTrue(self.h.clients[1]._http.is_closed)

    async def test_missing_session_and_recapture_are_detected_without_restart(self):
        self.h.settings.session_state_path.unlink()
        status = await self.h.service.status()
        self.assertEqual(status.status, "missing")
        self.assertEqual(status.action, "reconnect")
        self.assertEqual(self.h.requests, [])
        save_session(self.h.settings.session_state_path, STATE)
        self.assertEqual((await self.h.service.status()).status, "verified")

    async def test_replacement_and_permission_changes_invalidate_cached_verification(self):
        await self.h.service.status()
        old = self.h.clients[0]
        self.h.mode = "rejected"
        save_session(self.h.settings.session_state_path, STATE)
        self.assertEqual((await self.h.service.status()).status, "reconnect_required")
        self.assertTrue(old._http.is_closed)
        self.h.settings.session_state_path.chmod(0o644)
        status = await self.h.service.status()
        self.assertEqual(status.status, "unverifiable")
        self.assertEqual(status.reason, "session_file_invalid")

    async def test_probe_replacement_invalidates_the_cache(self):
        await self.h.service.status()
        self.h.mode = "inactive"
        save_session(self.h.settings.http_verification_path, {"version": 1, **asdict(SPEC)})
        status = await self.h.service.status()
        self.assertEqual(status.status, "not_subscribed")
        self.assertTrue(status.authenticated)
        self.assertFalse(status.subscriber)

    async def test_network_and_generic_forbidden_are_unverifiable(self):
        for mode in ("network", "forbidden"):
            self.h.mode = mode
            self.h.now += 60
            status = await self.h.service.status()
            self.assertEqual(status.status, "unverifiable")
            self.assertEqual(status.action, "retry")
            self.assertNotIn("SECRET", status.model_dump_json())
            with self.assertRaises(SessionAccessDenied):
                await self.h.service.get_article("https://www.newyorker.com/magazine/example")
        self.assertFalse(any(path.startswith("/magazine") for path in self.h.requests))

    async def test_article_redirect_clears_verified_status_immediately(self):
        await self.h.service.status()
        self.h.mode = "article_redirect"
        with self.assertRaises(SessionAccessDenied) as error:
            await self.h.service.get_article("https://www.newyorker.com/magazine/example")
        self.assertEqual(error.exception.status, "reconnect_required")
        self.assertEqual((await self.h.service.status()).status, "reconnect_required")
        self.assertTrue(self.h.clients[0]._http.is_closed)

    async def test_article_access_rechecks_after_verification_expiration(self):
        self.assertEqual(await self.h.service.get_article("https://www.newyorker.com/magazine/example"), "article body")
        self.h.now = 60
        self.h.mode = "rejected"
        with self.assertRaises(SessionAccessDenied):
            await self.h.service.get_article("https://www.newyorker.com/magazine/second")
        self.assertNotIn("/magazine/second", self.h.requests)

    async def test_session_replaced_during_verification_or_article_cannot_grant_access(self):
        original_handler = self.h.handler
        def handler(request):
            if request.url.path == "/api/subscription" and request.headers.get("cookie"):
                save_session(self.h.settings.session_state_path, STATE)
            return original_handler(request)
        self.h.handler = handler
        status = await self.h.service.status()
        self.assertEqual(status.reason, "session_changed_during_check")
        self.assertIsNone(self.h.service._cached)
        self.assertTrue(self.h.clients[0]._http.is_closed)
        self.h.handler = original_handler
        self.h.mode = "replace_during_article"
        with self.assertRaises(SessionAccessDenied) as error:
            await self.h.service.get_article("https://www.newyorker.com/magazine/example")
        self.assertEqual(error.exception.status, "unverifiable")
        self.assertIsNone(self.h.service._cached)

    async def test_missing_or_invalid_rules_never_use_old_result_file_as_proof(self):
        save_session(self.h.settings.session_state_path.parent / "http-verification-last-result.json", {"status": "verified"})
        self.h.settings.http_verification_path.unlink()
        self.assertEqual((await self.h.service.status()).reason, "verification_rules_missing")
        save_session(self.h.settings.http_verification_path, {"version": 2})
        self.assertEqual((await self.h.service.status()).reason, "verification_rules_invalid")
        self.assertEqual(self.h.requests, [])

    async def test_explicit_inactive_cds_records_are_classified_without_assuming_empty_is_inactive(self):
        subscriber = Probe("https://www.newyorker.com/api/user/v2/subscriptions", "/subscriptions/CDS", expected="active",
                           method="POST", body='{"brandSlug":"the-new-yorker"}', record_status_key="status")
        save_session(self.h.settings.http_verification_path, {"version": 1, **asdict(replace(SPEC, subscriber=subscriber))})
        original_handler = self.h.handler
        payload = {"subscriptions": {"CDS": {"PRIVATE:01": {"status": "inactive"}}}}
        def handler(request):
            return httpx.Response(200, json=payload) if request.method == "POST" else original_handler(request)
        self.h.handler = handler
        self.assertEqual((await self.h.service.status()).status, "not_subscribed")
        self.h.now = 60
        payload["subscriptions"]["CDS"] = {}
        self.assertEqual((await self.h.service.status()).status, "unverifiable")


def test_auth_status_api_is_safe_cached_and_closes_clients(tmp_path):
    h = Harness(tmp_path)
    with patch("app.main.get_settings", return_value=h.settings), patch("app.main.SessionService", return_value=h.service):
        with TestClient(app) as client:
            response = client.get("/auth/status")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert data["status"] == "verified"
            assert data["subscriber"] is True
            assert data["checked_at"] is not None
            assert data["action"] == "none"
            assert EMAIL not in response.text
            assert STATE["cookies"][0]["value"] not in response.text
            assert "control_diagnostics" not in data
            count = len(h.requests)
            assert client.get("/auth/status").json() == data
            assert len(h.requests) == count
            assert client.get("/health").json() == {"status": "ok"}
    assert all(client._http.is_closed for client in h.clients)


def test_auth_status_missing_is_http_200_with_reconnect_action(tmp_path):
    h = Harness(tmp_path)
    h.settings.session_state_path.unlink()
    with patch("app.main.get_settings", return_value=h.settings), patch("app.main.SessionService", return_value=h.service):
        with TestClient(app) as client:
            response = client.get("/auth/status")
    assert response.status_code == 200
    assert response.json()["status"] == "missing"
    assert response.json()["action"] == "reconnect"
    assert h.requests == []


def test_future_article_routes_report_backend_session_errors(tmp_path):
    from fastapi import FastAPI, Request
    from app.main import lifespan, session_access_denied

    for mode, expected_status, expected_code in (
        ("missing", 503, "newyorker_session_missing"),
        ("rejected", 503, "newyorker_session_reconnect_required"),
        ("inactive", 403, "newyorker_session_not_subscribed"),
        ("network", 503, "newyorker_session_unverifiable"),
    ):
        h = Harness(tmp_path / mode)
        h.mode = mode
        if mode == "missing":
            h.settings.session_state_path.unlink()
        test_app = FastAPI(lifespan=lifespan)
        test_app.add_exception_handler(SessionAccessDenied, session_access_denied)

        @test_app.get("/test-article")
        async def article(request: Request):
            return await request.app.state.session_service.get_article("https://www.newyorker.com/magazine/example")

        with patch("app.main.get_settings", return_value=h.settings), patch("app.main.SessionService", return_value=h.service):
            with TestClient(test_app) as client:
                response = client.get("/test-article")
        assert response.status_code == expected_status
        assert response.json()["detail"]["code"] == expected_code
        assert response.headers["cache-control"] == "no-store"
        assert "www-authenticate" not in response.headers
        assert EMAIL not in response.text
        assert "/magazine/example" not in h.requests
