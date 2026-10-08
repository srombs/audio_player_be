"""Discover real account probes, then verify browser-session reuse via httpx."""

import argparse
import asyncio
import base64
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from app.newyorker.client import NewYorkerClient, SITE_HOSTS, TOKEN_COOKIES, validate_upstream_url
from app.newyorker.errors import AuthenticationRequired, UnsafeUpstreamURL, UpstreamRequestFailed
from app.newyorker.session import SessionError, load_session, save_session


@dataclass(frozen=True)
class Probe:
    url: str
    pointer: str
    token_cookie: str | None = None
    expected: bool | str = True
    brand_pointer: str | None = None
    brand_value: str | None = None
    method: str = "GET"
    body: str | None = None
    record_status_key: str | None = None
    authorization_marker: str | None = None
    authorization_scheme: str = "Bearer"


@dataclass(frozen=True)
class VerificationSpec:
    identity: Probe
    subscriber: Probe
    user_agent: str = "NewYorkerAudioBackend/0.1"


@dataclass(frozen=True)
class VerificationResult:
    status: str
    authenticated: bool | None
    subscriber: bool | None
    http_session_reusable: bool | None
    reason: str
    control_diagnostics: dict[str, Any] | None = None


# Public request options can select a brand/schema, but must never carry a
# subscriber's email, identifier, session, bearer token, or redirect URL.
PUBLIC_QUERY_KEYS = frozenset({
    "brand", "brandCode", "brand_code", "site", "publication", "product",
    "client_id", "clientId", "locale", "lang", "language", "fields", "format", "version",
})
BRAND_QUERY_KEYS = frozenset({"brand", "brandCode", "brand_code", "site", "publication", "product"})
NEWYORKER_BRANDS = frozenset({"newyorker", "thenewyorker", "newyorkercom", "tny"})
DIAGNOSTIC_PATH_PARTS = frozenset({
    "api", "v1", "v2", "v3", "me", "userinfo", "users", "user", "identity",
    "account", "accounts", "profile", "access", "entitlement", "entitlements",
    "subscription", "subscriptions", "subscriber", "subscribers", "status",
    "session", "sessions", "auth", "authenticate", "authorize", "authorization",
    "check", "token", "refresh", "graphql", "bootstrap", "verify", "validate",
    "facts", "fact", "brands", "newyorker", "tny", "newyorker.com",
})
DIAGNOSTIC_FIELDS = frozenset({
    "user", "users", "profile", "data", "result", "response", "identity", "email", "emailaddress",
    "status", "authenticated", "isauthenticated", "subscriber", "issubscriber", "isactivesubscriber",
    "hasactivesubscription", "subscription", "subscriptions", "entitlement", "entitlements",
    "active", "isactive", "access", "membership", "memberships", "plan", "plans", "fct", "cds",
    "sub", "exp", "brand", "site", "publication", "product", "errors", "error",
    "startdate", "enddate", "expirydate", "expirationdate", "expiresat", "validuntil",
    "subscriptionstatus", "entitlementstatus", "subscriptiontype", "productcode", "brandcode",
})


class ProbeURLRejected(ValueError):
    """A rejection code with no URL or credential values."""


IDENTITY_MARKERS = {"{identity_sub}": "sub", "{identity_xid}": "xid", "{identity_email}": "email"}


def identity_selectors(state: dict[str, Any]) -> dict[str, str]:
    """Untrusted token claims select an account; live responses prove access."""
    values: dict[str, set[str]] = {marker: set() for marker in IDENTITY_MARKERS}
    for cookie in state.get("cookies", []):
        if cookie.get("name") != "CN_token_id":
            continue
        try:
            part = cookie["value"].split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            for marker, claim in IDENTITY_MARKERS.items():
                value = payload.get(claim)
                if isinstance(value, str) and 0 < len(value) <= 256 and not any(c in value for c in "\r\n"):
                    values[marker].add(value)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            continue
    return {marker: next(iter(items)) for marker, items in values.items() if len(items) == 1}


def template_account_url(url: str, state: dict[str, Any]) -> str:
    parsed = urlsplit(url)
    selectors = identity_selectors(state)
    # Only account selectors immediately after /users/ may become path templates.
    parts = parsed.path.split("/")
    for index, part in enumerate(parts):
        if index and parts[index - 1] == "users":
            parts[index] = next((marker for marker, value in selectors.items()
                                 if unquote(part) == value), part)
    return urlunsplit(parsed._replace(path="/".join(parts)))


def resolve_probe_url(probe: Probe, state: dict[str, Any]) -> str:
    validate_probe(probe)
    url = probe.url
    selectors = identity_selectors(state)
    for marker in IDENTITY_MARKERS:
        if marker in url:
            if marker not in selectors:
                raise AuthenticationRequired("Account selector unavailable. Capture login again.")
            url = url.replace(marker, quote(selectors[marker], safe=""))
    validate_upstream_url(url, probe=True)
    return url


def read_only_subscription_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in SITE_HOSTS and bool(re.fullmatch(
        r"/(?:[a-z][a-z0-9_-]{0,39}/)?api/user/v2/subscriptions", parsed.path
    )) and not parsed.query


SESSION_TOKEN_MARKERS = {
    "{session_access_token}": ("CN_token_access", None),
    "{session_id_token}": ("CN_token_id", None),
    "{session_access_cookie}": ("CN_access", None),
    "{session_entitlement_cookie}": ("pay_ent_sub", None),
    "{session_cnee_auth_token}": ("pay_ent_sub", "cneeAuthToken"),
}
PUBLIC_LOOKUP_VALUES = frozenset({"tny", "newyorker", "newyorker.com", "The New Yorker", "the-new-yorker"})
MUTATION_BODY_KEYS = frozenset({"operation", "action", "query", "mutation", "method"})


def session_lookup_values(state: dict[str, Any]) -> dict[str, str]:
    values = identity_selectors(state)
    for marker, (name, claim) in SESSION_TOKEN_MARKERS.items():
        candidates: set[str] = set()
        for cookie in state.get("cookies", []):
            if cookie.get("name") != name:
                continue
            value = cookie.get("value")
            if claim is not None:
                try:
                    part = value.split(".")[1]
                    value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get(claim)
                except (AttributeError, IndexError, ValueError, TypeError):
                    continue
            if isinstance(value, str) and value and len(value) <= 16384 and not any(c in value for c in "\r\n"):
                candidates.add(value)
        if len(candidates) == 1:
            values[marker] = candidates.pop()
    return values


def valid_lookup_body(body: Any) -> bool:
    return isinstance(body, dict) and len(body) <= 8 and all(
        isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,59}", key)
        and key.lower() not in MUTATION_BODY_KEYS and isinstance(value, str)
        and value in (*IDENTITY_MARKERS, *SESSION_TOKEN_MARKERS, *PUBLIC_LOOKUP_VALUES)
        for key, value in body.items()
    )


def observed_post_body(request: Any, state: dict[str, Any]) -> str | None:
    """Accept only empty or narrowly scoped JSON selectors for the lookup route."""
    raw = request.post_data
    if not raw:
        return None
    if len(raw) > 4096:
        raise ProbeURLRejected("unsupported_request_body")
    try:
        body = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise ProbeURLRejected("unsupported_request_body") from error
    if not isinstance(body, dict) or len(body) > 8:
        raise ProbeURLRejected("unsupported_request_body")
    selectors = session_lookup_values(state)
    for key, value in body.items():
        if not isinstance(value, str):
            raise ProbeURLRejected("unsupported_request_body")
        marker = next((marker for marker, selector in selectors.items() if selector == value), None)
        if marker is not None:
            body[key] = marker
        elif value not in PUBLIC_LOOKUP_VALUES:
            raise ProbeURLRejected("unsupported_request_body")
    if not valid_lookup_body(body):
        raise ProbeURLRejected("unsupported_request_body")
    return json.dumps(body, sort_keys=True)


def invalid_signature_token(token: str) -> str:
    """Preserve JWT structure while changing decoded signature bytes."""
    try:
        header, payload, signature = token.split(".")
        decoded = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
        if not decoded:
            raise ValueError
        decoded[0] ^= 1
        invalid = base64.urlsafe_b64encode(decoded).decode().rstrip("=")
        return header + "." + payload + "." + invalid
    except (ValueError, TypeError):
        raise AuthenticationRequired("Cannot construct an invalid-signature control for this token.") from None


async def request_probe(client: NewYorkerClient, probe: Probe, state: dict[str, Any], *, anonymous: bool = False, invalid_signature: bool = False) -> Any:
    url = resolve_probe_url(probe, state)
    if probe.method == "GET":
        return await client.get(url, token_cookie=None if anonymous else probe.token_cookie)
    body = json.loads(probe.body) if probe.body is not None else None
    if body is not None:
        selectors = session_lookup_values(state)
        if any(value in (*IDENTITY_MARKERS, *SESSION_TOKEN_MARKERS) and value not in selectors for value in body.values()):
            raise AuthenticationRequired("Subscription lookup session reference unavailable. Capture login again.")
        # Remove credential fields from the anonymous control, keeping account selectors.
        body = {key: selectors.get(value, value) for key, value in body.items()
                if not anonymous or value not in SESSION_TOKEN_MARKERS}
    authorization = None
    if (not anonymous or invalid_signature) and probe.authorization_marker is not None:
        value = session_lookup_values(state).get(probe.authorization_marker)
        if value is None:
            raise AuthenticationRequired("Subscription authorization unavailable. Capture login again.")
        if invalid_signature:
            value = invalid_signature_token(value)
        authorization = ("Bearer " if probe.authorization_scheme == "Bearer" else "") + value
    return await client.post_subscription_lookup(url, body=body, token_cookie=None if anonymous else probe.token_cookie,
                                                 authorization=authorization)


def validate_probe_url(url: str) -> None:
    try:
        validate_upstream_url(url, probe=True)
    except UnsafeUpstreamURL as error:
        raise ProbeURLRejected("unapproved_destination") from error
    parsed = urlsplit(url)
    parts = parsed.path.split("/")
    for index, encoded_part in enumerate(parts):
        part = unquote(encoded_part)
        if index and parts[index - 1] == "users" and part not in (*IDENTITY_MARKERS, "me"):
            raise ProbeURLRejected("identifier_in_path")
        if "{" in part or "}" in part:
            if part not in IDENTITY_MARKERS or not index or parts[index - 1] != "users" or parsed.hostname not in SITE_HOSTS:
                raise ProbeURLRejected("unsupported_account_template")
    if any(
        part not in IDENTITY_MARKERS and (len(part) > 60 or "@" in part
        or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", part))
        for part in map(unquote, parts)
    ):
        raise ProbeURLRejected("identifier_in_path")
    options = parse_qsl(parsed.query, keep_blank_values=True)
    if len(options) > 12 or len({key for key, _ in options}) != len(options):
        raise ProbeURLRejected("unsupported_query_parameters")
    for key, value in options:
        if key not in PUBLIC_QUERY_KEYS or not re.fullmatch(r"[A-Za-z0-9_,.-]{1,80}", value):
            raise ProbeURLRejected("unsupported_query_parameters")
        # Dotted fields/locales are legitimate; a JWT-shaped value is not.
        if len(value.split(".")) == 3 and all(len(part) > 8 for part in value.split(".")):
            raise ProbeURLRejected("unsupported_query_parameters")


def newyorker_scoped_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in SITE_HOSTS or any(
        key in BRAND_QUERY_KEYS and re.sub(r"[^a-z]", "", value.lower()) in NEWYORKER_BRANDS
        for key, value in parse_qsl(parsed.query)
    )


def safe_request_description(url: str, method: str, reason: str) -> dict[str, Any]:
    """Expose endpoint structure, not paths/queries containing account data."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "redacted"
        if not any(host == root or host.endswith("." + root) for root in ("newyorker.com", "condenast.com", "conde.io")):
            host = "other_host"
        else:
            for root in ("newyorker.com", "condenast.com", "conde.io"):
                if host.endswith("." + root):
                    prefix = host[:-(len(root) + 1)]
                    if prefix not in ("www", "id", "access", "api", "identity", "accounts", "auth", "subscriptions", "entitlements"):
                        host = "<redacted>." + root
                    break
        path = "/" + "/".join(
            part if part in DIAGNOSTIC_PATH_PARTS else "<redacted>"
            for part in parsed.path.split("/") if part
        )
        keys = sorted({key if key in PUBLIC_QUERY_KEYS else "<redacted>" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    except ValueError:
        host, path, keys = "redacted", "/<redacted>", []
    return {"method": method if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS") else "other",
            "host": host, "path": path, "query_keys": keys, "reason": reason}


def safe_response_fields(payload: Any) -> list[str]:
    fields: set[str] = set()
    def visit(value: Any, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:100]:
                name = re.sub(r"[^a-z]", "", key.lower())
                if name in DIAGNOSTIC_FIELDS:
                    fields.add(name)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:10]:
                visit(child, depth + 1)
    visit(payload, 0)
    return sorted(fields)


def safe_response_schema(payload: Any, *, fields: frozenset[str] = DIAGNOSTIC_FIELDS,
                         sources: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Traverse unknown containers under redacted names; never expose their values."""
    result: list[dict[str, Any]] = []
    visited = 0
    def visit(value: Any, pointer: str, depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > 8 or len(result) >= 60 or visited > 300:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:50]:
                if len(result) >= 60 or visited > 300:
                    break
                name = re.sub(r"[^a-z]", "", key.lower())
                path = pointer + "/" + (name if name in fields else "<redacted>")
                kind = ("object" if isinstance(child, dict) else "array" if isinstance(child, list)
                        else "boolean" if isinstance(child, bool) else "string" if isinstance(child, str)
                        else "null" if child is None else "number" if isinstance(child, (int, float)) else "other")
                item = {"path": path, "type": kind}
                if name in ("status", "subscriptionstatus", "entitlementstatus") and isinstance(child, str):
                    item["category"] = child if child in ("active", "inactive", "expired", "cancelled", "success", "ok") else "other"
                if sources is not None and isinstance(child, str):
                    item["value_source"] = sources.get(child, "unrecognized")
                result.append(item)
                visit(child, path, depth + 1)
        elif isinstance(value, list):
            for child in value[:2]:
                visit(child, pointer + "/*", depth + 1)
    visit(payload, "", 0)
    return result


REQUEST_DIAGNOSTIC_FIELDS = DIAGNOSTIC_FIELDS | frozenset({
    "userid", "id", "xid", "token", "accesstoken", "idtoken", "authtoken", "cneeauthtoken",
    "authorization", "clientid", "locale", "lang", "language", "fields", "format", "version",
})


def safe_request_body_description(request: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Describe rejected lookup bodies without revealing names or scalar values."""
    raw = request.post_data
    if not raw:
        return {"encoding": "empty", "schema": []}
    if not isinstance(raw, str) or len(raw) > 4096:
        return {"encoding": "unsupported", "schema": []}
    sources = {value: "identity_selector" if marker in IDENTITY_MARKERS else "session_token"
               for marker, value in session_lookup_values(state).items()}
    for cookie in state.get("cookies", []):
        value = cookie.get("value")
        if isinstance(value, str) and value and value not in sources:
            sources[value] = "access_token" if cookie.get("name") in TOKEN_COOKIES else "saved_cookie"
    try:
        payload = json.loads(raw)
        encoding = "json"
    except ValueError:
        headers = request.all_headers()
        if "application/x-www-form-urlencoded" not in headers.get("content-type", ""):
            return {"encoding": "unsupported", "schema": []}
        # Diagnostic only; form bodies are not replayed.
        payload = {key: value for key, value in parse_qsl(raw, keep_blank_values=True, max_num_fields=20)}
        encoding = "form"
    return {"encoding": encoding, "schema": safe_response_schema(payload, fields=REQUEST_DIAGNOSTIC_FIELDS, sources=sources)}


def identity_reference(state: dict[str, Any]) -> str | None:
    """Decode an identity hint for comparison, never as authentication proof."""
    candidates = set()
    for cookie in state.get("cookies", []):
        if cookie.get("name") != "CN_token_id":
            continue
        try:
            part = cookie["value"].split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            email = payload.get("email")
            if isinstance(email, str) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                candidates.add(email)
        except (KeyError, IndexError, ValueError, TypeError):
            pass
    return candidates.pop() if len(candidates) == 1 else None


def pointer_value(payload: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        return None
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(payload, list):
                if not part.isdecimal():
                    return None
                payload = payload[int(part)]
            else:
                payload = payload[part]
        return payload
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def record_is_newyorker(record: dict[str, Any]) -> bool:
    """Honor explicit publication labels within a brand-scoped CDS response."""
    for key, value in record.items():
        if key.lower() in ("brand", "site", "publication", "product") and isinstance(value, str):
            if re.sub(r"[^a-z]", "", value.lower()) not in NEWYORKER_BRANDS:
                return False
    return True


def matches(payload: Any, probe: Probe, reference: str | None = None) -> bool:
    if probe.brand_pointer is not None and pointer_value(payload, probe.brand_pointer) != probe.brand_value:
        return False
    value = pointer_value(payload, probe.pointer)
    if probe.record_status_key is not None:
        return isinstance(value, dict) and any(
            isinstance(record, dict) and record_is_newyorker(record)
            and record.get(probe.record_status_key) == "active"
            for record in value.values()
        )
    if reference is not None:
        return isinstance(value, str) and value == reference
    return type(value) is type(probe.expected) and value == probe.expected


def response_json(response: Any) -> Any:
    if response.status_code != 200 or "json" not in response.headers.get("content-type", "").lower():
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def anonymous_control(client: NewYorkerClient, probe: Probe, state: dict[str, Any], reference: str | None = None,
                            *, diagnostics: dict[str, Any] | None = None) -> str:
    async def attempt(invalid_signature: bool = False) -> str:
        label = "invalid_signature" if invalid_signature else "no_credentials"
        try:
            response = await request_probe(client, probe, state, anonymous=True, invalid_signature=invalid_signature)
        except AuthenticationRequired:
            # A sign-in redirect is a rejection; construction failures are unavailable.
            if invalid_signature:
                if diagnostics is not None:
                    diagnostics[label] = {"outcome": "unavailable"}
                return "unavailable"
            if diagnostics is not None:
                diagnostics[label] = {"outcome": "negative", "sign_in_redirect": True}
            return "negative"
        payload = response_json(response)
        if matches(payload, probe, reference):
            outcome = "positive"
        elif response.status_code in (401, 403) or isinstance(payload, (dict, list)):
            outcome = "negative"
        else:
            outcome = "unavailable"
        if diagnostics is not None:
            diagnostics[label] = {"outcome": outcome, "http_status": response.status_code,
                                  "json_object_or_array": isinstance(payload, (dict, list))}
        return outcome

    outcome = await attempt()
    # Some lookup handlers cannot process an absent Authorization header. A
    # corrupted JWT tests signature enforcement without sending a valid credential.
    if (outcome == "unavailable" and probe.method == "POST" and probe.authorization_marker is not None
            and probe.authorization_scheme == "Bearer"):
        return await attempt(invalid_signature=True)
    return outcome


async def verify_http_identity(
    client: NewYorkerClient, anonymous: NewYorkerClient, state: dict[str, Any], probe: Probe,
) -> VerificationResult:
    client._subscriber_verified = False
    reference = identity_reference(state)
    if reference is None:
        return VerificationResult("unverifiable", None, None, None, "identity_reference_unavailable")
    try:
        identity_response = await request_probe(client, probe, state)
        if identity_response.status_code == 401:
            return VerificationResult("authentication_required", False, None, False, "identity_rejected")
        identity_payload = response_json(identity_response)
        if not matches(identity_payload, probe, reference):
            return VerificationResult("unverifiable", None, None, None, "identity_signal_missing_or_changed")
        # A public JSON response with a matching identity is not sufficient.
        identity_control = await anonymous_control(anonymous, probe, state, reference)
        if identity_control == "positive":
            return VerificationResult("unverifiable", None, None, None, "identity_probe_is_public")
        if identity_control == "unavailable":
            return VerificationResult("unverifiable", None, None, None, "anonymous_control_unavailable")

        return VerificationResult("unverifiable", True, None, True, "no_explicit_subscriber_probe_observed")
    except AuthenticationRequired:
        return VerificationResult("authentication_required", False, None, False, "reconnect_required")
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        return VerificationResult("unverifiable", None, None, None, "upstream_unavailable_or_unsupported")


def explicitly_inactive(payload: Any, probe: Probe) -> bool:
    """Only recognized inactive fields/records can classify a non-subscriber."""
    value = pointer_value(payload, probe.pointer)
    inactive = {"inactive", "expired", "cancelled"}
    if probe.brand_pointer is not None and pointer_value(payload, probe.brand_pointer) != probe.brand_value:
        return False
    if probe.record_status_key is not None:
        if not isinstance(value, dict) or not value:
            return False
        records = [record for record in value.values() if isinstance(record, dict) and record_is_newyorker(record)]
        return bool(records) and len(records) == len(value) and all(
            isinstance(record.get(probe.record_status_key), str) and record[probe.record_status_key] in inactive
            for record in records
        )
    return probe.expected is True and value is False or probe.expected == "active" and isinstance(value, str) and value in inactive


async def verify_http_session(
    client: NewYorkerClient, anonymous: NewYorkerClient, state: dict[str, Any], spec: VerificationSpec,
) -> VerificationResult:
    client._subscriber_verified = False
    identity_result = await verify_http_identity(client, anonymous, state, spec.identity)
    if identity_result.authenticated is not True:
        return identity_result
    try:
        subscription_response = await request_probe(client, spec.subscriber, state)
        if subscription_response.status_code == 401:
            return VerificationResult("authentication_required", False, None, False, "subscriber_probe_rejected")
        subscription_payload = response_json(subscription_response)
        if not matches(subscription_payload, spec.subscriber):
            if explicitly_inactive(subscription_payload, spec.subscriber):
                return VerificationResult("not_subscribed", True, False, True, "active_subscription_absent")
            return VerificationResult("unverifiable", True, None, True, "subscriber_signal_missing_or_changed")
        control_diagnostics: dict[str, Any] = {}
        subscription_control = await anonymous_control(anonymous, spec.subscriber, state, diagnostics=control_diagnostics)
        if subscription_control == "positive":
            return VerificationResult("unverifiable", True, None, True, "subscriber_probe_is_public", control_diagnostics)
        if subscription_control == "unavailable":
            return VerificationResult("unverifiable", True, None, True, "anonymous_control_unavailable", control_diagnostics)
        client._subscriber_verified = True
        return VerificationResult("verified", True, True, True, "live_identity_and_subscriber_signals", control_diagnostics)
    except AuthenticationRequired:
        return VerificationResult("authentication_required", False, None, False, "reconnect_required")
    except (UpstreamRequestFailed, UnsafeUpstreamURL):
        return VerificationResult("unverifiable", None, None, None, "upstream_unavailable_or_unsupported")


def validate_probe(probe: Probe, *, subscriber: bool = False) -> None:
    validate_probe_url(probe.url)
    if probe.method not in ("GET", "POST") or probe.method == "POST" and not read_only_subscription_url(probe.url):
        raise ProbeURLRejected("unsupported_method")
    if probe.body is not None:
        if probe.method != "POST" or not isinstance(probe.body, str) or len(probe.body) > 4096:
            raise ProbeURLRejected("unsupported_request_body")
        body = json.loads(probe.body)
        if not valid_lookup_body(body):
            raise ProbeURLRejected("unsupported_request_body")
    if not probe.pointer.startswith("/") or len(probe.pointer) > 512:
        raise ValueError("Invalid verification JSON pointer.")
    if probe.authorization_marker is not None:
        if (probe.authorization_marker not in SESSION_TOKEN_MARKERS or probe.authorization_scheme not in ("Bearer", "raw")
                or probe.token_cookie is not None or probe.method != "POST" or not read_only_subscription_url(probe.url)):
            raise ValueError("Unsupported subscription authorization reference.")
    elif probe.authorization_scheme != "Bearer":
        raise ValueError("Authorization scheme needs a session reference.")
    if probe.record_status_key is not None:
        if (not isinstance(probe.record_status_key, str) or probe.record_status_key.lower() != "status"
                or [part.lower() for part in probe.pointer.split("/")[-2:]] != ["subscriptions", "cds"]
                or not read_only_subscription_url(probe.url) or probe.method != "POST" or probe.expected != "active"
                or probe.body is None or json.loads(probe.body).get("brandSlug") not in PUBLIC_LOOKUP_VALUES):
            raise ValueError("Unsupported subscription collection predicate.")
    if probe.token_cookie is not None and probe.token_cookie not in TOKEN_COOKIES:
        raise ValueError("Unsupported verification token source.")
    if probe.expected is not True and probe.expected != "active":
        raise ValueError("Unsupported subscriber predicate.")
    if probe.brand_pointer is not None:
        if not isinstance(probe.brand_pointer, str) or not probe.brand_pointer.startswith("/") or not isinstance(probe.brand_value, str):
            raise ValueError("Invalid subscriber brand guard.")
        if re.sub(r"[^a-z]", "", probe.brand_value.lower()) not in NEWYORKER_BRANDS:
            raise ValueError("Subscriber brand must identify New Yorker.")
    elif probe.brand_value is not None or subscriber and not newyorker_scoped_url(probe.url):
        raise ValueError("Global subscriber probes need an explicit New Yorker brand guard.")


def read_spec(path: Path) -> VerificationSpec:
    try:
        if path.is_symlink():
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != 1:
            raise ValueError
        spec = VerificationSpec(Probe(**data["identity"]), Probe(**data["subscriber"]), data["user_agent"])
        validate_probe(spec.identity)
        validate_probe(spec.subscriber, subscriber=True)
        if not isinstance(spec.user_agent, str) or len(spec.user_agent) > 512 or "\n" in spec.user_agent or "\r" in spec.user_agent:
            raise ValueError
        return spec
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise SessionError("Verification probes are missing or invalid. Run http_auth discover first.") from error


def field_probes(payload: Any, url: str, token_cookie: str | None, reference: str) -> tuple[list[Probe], list[Probe]]:
    """Recognize explicit account/subscriber fields in an observed server response."""
    identities: list[Probe] = []
    subscribers: list[Probe] = []
    site_scoped = newyorker_scoped_url(url)

    def visit(value: Any, pointer: str, scoped: bool, subscription: bool, depth: int, brand_guard: tuple[str, str] | None = None) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            # CDS records are keyed by private account/record identifiers. Save
            # the fixed collection path and status field, never those identifiers.
            if (scoped and subscription and read_only_subscription_url(url)
                    and [part.lower() for part in pointer.split("/")[-2:]] == ["subscriptions", "cds"]):
                keys = {key for record in value.values() if isinstance(record, dict) and record_is_newyorker(record)
                        for key, status in record.items() if key.lower() == "status" and status == "active"}
                for key in sorted(keys):
                    subscribers.append(Probe(url, pointer, token_cookie, "active", record_status_key=key))
                return
            for key, item in value.items():
                if key.lower() in ("brand", "site", "publication", "product") and isinstance(item, str):
                    scoped = re.sub(r"[^a-z]", "", item.lower()) in NEWYORKER_BRANDS
                    brand_guard = (pointer + "/" + key, item) if scoped else None
                    break
            for key, item in value.items():
                # Schema fields only: skip dynamic keys that might contain PII.
                known_brand_key = re.sub(r"[^a-z]", "", key.lower()) in NEWYORKER_BRANDS
                known_product_key = key in ("newyorker.com:basic", "newyorker.com:print")
                if not known_brand_key and not known_product_key and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,59}", key):
                    continue
                child = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
                name = re.sub(r"[^a-z]", "", key.lower())
                if name in ("email", "emailaddress") and item == reference:
                    identities.append(Probe(url, child, token_cookie))
                if scoped and isinstance(item, bool) and (
                    name in ("issubscriber", "isactivesubscriber", "hasactivesubscription")
                    or subscription and name in ("active", "isactive")
                ):
                    subscribers.append(Probe(url, child, token_cookie, brand_pointer=brand_guard[0] if brand_guard else None,
                                             brand_value=brand_guard[1] if brand_guard else None))
                if scoped and subscription and name == "status" and item == "active":
                    subscribers.append(Probe(url, child, token_cookie, "active", brand_pointer=brand_guard[0] if brand_guard else None,
                                             brand_value=brand_guard[1] if brand_guard else None))
                visit(item, child, scoped, subscription or name in ("subscription", "subscriptions", "entitlement", "entitlements"), depth + 1, brand_guard)
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                visit(item, pointer + "/" + str(index), scoped, subscription, depth + 1, brand_guard)

    visit(payload, "", site_scoped, False, 0)
    return identities, subscribers


def discover_browser_probes(state: dict[str, Any], cdp_url: str, timeout_ms: int, *, capture_path: Path | None = None) -> tuple[list[Probe], list[Probe], str, dict[str, Any]]:
    """Observe account lookups, report redacted metadata, and optionally save a private capture."""
    from playwright.sync_api import sync_playwright
    from app.newyorker.auth import verify_browser_login

    reference = identity_reference(state)
    if reference is None:
        raise SessionError("Saved session has no usable identity reference. Capture login again.")
    identities: list[Probe] = []
    subscribers: list[Probe] = []
    user_agent = "NewYorkerAudioBackend/0.1"
    diagnostics = {"json_responses": 0, "approved_json_responses": 0, "unsupported_probe_urls": 0,
                   "unsupported_authorization": 0, "unsupported_methods": 0, "unsupported_request_bodies": 0, "responses": []}
    subscription_capture: dict[str, Any] | None = None
    tokens = {cookie["name"]: cookie["value"] for cookie in state["cookies"] if cookie.get("name") in TOKEN_COOKIES}

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
        context = browser.new_context(storage_state=state, accept_downloads=False)

        def observe(response: Any) -> None:
            nonlocal user_agent, subscription_capture
            try:
                request = response.request
                if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
                    return
                diagnostics["json_responses"] += 1
                reason = "approved"
                try:
                    template_url = template_account_url(request.url, state)
                    method = request.method
                    if method == "POST" and not read_only_subscription_url(template_url) or method not in ("GET", "POST"):
                        raise ProbeURLRejected("unsupported_method")
                    post_body = observed_post_body(request, state) if method == "POST" else None
                    prototype = Probe(template_url, "/placeholder", method=method, body=post_body)
                    validate_probe(prototype)
                except ProbeURLRejected as error:
                    reason = str(error)
                    if reason == "unsupported_request_body":
                        diagnostics["unsupported_request_bodies"] += 1
                    elif reason != "unsupported_method":
                        diagnostics["unsupported_probe_urls"] += 1
                if reason == "unsupported_method":
                    diagnostics["unsupported_methods"] += 1
                description = safe_request_description(request.url, request.method, reason)
                if reason == "unsupported_request_body":
                    description["request_body"] = safe_request_body_description(request, state)
                if len(diagnostics["responses"]) < 20 and description not in diagnostics["responses"]:
                    diagnostics["responses"].append(description)
                # Inspect first-party response schemas only; never print account values.
                host = urlsplit(request.url).hostname or ""
                first_party = any(host == root or host.endswith("." + root) for root in ("newyorker.com", "condenast.com", "conde.io"))
                if not first_party:
                    return
                body = response.body()
                if len(body) > 1_000_000:
                    return
                payload = json.loads(body)
                if capture_path is not None and read_only_subscription_url(request.url):
                    subscription_capture = {"version": 1, "request": {"url": request.url, "method": request.method,
                                            "body": request.post_data,
                                            "authorization": request.all_headers().get("authorization")}, "response": payload}
                description["fields"] = safe_response_fields(payload)
                description["schema"] = safe_response_schema(payload)
                if reason != "approved":
                    return
                diagnostics["approved_json_responses"] += 1
                request_headers = request.all_headers()
                authorization = request_headers.get("authorization")
                token_cookie = None
                authorization_marker = None
                authorization_scheme = "Bearer"
                if authorization:
                    token_cookie = next((name for name, token in tokens.items() if authorization == "Bearer " + token), None)
                    if token_cookie is None and method == "POST" and read_only_subscription_url(template_url):
                        values = session_lookup_values(state)
                        authorization_marker = next((marker for marker, value in values.items()
                                                     if marker in SESSION_TOKEN_MARKERS and authorization == "Bearer " + value), None)
                        if authorization_marker is None:
                            authorization_marker = next((marker for marker, value in values.items()
                                                         if marker in SESSION_TOKEN_MARKERS and authorization == value), None)
                            if authorization_marker is not None:
                                authorization_scheme = "raw"
                    if token_cookie is None and authorization_marker is None:
                        diagnostics["unsupported_authorization"] += 1
                        description["reason"] = "unsupported_authorization"
                        description["authorization_scheme"] = "Bearer" if authorization.startswith("Bearer ") else "other"
                        return
                found_identity, found_subscriber = field_probes(payload, template_url, token_cookie, reference)
                identities.extend(replace(probe, method=method, body=post_body, authorization_marker=authorization_marker, authorization_scheme=authorization_scheme) for probe in found_identity)
                subscribers.extend(replace(probe, method=method, body=post_body, authorization_marker=authorization_marker, authorization_scheme=authorization_scheme) for probe in found_subscriber)
                if found_identity or found_subscriber:
                    user_agent = request_headers.get("user-agent", user_agent)
            except Exception:
                # Browser response URLs/bodies/errors may contain account data.
                return

        context.on("response", observe)
        try:
            verify_browser_login(context, timeout_ms)
            # Pump late first-party response callbacks without waiting on ads.
            page = context.new_page()
            page.wait_for_timeout(1000)
            page.close()
        finally:
            context.close()
        # Disconnect from Chrome; do not close the manually launched browser.
    if capture_path is not None and subscription_capture is not None:
        save_session(capture_path, subscription_capture)
        diagnostics["private_subscription_capture_saved"] = True
    return list(dict.fromkeys(identities))[:4], list(dict.fromkeys(subscribers))[:4], user_agent, diagnostics


async def discover_spec(state: dict[str, Any], identities: list[Probe], subscribers: list[Probe], user_agent: str) -> tuple[VerificationSpec | None, VerificationResult]:
    result = VerificationResult("unverifiable", None, None, None, "no_explicit_subscriber_and_identity_probes_observed")
    for identity in identities:
        if not subscribers:
            async with NewYorkerClient(state, user_agent=user_agent) as client, NewYorkerClient({"cookies": [], "origins": []}, user_agent=user_agent) as anonymous:
                result = await verify_http_identity(client, anonymous, state, identity)
            if result.authenticated is True or result.status == "authentication_required" or result.reason in (
                "upstream_unavailable_or_unsupported", "anonymous_control_unavailable",
            ):
                return None, result
            continue
        for subscriber in subscribers:
            spec = VerificationSpec(identity, subscriber, user_agent)
            async with NewYorkerClient(state, user_agent=user_agent) as client, NewYorkerClient({"cookies": [], "origins": []}, user_agent=user_agent) as anonymous:
                result = await verify_http_session(client, anonymous, state, spec)
            if result.status == "verified":
                return spec, result
            if result.status == "authentication_required" or result.reason in (
                "upstream_unavailable_or_unsupported", "anonymous_control_unavailable",
            ):
                return None, result
            if result.reason in ("identity_probe_is_public", "identity_signal_missing_or_changed"):
                break
    return None, result


def main(argv: list[str] | None = None) -> int:
    from app.newyorker.auth import local_cdp_url, positive_seconds

    parser = argparse.ArgumentParser(description="Verify subscriber-session reuse through httpx.")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="Observe actual account requests in local Chrome, then verify them with httpx.")
    discover.add_argument("--cdp-url", type=local_cdp_url, default="http://127.0.0.1:9222")
    discover.add_argument("--timeout-seconds", type=positive_seconds, default=30)
    commands.add_parser("check", help="Recheck the saved probes with httpx only; no browser is needed.")
    arguments = parser.parse_args(argv)

    try:
        from app.config import get_settings
        settings = get_settings()
        path = settings.session_state_path.expanduser()
        if not path.exists():
            print(json.dumps(asdict(VerificationResult("missing", False, None, False, "capture_login_first"))))
            return 2
        state = load_session(path)
        spec_path = settings.http_verification_path.expanduser()
        if arguments.command == "discover":
            identities, subscribers, user_agent, diagnostics = discover_browser_probes(state, arguments.cdp_url, arguments.timeout_seconds * 1000, capture_path=settings.discovery_capture_path.expanduser())
            spec, result = asyncio.run(discover_spec(state, identities, subscribers, user_agent))
            if spec is not None:
                save_session(spec_path, {"version": 1, **asdict(spec)})
            output = {**asdict(result), "identity_probe_candidates": len(identities), "subscriber_probe_candidates": len(subscribers),
                      "discovery": diagnostics}
        else:
            spec = read_spec(spec_path)

            async def check() -> VerificationResult:
                async with NewYorkerClient(state, user_agent=spec.user_agent) as client, NewYorkerClient({"cookies": [], "origins": []}, user_agent=spec.user_agent) as anonymous:
                    return await verify_http_session(client, anonymous, state, spec)

            result = asyncio.run(check())
            output = asdict(result)
        save_session(settings.http_verification_result_path.expanduser(), output)
        print(json.dumps(output))
        return 0 if result.status == "verified" else 2
    except SessionError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("HTTP verification cancelled. Saved state was not changed.", file=sys.stderr)
        return 130
    except Exception:
        # Never print raw upstream/browser exceptions, tokens, URLs, or bodies.
        print("HTTP verification could not finish. Check local Chrome, session configuration, and network access. Saved state was not changed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
