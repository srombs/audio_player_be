# New Yorker Audio Backend — Initial Implementation Plan

## Goal

Build a small Python backend for a native Android app that browses and plays New Yorker narrated articles. Authentication is a requirement of the first milestone: the backend must use a verified New Yorker subscriber session, even when some article metadata or MP3 URLs are publicly accessible.

Keep the architecture small: FastAPI for the API, httpx for ordinary retrieval, Playwright for interactive login, and direct CDN audio playback from Android. Defer databases, queues, remote browser authentication UI, and audio proxying.

## Implementation order

### 1. Create the project skeleton

- Initialize Git and use `pyproject.toml` with `uv`.
- Add FastAPI, httpx, Pydantic, Playwright, and pytest.
- Create separate modules for configuration, HTTP retrieval, authentication, parsing, audio resolution, and application models.
- Implement `GET /health`.
- Exclude session files and secrets from Git immediately.

### 2. Establish an authenticated browser session

- Add a local interactive login command, such as `python -m app.newyorker.auth`.
- Open a headed browser so the user can complete New Yorker sign-in.
- Save browser session state to a private, configurable location.
- Do not store passwords or automate credential entry.
- Treat persisted session state as a secret; never log or expose its contents.

### 3. Verify subscriber access through httpx

- Load the saved cookies into the HTTP client, preserving domains, paths, and expiration.
- Identify an upstream signal that confirms authentication and subscriber access.
- Observe account GET requests and the narrowly scoped subscription POST lookup in local Chrome; store account selectors as session-derived templates.
- Compare live identity and explicit New Yorker subscriber signals with cookie-free controls. Keep article retrieval blocked until both pass.
- Verify available identity probes independently when subscriber discovery is incomplete. Inspect rejected lookup body encoding and redacted nested response structure to adapt unsupported upstream layouts; live subscriber verification remains pending until discovery reports `verified`.
- Do not treat a saved session file or an article returning HTTP 200 as proof of authentication.
- Verify that the authenticated browser session also works for ordinary HTTP requests.
- Investigate any additional session requirements if browser access succeeds but httpx access does not.

Live checkpoint (2026-10-08): **Step 3 passed.** Discovery reported `status: verified`, `authenticated: true`, `subscriber: true`, and `http_session_reusable: true`. The account identity and explicit active New Yorker subscription signals both worked through httpx. The subscription endpoint returned HTTP 400 without credentials and HTTP 401 with an invalid JWT signature, completing the negative control.

The subscription probe uses `brandSlug: "the-new-yorker"`, the fixed `subscriptions.CDS` collection path, and a session-derived authorization reference. Saved probe rules exclude private record keys and token values. They are stored privately in `data/http-verification.json`; the latest redacted result is in `data/http-verification-last-result.json`. Recheck with `uv run python -m app.newyorker.http_auth check` without Chrome. Verification must continue to pass before article operations.

This first technical checkpoint and step 4 are complete. Next: step 5, the authenticated article-to-audio pipeline.

### 4. Implement session status and expiration handling

Implemented:

- `GET /auth/status` reports `missing`, `verified`, `reconnect_required`, `not_subscribed`, or `unverifiable`, with safe identity/subscriber flags, check time, reason, and recovery action. Status requests return HTTP 200 and exclude account values and probe diagnostics.
- A shared session service owns the subscriber client, coalesces concurrent checks, and caches verification for 60 seconds by default. It never treats a saved CLI report as authorization.
- Session/probe replacement invalidates the cache on the next operation, so manual login capture requires no server restart. File changes during verification or article retrieval cannot grant access.
- `SessionService.get_article` requires fresh verified subscriber access. Authentication rejection or a sign-in redirect immediately invalidates it; other upstream failures force a fresh check without claiming expiration.
- Future article routes receive distinct backend session errors: HTTP 503 for missing/rejected/unverifiable sessions and HTTP 403 for explicit subscription inactivity. Android app authentication remains separate.
- Login remains manual for V1. Reuse the saved probes after capture; rediscover only when upstream endpoint/schema changes require it.
- Offline tests cover status transitions, caching, concurrency, replacement, failure classification, access gating, API errors, safe responses, and client cleanup. Real upstream verification of the new API remains a local-host check.

### 5. Build the authenticated archive-to-audio pipeline

Start with magazine discovery so the app can browse issues, then their articles:

- Implemented an authenticated archive sync and issue index (`app.newyorker.archive`) storing canonical dates, date labels, and URLs. Default sync loads the archive listing and resolves its current-issue alias when present; optional historical sync follows observed Previous issue links with atomic checkpoints and resume support. The initial live archive sync returned the 2026 issues.
- Collect the issue list with `uv run python -m app.newyorker.archive sync`; collect older issues with `sync --history`. Read/filter the local index with `list --year 2026`.
- Implemented issue table-of-contents extraction (`app.newyorker.issue`): `sync --latest` resolves the current magazine page, saves deduplicated article/card metadata and a private HTML capture, and updates the issue index. `list` reads the saved list. Offline extraction and authentication-failure tests pass; live extraction is pending a local run.
- Next, determine narration availability and playable URLs from actual article/player metadata. A table-of-contents Listen marker is only a hint; missing markers remain unknown. Do not assume all issues or articles have audio.

Then build article/audio resolution:

- Implemented JSON application-state extraction, narration/source models, a credential-free bounded player/CDN client, and MP3 byte-range verification with offline tests. Live captures confirm primary narration under `transformed.article.relatedAudio` and an HTTP 302 permalink redirect directly to the MP3, including its non-guessable filename suffix.
- `uv run python -m app.newyorker.audio resolve <article-url>` requires verified subscriber access, parses primary article metadata, follows the observed narration redirect, checks the audio ID, and verifies a bounded MP3 byte range. It returns clean article/audio JSON and saves the latest successful result privately. Explicitly unnarrated articles return `audio: null`; resolution failures return errors. End-to-end local resolution succeeded on 2026-10-08: verified MP3 bytes, `audio/mpeg`, duration 1,630,000 ms (27 minutes 10 seconds), and a direct CDN source. The saved result records verification at 19:57:46 UTC.
- `uv run python -m app.newyorker.audio_capture` remains a private diagnostic command preserving the article, redirect destinations, and response/failure details. No subscriber credentials are sent to the player/CDN; article bodies and private state are not exported in resolution results.
- Select a known narrated article and retrieve it using the verified session.
- Extract `window.__PRELOADED_STATE__` without executing JavaScript.
- Normalize article metadata and internal `relatedAudio` metadata into explicit Pydantic models.
- Inspect the embed/player metadata to discover the actual MP3 resolution process.
- Isolate audio resolution in its own module; do not guess filename suffixes or construct unverified MP3 URLs.
- Verify a resolved audio URL with a small range request.
- Save sanitized fixtures and test parsing and audio resolution offline.
- Make the pipeline runnable as a diagnostic command that accepts an article URL and returns clean JSON.
- Restrict fetch destinations to the expected hosts and handle redirects carefully.

### 6. Expose magazine and article lists, then Dockerize

- Implemented `GET /issues` (newest-first saved magazine index, optional `year`) and `GET /issues/{issue_date}/articles` (per-issue metadata and article list). These are the app's initial browsing endpoints, as requested.
- Issue records include an optional `cover_image_url`, extracted from archive-card images or the issue page's matching full cover. Existing records remain readable and acquire covers on sync. Cached article-list responses use current issue metadata from the index. The initial browsing API was live-tested by the user and returned the expected data.
- Both endpoints use the shared verified-subscriber gate. Existing article lists are read locally; a known issue with no saved list is fetched and saved on demand. Unknown issue dates, missing/corrupt local data, upstream failures, and subscriber rejection return explicit safe errors. Audio resolution is separate from list browsing.
- API tests cover filtering/order, grouped metadata, cached and on-demand reads, invalid dates, missing/corrupt data, and subscriber/upstream failures. A live API check remains a local-host step.
- URL/ID-based article and playback endpoints can be added later when needed by the app.
- Keep subscriber cookies on the backend; Android receives article metadata and the resolved audio URL.
- Stream audio directly from the CDN using Android Media3.
- Dockerize the working service and persist session state across container replacement.
- Define how locally established sessions reach the container before adding container-based interactive login. Including Chromium in the image alone does not provide a usable headed login flow.

## First milestone

```text
Interactive subscriber login
    ↓
Persisted session
    ↓
Verified authenticated httpx access
    ↓
Article parsing
    ↓
Resolved MP3 URL
    ↓
Clean JSON
```

The milestone is complete when:

1. Interactive login establishes a verified subscriber session.
2. Ordinary HTTP requests reuse that session successfully.
3. A known narrated article produces normalized metadata and a verified MP3 URL.
4. An article without narration is handled cleanly.
5. Missing or expired sessions stop article operations with a reconnect error.
6. A backend restart can reuse a still-valid saved session.
7. Parser and audio-resolver tests run offline using sanitized fixtures.
8. Session secrets are neither committed nor exposed through logs or API responses.

Authentication is part of the foundation, not a later phase.
