import base64
import json
import unittest
import tempfile
from pathlib import Path
from dataclasses import asdict
from copy import deepcopy
from unittest.mock import Mock, patch

import httpx

from app.newyorker.client import NewYorkerClient
from app.newyorker.errors import AuthenticationRequired
from app.newyorker.http_auth import (
    Probe, VerificationSpec, discover_browser_probes, field_probes, matches,
    safe_request_description, safe_response_fields, safe_response_schema, validate_probe, verify_http_session,
    template_account_url, observed_post_body, read_spec, discover_spec, safe_request_body_description, invalid_signature_token,
)

EMAIL = "subscriber@example.test"
payload = base64.urlsafe_b64encode(json.dumps({"email": EMAIL}).encode()).decode().rstrip("=")
STATE = {"cookies": [
    {"name": "CN_token_id", "value": f"e30.{payload}.synthetic", "domain": ".newyorker.com", "path": "/", "expires": -1, "secure": True},
    {"name": "CN_token_access", "value": "synthetic", "domain": ".newyorker.com", "path": "/", "expires": -1, "secure": True},
], "origins": []}
SPEC = VerificationSpec(Probe("https://www.newyorker.com/api/identity", "/email"),
                        Probe("https://www.newyorker.com/api/subscription", "/isActiveSubscriber"))


class HttpAuthTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, *, subscriber=True, public_identity=False, public_subscriber=False,
                    authenticated_status=200, guest_status=200, subscriber_field=True):
        def handler(request):
            signed_in = "CN_token_access=synthetic" in request.headers.get("cookie", "")
            if signed_in and authenticated_status != 200:
                return httpx.Response(authenticated_status, json={})
            if not signed_in and guest_status != 200:
                return httpx.Response(guest_status, json={})
            if request.url.path.endswith("identity"):
                return httpx.Response(200, json={"email": EMAIL} if signed_in or public_identity else {})
            if request.url.path.endswith("subscription"):
                return httpx.Response(200, json={"isActiveSubscriber": subscriber if signed_in else public_subscriber} if subscriber_field else {})
            return httpx.Response(200, text="article")
        transport = httpx.MockTransport(handler)
        async with NewYorkerClient(STATE, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
            result = await verify_http_session(client, guest, STATE, SPEC)
            if result.status == "verified":
                self.assertEqual(await client.get_article("https://www.newyorker.com/magazine/example"), "article")
            else:
                with self.assertRaises(AuthenticationRequired):
                    await client.get_article("https://www.newyorker.com/magazine/example")
        return result

    async def test_live_signals_and_negative_controls_verify_session(self):
        result = await self.check()
        self.assertEqual(result.status, "verified")
        self.assertTrue(result.authenticated)
        self.assertTrue(result.subscriber)

    async def test_public_identity_is_not_authentication(self):
        self.assertEqual((await self.check(public_identity=True)).reason, "identity_probe_is_public")

    async def test_public_subscriber_flag_is_not_entitlement(self):
        self.assertEqual((await self.check(public_subscriber=True)).reason, "subscriber_probe_is_public")

    async def test_signed_in_nonsubscriber_remains_blocked(self):
        result = await self.check(subscriber=False)
        self.assertEqual(result.status, "not_subscribed")
        self.assertTrue(result.authenticated)
        self.assertFalse(result.subscriber)

    async def test_missing_subscriber_signal_is_unknown(self):
        result = await self.check(subscriber_field=False)
        self.assertEqual(result.status, "unverifiable")
        self.assertIsNone(result.subscriber)

    async def test_unauthorized_requires_reconnect_but_forbidden_is_unknown(self):
        self.assertEqual((await self.check(authenticated_status=401)).status, "authentication_required")
        self.assertEqual((await self.check(authenticated_status=403)).status, "unverifiable")

    async def test_anonymous_control_must_finish(self):
        self.assertEqual((await self.check(guest_status=503)).reason, "anonymous_control_unavailable")
        self.assertEqual((await self.check(guest_status=401)).status, "verified")

    async def test_untrusted_decoded_id_token_alone_is_not_proof(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        async with NewYorkerClient(STATE, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
            result = await verify_http_session(client, guest, STATE, SPEC)
        self.assertEqual(result.status, "unverifiable")

    async def test_network_errors_are_safe_and_do_not_claim_expiration(self):
        def handler(request):
            raise httpx.ConnectError("SECRET UPSTREAM DETAIL", request=request)
        transport = httpx.MockTransport(handler)
        async with NewYorkerClient(STATE, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
            result = await verify_http_session(client, guest, STATE, SPEC)
        self.assertEqual(result.status, "unverifiable")
        self.assertNotIn("SECRET", json.dumps(result.__dict__))

    def test_discovery_uses_explicit_newyorker_subscription_fields(self):
        identity, subscriber = field_probes({"email": EMAIL, "isActiveSubscriber": True}, SPEC.identity.url, None, EMAIL)
        self.assertEqual(identity[0].pointer, "/email")
        self.assertEqual(subscriber[0].pointer, "/isActiveSubscriber")
        _, global_subscriber = field_probes({"isActiveSubscriber": True}, "https://id.condenast.com/userinfo", None, EMAIL)
        self.assertEqual(global_subscriber, [])
        _, branded = field_probes({"subscription": {"brand": "The New Yorker", "status": "active"}},
                                  "https://id.condenast.com/userinfo", None, EMAIL)
        self.assertEqual(branded[0].pointer, "/subscription/status")

    def test_cookie_facts_and_unrelated_active_fields_do_not_grant_access(self):
        _, subscribers = field_probes({"fct": {"cds": ["newyorker.com:print"]},
                                      "newsletter": {"active": True}}, SPEC.subscriber.url, None, EMAIL)
        self.assertEqual(subscribers, [])

    def test_brand_guard_prevents_list_reordering_from_granting_other_title_access(self):
        observed = {"subscriptions": [{"brand": "The New Yorker", "active": True}]}
        _, subscribers = field_probes(observed, "https://id.condenast.com/userinfo", None, EMAIL)
        self.assertTrue(matches(observed, subscribers[0]))
        changed = {"subscriptions": [{"brand": "Vogue", "active": True}]}
        self.assertFalse(matches(changed, subscribers[0]))

    def test_newyorker_host_does_not_override_another_title_brand(self):
        _, subscribers = field_probes({"subscription": {"brand": "Vogue", "active": True}},
                                     SPEC.subscriber.url, None, EMAIL)
        self.assertEqual(subscribers, [])

    def test_global_subscriber_probe_requires_newyorker_brand_guard(self):
        with self.assertRaises(ValueError):
            validate_probe(Probe("https://id.condenast.com/userinfo", "/active"), subscriber=True)

    def test_public_brand_and_client_query_options_are_supported(self):
        url = "https://id.condenast.com/userinfo?brandCode=tny&client_id=project-client&fields=email,subscription"
        validate_probe(Probe(url, "/isActiveSubscriber"), subscriber=True)
        identities, subscribers = field_probes({"email": EMAIL, "isActiveSubscriber": True}, url, None, EMAIL)
        self.assertEqual(len(identities), 1)
        self.assertEqual(len(subscribers), 1)

    def test_sensitive_unknown_and_ambiguous_query_options_are_rejected(self):
        queries = ("email=subscriber%40example.test", "token=SECRET", "brand=tny&brand=vogue",
                   "redirect=https%3A%2F%2Fevil.test", "client_id=longsecretpart.longsecretpart.longsecretpart")
        for query in queries:
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_probe(Probe("https://id.condenast.com/userinfo?" + query, "/email"))

    def test_diagnostic_endpoint_description_never_contains_account_values(self):
        description = safe_request_description(
            "https://id.condenast.com/users/SECRET_ACCOUNT/profile?email=SECRET_EMAIL&token=SECRET_TOKEN&brand=SECRET_BRAND",
            "GET", "unsupported_query_parameters",
        )
        output = json.dumps(description)
        self.assertNotIn("SECRET", output)
        self.assertIn("/users/<redacted>/profile", output)
        self.assertEqual(description["query_keys"], ["<redacted>", "brand"])
        unknown = safe_request_description("https://SECRET.condenast.com/SECRET", "POST", "unapproved_destination")
        self.assertNotIn("SECRET", json.dumps(unknown))

    def test_schema_diagnostics_only_report_known_field_names(self):
        fields = safe_response_fields({"SECRET_ACCOUNT": {"email": "SECRET_EMAIL", "subscription": {"active": True}},
                                       "token": "SECRET_TOKEN"})
        self.assertEqual(fields, ["active", "email", "subscription"])

    async def test_discovery_verifies_identity_without_a_subscriber_probe(self):
        seen = []
        clients = []
        def handler(request):
            signed_in = "CN_token_access=synthetic" in request.headers.get("cookie", "")
            seen.append(signed_in)
            return httpx.Response(200, json={"email": EMAIL} if signed_in else {})
        transport = httpx.MockTransport(handler)
        def client_factory(state, **kwargs):
            client = NewYorkerClient(state, transport=transport, **kwargs)
            clients.append(client)
            return client
        with patch("app.newyorker.http_auth.NewYorkerClient", side_effect=client_factory):
            spec, result = await discover_spec(STATE, [SPEC.identity], [], "synthetic-browser")
        self.assertIsNone(spec)
        self.assertTrue(result.authenticated)
        self.assertTrue(result.http_session_reusable)
        self.assertIsNone(result.subscriber)
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual(result.reason, "no_explicit_subscriber_probe_observed")
        self.assertEqual(seen, [True, False])
        self.assertFalse(clients[0]._subscriber_verified)

    async def test_identity_only_discovery_still_rejects_public_lookups(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"email": EMAIL}))
        def client_factory(state, **kwargs):
            return NewYorkerClient(state, transport=transport, **kwargs)
        with patch("app.newyorker.http_auth.NewYorkerClient", side_effect=client_factory):
            spec, result = await discover_spec(STATE, [SPEC.identity], [], "synthetic-browser")
        self.assertIsNone(spec)
        self.assertIsNone(result.authenticated)
        self.assertEqual(result.reason, "identity_probe_is_public")

    def test_schema_traverses_unknown_containers_without_revealing_their_names(self):
        schema = safe_response_schema({"subscriptions": {"cds": {"SECRET_ACCOUNT": {"status": "active", "endDate": "SECRET_DATE"}}}})
        self.assertIn({"path": "/subscriptions/cds/<redacted>/status", "type": "string", "category": "active"}, schema)
        self.assertNotIn("SECRET", json.dumps(schema))
        wide = {f"SECRET_{index}": {"status": "SECRET"} for index in range(1000)}
        self.assertLessEqual(len(safe_response_schema(wide)), 60)

    def test_rejected_body_diagnostics_report_encoding_and_sources_without_values(self):
        request = Mock(post_data=json.dumps({"SECRET_CONTAINER": {"email": EMAIL, "token": "synthetic", "SECRET_KEY": "SECRET_VALUE"}}))
        description = safe_request_body_description(request, STATE)
        self.assertEqual(description["encoding"], "json")
        self.assertIn({"path": "/<redacted>/email", "type": "string", "value_source": "identity_selector"}, description["schema"])
        output = json.dumps(description)
        self.assertNotIn(EMAIL, output)
        self.assertNotIn("SECRET", output)
        self.assertNotIn("synthetic", output)
        request.post_data = "email=subscriber%40example.test&SECRET_KEY=SECRET_VALUE"
        request.all_headers.return_value = {"content-type": "application/x-www-form-urlencoded"}
        form = safe_request_body_description(request, STATE)
        self.assertEqual(form["encoding"], "form")
        self.assertNotIn(EMAIL, json.dumps(form))
        self.assertNotIn("SECRET", json.dumps(form))

    async def test_subscription_token_is_templated_and_removed_from_anonymous_control(self):
        state = deepcopy(STATE)
        claims = base64.urlsafe_b64encode(json.dumps({"cneeAuthToken": "SECRET_NESTED_TOKEN"}).encode()).decode().rstrip("=")
        state["cookies"].append({"name": "pay_ent_sub", "value": f"e30.{claims}.synthetic", "domain": ".newyorker.com", "path": "/", "expires": -1, "secure": True})
        body = observed_post_body(Mock(post_data=json.dumps({"lookupCredential": "SECRET_NESTED_TOKEN", "brandSlug": "the-new-yorker"})), state)
        self.assertNotIn("SECRET", body)
        self.assertEqual(json.loads(body), {"lookupCredential": "{session_cnee_auth_token}", "brandSlug": "the-new-yorker"})
        _, candidates = field_probes({"subscriptions": {"cds": {"newyorker.com": {"status": "active"}}}},
                                     "https://www.newyorker.com/demeter/api/user/v2/subscriptions", None, EMAIL)
        from dataclasses import replace
        spec = VerificationSpec(SPEC.identity, replace(candidates[0], method="POST", body=body))
        validate_probe(spec.subscriber, subscriber=True)
        seen = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"email": EMAIL} if request.headers.get("cookie") else {})
            data = json.loads(request.content)
            seen.append(data)
            if data == {"lookupCredential": "SECRET_NESTED_TOKEN", "brandSlug": "the-new-yorker"}:
                return httpx.Response(200, json={"subscriptions": {"cds": {"newyorker.com": {"status": "active"}}}})
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(401, json={})
        transport = httpx.MockTransport(handler)
        async with NewYorkerClient(state, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
            result = await verify_http_session(client, guest, state, spec)
        self.assertEqual(result.status, "verified")
        self.assertEqual(seen, [{"lookupCredential": "SECRET_NESTED_TOKEN", "brandSlug": "the-new-yorker"}, {"brandSlug": "the-new-yorker"}])

    def test_unknown_lookup_fields_accept_only_session_references_or_public_selectors(self):
        body = observed_post_body(Mock(post_data='{"publicationSlug":"newyorker"}'), STATE)
        self.assertEqual(json.loads(body), {"publicationSlug": "newyorker"})
        for raw in ('{"lookupCredential":"SECRET_UNRECOGNIZED"}', '{"operation":"synthetic"}', '{"lookupCredential":{"value":"synthetic"}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                observed_post_body(Mock(post_data=raw), STATE)

    async def test_brand_slug_and_private_cds_record_keys_verify_with_negative_control(self):
        from dataclasses import replace
        url = "https://www.newyorker.com/demeter/api/user/v2/subscriptions"
        payload = {"subscriptions": {"CDS": {"PRIVATE_RECORD:001": {"status": "active"}}}}
        _, candidates = field_probes(payload, url, None, EMAIL)
        self.assertEqual(len(candidates), 1)
        body = observed_post_body(Mock(post_data='{"brandSlug":"the-new-yorker"}'), STATE)
        subscriber = replace(candidates[0], method="POST", body=body)
        validate_probe(subscriber, subscriber=True)
        self.assertNotIn("PRIVATE_RECORD", json.dumps(asdict(subscriber)))
        self.assertEqual(subscriber.pointer, "/subscriptions/CDS")
        self.assertTrue(matches({"subscriptions": {"CDS": {"CHANGED:002": {"status": "active"}}}}, subscriber))
        self.assertFalse(matches({"subscriptions": {"CDS": {"CHANGED:002": {"status": "inactive"}}}}, subscriber))
        self.assertFalse(matches({"subscriptions": {"CDS": {"CHANGED:002": {"brand": "Vogue", "status": "active"}}}}, subscriber))
        with self.assertRaises(ValueError):
            validate_probe(replace(subscriber, body="{}"), subscriber=True)
        with self.assertRaises(ValueError):
            validate_probe(replace(subscriber, record_status_key="subscriber"), subscriber=True)
        for public in (False, True):
            def handler(request):
                signed_in = "CN_token_access=synthetic" in request.headers.get("cookie", "")
                if request.method == "GET":
                    return httpx.Response(200, json={"email": EMAIL} if signed_in else {})
                self.assertEqual(json.loads(request.content), {"brandSlug": "the-new-yorker"})
                return httpx.Response(200, json=payload if signed_in or public else {"subscriptions": {"CDS": {}}})
            transport = httpx.MockTransport(handler)
            async with NewYorkerClient(STATE, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
                result = await verify_http_session(client, guest, STATE, VerificationSpec(SPEC.identity, subscriber))
            self.assertEqual(result.status, "unverifiable" if public else "verified")
            if public:
                self.assertEqual(result.reason, "subscriber_probe_is_public")

    async def test_subscription_header_reference_is_private_and_omitted_from_guest(self):
        from dataclasses import replace
        url = "https://www.newyorker.com/demeter/api/user/v2/subscriptions"
        token = STATE["cookies"][0]["value"]
        payload = {"subscriptions": {"CDS": {"PRIVATE:001": {"status": "active"}}}}
        _, candidates = field_probes(payload, url, None, EMAIL)
        subscriber = replace(candidates[0], method="POST", body='{"brandSlug":"the-new-yorker"}',
                             authorization_marker="{session_id_token}")
        validate_probe(subscriber, subscriber=True)
        self.assertNotIn(token, json.dumps(asdict(subscriber)))
        seen = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"email": EMAIL} if request.headers.get("cookie") else {})
            authorization = request.headers.get("authorization")
            seen.append(authorization)
            return httpx.Response(200, json=payload) if authorization == "Bearer " + token else httpx.Response(401, json={})
        transport = httpx.MockTransport(handler)
        async with NewYorkerClient(STATE, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
            result = await verify_http_session(client, guest, STATE, VerificationSpec(SPEC.identity, subscriber))
        self.assertEqual(result.status, "verified")
        self.assertEqual(seen, ["Bearer " + token, None])
        for invalid in (replace(subscriber, method="GET"), replace(subscriber, authorization_marker="SECRET"),
                        replace(subscriber, authorization_scheme="Basic"), replace(subscriber, token_cookie="CN_token_access")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_probe(invalid, subscriber=True)

    def test_discovery_matches_saved_header_token_and_captures_unknown_header_privately(self):
        url = "https://www.newyorker.com/demeter/api/user/v2/subscriptions"
        def response(authorization):
            item = Mock(status=200, headers={"content-type": "application/json"})
            item.request.url = url
            item.request.method = "POST"
            item.request.post_data = '{"brandSlug":"the-new-yorker"}'
            item.request.all_headers.return_value = {"authorization": authorization}
            item.body.return_value = json.dumps({"subscriptions": {"CDS": {"PRIVATE:001": {"status": "active"}}}}).encode()
            return item
        known = response("Bearer " + STATE["cookies"][0]["value"])
        unknown = response("Bearer SECRET_UNKNOWN_TOKEN")
        callbacks = {}
        with tempfile.TemporaryDirectory() as directory:
            capture_path = Path(directory) / "private" / "capture.json"
            with patch("playwright.sync_api.sync_playwright") as playwright, patch("app.newyorker.auth.verify_browser_login") as verify:
                context = playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value.new_context.return_value
                context.on.side_effect = lambda event, callback: callbacks.update({event: callback})
                verify.side_effect = lambda *args: [callbacks["response"](item) for item in (known, unknown)]
                _, subscribers, _, diagnostics = discover_browser_probes(STATE, "http://127.0.0.1:9222", 100, capture_path=capture_path)
            self.assertEqual(len(subscribers), 1)
            self.assertEqual(subscribers[0].authorization_marker, "{session_id_token}")
            validate_probe(subscribers[0], subscriber=True)
            self.assertEqual(diagnostics["unsupported_authorization"], 1)
            self.assertNotIn("SECRET", json.dumps(diagnostics))
            capture = json.loads(capture_path.read_text())
            self.assertEqual(capture["request"]["authorization"], "Bearer SECRET_UNKNOWN_TOKEN")

    async def test_invalid_signature_control_handles_missing_header_failures_conservatively(self):
        from dataclasses import replace
        state = deepcopy(STATE)
        token = ".".join(STATE["cookies"][0]["value"].split(".")[:2]) + "." + base64.urlsafe_b64encode(b"synthetic-signature-bytes").decode().rstrip("=")
        state["cookies"][0]["value"] = token
        corrupted = invalid_signature_token(token)
        self.assertEqual(token.split(".")[:2], corrupted.split(".")[:2])
        def decode_signature(value):
            signature = value.split(".")[2]
            return base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        self.assertNotEqual(decode_signature(token), decode_signature(corrupted))
        url = "https://www.newyorker.com/demeter/api/user/v2/subscriptions"
        payload = {"subscriptions": {"CDS": {"PRIVATE:001": {"status": "active"}}}}
        _, candidates = field_probes(payload, url, None, EMAIL)
        subscriber = replace(candidates[0], method="POST", body='{"brandSlug":"the-new-yorker"}', authorization_marker="{session_id_token}")
        for invalid_response in ("rejected", "accepted", "server_error"):
            seen = []
            def handler(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"email": EMAIL} if request.headers.get("cookie") else {})
                authorization = request.headers.get("authorization")
                seen.append(authorization)
                if authorization == "Bearer " + token:
                    return httpx.Response(200, json=payload)
                self.assertNotIn("cookie", request.headers)
                if authorization is None:
                    return httpx.Response(500, text="handler requires an authorization header")
                self.assertEqual(authorization, "Bearer " + corrupted)
                if invalid_response == "rejected":
                    return httpx.Response(401, json={})
                if invalid_response == "accepted":
                    return httpx.Response(200, json=payload)
                return httpx.Response(503, text="temporarily unavailable")
            transport = httpx.MockTransport(handler)
            async with NewYorkerClient(state, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
                result = await verify_http_session(client, guest, state, VerificationSpec(SPEC.identity, subscriber))
                self.assertEqual(client._subscriber_verified, invalid_response == "rejected")
            with self.subTest(invalid_response=invalid_response):
                self.assertEqual(result.reason, {"rejected": "live_identity_and_subscriber_signals", "accepted": "subscriber_probe_is_public", "server_error": "anonymous_control_unavailable"}[invalid_response])
                self.assertEqual(seen, ["Bearer " + token, None, "Bearer " + corrupted])
                self.assertNotIn(token, json.dumps(asdict(result)))
                self.assertEqual(result.control_diagnostics["no_credentials"]["http_status"], 500)

    def selector_state(self, identifier="00000000-0000-0000-0000-000000000001"):
        state = deepcopy(STATE)
        payload = base64.urlsafe_b64encode(json.dumps({"email": EMAIL, "sub": identifier}).encode()).decode().rstrip("=")
        state["cookies"][0]["value"] = f"e30.{payload}.synthetic"
        return state

    async def test_account_templates_and_subscription_post_verify_with_anonymous_controls(self):
        state = self.selector_state()
        account_url = template_account_url("https://www.newyorker.com/api/accounts/v2/users/00000000-0000-0000-0000-000000000001", state)
        request = Mock(post_data=json.dumps({"email": EMAIL}))
        body = observed_post_body(request, state)
        spec = VerificationSpec(Probe(account_url, "/data/email"),
                                Probe("https://www.newyorker.com/demeter/api/user/v2/subscriptions",
                                      "/subscriptions/0/status", expected="active", method="POST", body=body))
        serialized = json.dumps({"version": 1, **asdict(spec)})
        self.assertNotIn(EMAIL, serialized)
        self.assertNotIn("00000000-0000", serialized)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text(serialized)
            spec = read_spec(path)
        seen = []
        def handler(request):
            signed_in = "CN_token_access=synthetic" in request.headers.get("cookie", "")
            seen.append((request.method, signed_in))
            if request.method == "POST":
                self.assertEqual(json.loads(request.content), {"email": EMAIL})
                return httpx.Response(200, json={"subscriptions": [{"status": "active"}]} if signed_in else {})
            self.assertTrue(request.url.path.endswith("00000000-0000-0000-0000-000000000001"))
            return httpx.Response(200, json={"data": {"email": EMAIL}} if signed_in else {})
        transport = httpx.MockTransport(handler)
        async with NewYorkerClient(state, transport=transport) as client, NewYorkerClient({"cookies": []}, transport=transport) as guest:
            result = await verify_http_session(client, guest, state, spec)
        self.assertEqual(result.status, "verified")
        self.assertEqual(seen, [("GET", True), ("GET", False), ("POST", True), ("POST", False)])

    def test_unrelated_ids_mutation_routes_and_request_operations_are_rejected(self):
        state = self.selector_state()
        unrelated = "https://www.newyorker.com/api/users/11111111-1111-1111-1111-111111111111"
        self.assertEqual(template_account_url(unrelated, state), unrelated)
        invalid = [Probe(unrelated, "/email"),
                   Probe("https://www.newyorker.com/api/user/v2/cancel", "/active", method="POST"),
                   Probe("https://www.newyorker.com/api/user/v2/subscriptions", "/active", method="DELETE"),
                   Probe("https://www.newyorker.com/api/user/v2/subscriptions", "/active", method="POST", body='{"operation":"cancel"}'),
                   Probe("https://www.newyorker.com/api/{identity_sub}", "/email")]
        for probe in invalid:
            with self.subTest(probe=probe), self.assertRaises(ValueError):
                validate_probe(probe)
        for body in ({"email": "SECRET_EMAIL"}, {"token": "SECRET_TOKEN"}, {"operation": "cancel"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                observed_post_body(Mock(post_data=json.dumps(body)), state)

    async def test_subscription_lookup_does_not_follow_redirects(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"location": "/api/user/v2/cancel"})
        from app.newyorker.errors import UnsafeUpstreamURL
        async with NewYorkerClient(STATE, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(UnsafeUpstreamURL):
                await client.post_subscription_lookup("https://www.newyorker.com/api/user/v2/subscriptions", body={})
        self.assertEqual(len(seen), 1)

    def test_schema_reports_structure_without_account_values(self):
        schema = safe_response_schema({"data": {"email": EMAIL, "status": "SECRET_STATUS",
                                               "subscriptions": [{"status": "active"}], "SECRET_KEY": "SECRET_VALUE"}})
        output = json.dumps(schema)
        self.assertNotIn(EMAIL, output)
        self.assertNotIn("SECRET", output)
        self.assertIn({"path": "/data/subscriptions/*/status", "type": "string", "category": "active"}, schema)

    def test_discovery_selects_subscription_lookup_but_rejects_other_post_routes(self):
        def response(method, url, data):
            item = Mock()
            item.status = 200
            item.headers = {"content-type": "application/json"}
            item.request.method = method
            item.request.url = url
            item.request.all_headers.return_value = {"user-agent": "synthetic-browser"}
            item.body.return_value = json.dumps(data).encode()
            return item

        approved = response("GET", "https://id.condenast.com/userinfo?brandCode=tny", {"email": EMAIL, "isActiveSubscriber": True})
        post = response("POST", "https://id.condenast.com/graphql", {"email": EMAIL, "subscription": {"brand": "The New Yorker", "active": True}})
        lookup = response("POST", "https://www.newyorker.com/demeter/api/user/v2/subscriptions", {"subscriptions": [{"status": "active"}]})
        lookup.request.post_data = "{}"
        rejected_lookup = response("POST", "https://www.newyorker.com/demeter/api/user/v2/subscriptions", {"subscriptions": {"cds": {"SECRET_ACCOUNT": {"status": "active"}}}})
        rejected_lookup.request.post_data = json.dumps({"SECRET_KEY": "SECRET_VALUE"})
        third_party = response("GET", "https://analytics.example.test/SECRET?email=SECRET", {})
        callbacks = {}
        capture_directory = tempfile.TemporaryDirectory()
        self.addCleanup(capture_directory.cleanup)
        capture_path = Path(capture_directory.name) / "private" / "subscription-discovery-private.json"
        with patch("playwright.sync_api.sync_playwright") as playwright, patch("app.newyorker.auth.verify_browser_login") as verify:
            context = playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value.new_context.return_value
            context.on.side_effect = lambda event, callback: callbacks.update({event: callback})
            def emit(*args):
                for item in (approved, post, lookup, rejected_lookup, third_party):
                    callbacks["response"](item)
            verify.side_effect = emit
            identities, subscribers, agent, diagnostics = discover_browser_probes(STATE, "http://127.0.0.1:9222", 100, capture_path=capture_path)
        import stat
        self.assertEqual(stat.S_IMODE(capture_path.stat().st_mode), 0o600)
        capture = json.loads(capture_path.read_text())
        self.assertEqual(json.loads(capture["request"]["body"]), {"SECRET_KEY": "SECRET_VALUE"})
        self.assertEqual(capture["response"]["subscriptions"]["cds"]["SECRET_ACCOUNT"]["status"], "active")
        self.assertTrue(diagnostics["private_subscription_capture_saved"])
        self.assertEqual(len(identities), 1)
        self.assertEqual(len(subscribers), 2)
        self.assertEqual(subscribers[1].method, "POST")
        self.assertEqual(subscribers[1].body, "{}")
        self.assertEqual(agent, "synthetic-browser")
        self.assertEqual(diagnostics["unsupported_methods"], 1)
        self.assertEqual(diagnostics["unsupported_request_bodies"], 1)
        rejected = next(item for item in diagnostics["responses"] if item["reason"] == "unsupported_request_body")
        self.assertEqual(rejected["request_body"]["encoding"], "json")
        self.assertIn({"path": "/subscriptions/cds/<redacted>/status", "type": "string", "category": "active"}, rejected["schema"])
        self.assertEqual(diagnostics["approved_json_responses"], 2)
        self.assertEqual(diagnostics["unsupported_probe_urls"], 1)
        third_party.body.assert_not_called()
        self.assertNotIn("SECRET", json.dumps(diagnostics))
        self.assertIn("unsupported_method", [item["reason"] for item in diagnostics["responses"]])


if __name__ == "__main__":
    unittest.main()
