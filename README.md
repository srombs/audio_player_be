# New Yorker Audio Backend

Python backend for a native Android app that browses and plays New Yorker narrated articles. Subscriber authentication is required for the article pipeline. See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the implementation sequence.

## Local setup

Install Python 3.12 or later and `uv`, then run:

```sh
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Check the service:

```sh
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

API documentation is available at `http://127.0.0.1:8000/docs`.

## Docker

Install Docker with a running engine, then build from the repository directory:

```sh
docker build -t newyorker-audio-backend .
```

The image installs Python 3.12 and runtime dependencies from `uv.lock`, and starts
Uvicorn on `0.0.0.0:8000` without development reload. Its health check tests
`/health`; it does not verify subscriber access. The build context excludes local
data, credentials, environment files, and browser profiles. The image includes
the Playwright Python dependency but no browser installation: perform interactive
login/capture and verification discovery on your local machine.

For local testing, mount your existing `data/` directory, which holds the saved
session, verification rules, magazine index, and article caches:

```sh
mkdir -p data
chmod 700 data
docker run -d --name newyorker-audio-backend \
  --user "$(id -u):$(id -g)" \
  --publish 8000:8000 \
  --mount "type=bind,source=$PWD/data,target=/app/data" \
  newyorker-audio-backend
```

Using your host UID/GID lets the container read mode-600 session files and write
caches without changing their ownership. The image defaults to UID/GID 10001;
on a Linux server, either give that user ownership of the mounted directory and
files, or use `--user` with the UID/GID of their owner. Keep the data directory at
mode 700 and the credential files at mode 600. The mount is writable because the
backend saves newly fetched issue lists and CLI sync results there.

For a remote server, transfer `newyorker-session.json` and
`http-verification.json` securely into its mounted data directory. You can also
transfer the magazine index and article caches, or create them with the sync
commands below. Copying saved credentials does not guarantee acceptance from a
different network; check access from inside the container:

```sh
docker exec newyorker-audio-backend python -m app.newyorker.http_auth check
docker exec newyorker-audio-backend python -m app.newyorker.archive sync
docker exec newyorker-audio-backend python -m app.newyorker.issue sync --latest
```

Open `http://localhost:8000/health` or `/docs` on the host. From an Android phone
on the same network, use `http://<host-LAN-IP>:8000`. The published port binds to
all host interfaces; use it on a trusted testing network. The current API has no
client authentication, so an internet deployment needs an access-controlled HTTPS
entry point before exposing these subscriber-backed endpoints.

Replace expired session files in the mounted directory after capturing a new
session locally; the backend detects replacements on its next access check.
Mounted data persists when the container is replaced. Configuration can be
passed using Docker `--env` options with the existing `NY_AUDIO_` variables.

## Configuration

Settings use the `NY_AUDIO_` environment-variable prefix. `NY_AUDIO_SESSION_STATE_PATH` defaults to `data/newyorker-session.json`; set it to another private, persistent path when needed. Configuration reads process environment variables rather than loading `.env` automatically.

Session state must stay out of Git, logs, and API responses. The default `data/` directory and common session filenames are ignored by Git. Use an ignored location for custom session paths too.

## Tests

```sh
uv run pytest
```

Offline browser tests use synthetic pages and require installed Chromium:

```sh
NY_AUDIO_BROWSER_TESTS=1 uv run pytest tests/test_auth_browser.py
```

## Interactive login (step 2)

Install the browser and make the dedicated session directory private:

```sh
uv sync
uv run playwright install chromium
mkdir -p data
chmod 700 data
uv run python -m app.newyorker.auth login
```

Complete New Yorker sign-in yourself in the visible browser. Select **Keep me signed in** if available. Leave the browser open and return to the terminal: press Enter to verify and save, or type `cancel` to leave the existing session untouched. Ctrl-C and terminal EOF also cancel. No passwords are entered or saved by the command.

The command checks the rendered account profile for a populated email input or read-only email text beside an explicit email label, together with a visible Sign out control. Sign out alone is insufficient because the public account-page HTML contains it. Unrelated contact emails and hidden fields do not count. It then restores the captured New Yorker state in a fresh browser context and repeats the check before saving.

Verification errors identify whether the existing browser or the restored session failed, and report only boolean signals (account page, Sign out, email input, labelled email text). No email addresses or cookie values are printed. If verification fails, the existing session is preserved. An existing-browser failure may indicate a changed profile layout; a restored-session failure means the current browser passed but the captured New Yorker state did not restore login.

Only cookies and HTTPS storage origins belonging to `newyorker.com` and its subdomains are retained, including IndexedDB. Google and other third-party session state is excluded. Writes use a mode-600 temporary file and atomic replacement. The session directory must have mode 700; existing directories are not automatically chmodded. For a custom `NY_AUDIO_SESSION_STATE_PATH`, create a dedicated private directory first and keep that location out of Git.

To check that saved state still restores browser login without changing the file:

```sh
uv run python -m app.newyorker.auth check
```

If needed, use an installed browser instead of Playwright Chromium:

```sh
uv run python -m app.newyorker.auth login --browser-channel chrome
uv run python -m app.newyorker.auth check --browser-channel chrome
```

Each navigation and account verification defaults to a 30-second timeout, configurable with `--timeout-seconds`. Manual sign-in has no time limit. Re-run `login` to replace an expired session.

### If Google says the browser may not be secure

Google may reject sign-in from a browser controlled through automation, even when credentials are entered manually ([Google guidance](https://support.google.com/accounts/answer/7675428?hl=en)). `--browser-channel chrome` still launches Chrome through Playwright, so switching channels does not guarantee that Google sign-in will work. If your account supports New Yorker email/password sign-in, that is an alternative.

For Google sign-in, try completing login in a separate Chrome window before attaching Playwright. From the repository directory, create a private, dedicated browser profile and launch installed Chrome yourself (macOS):

```sh
mkdir -p data/login-browser-profile
chmod 700 data data/login-browser-profile
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/data/login-browser-profile" \
  "https://www.newyorker.com/account/sign-in"
```

Sign in manually and make sure you reach your New Yorker account. Do not run capture while Google login is still in progress. Leave that Chrome window open, then run this in a second terminal from the repository directory:

```sh
uv run python -m app.newyorker.auth capture
```

The command attaches to the local debugging endpoint, checks the already established New Yorker login, filters out third-party session state, verifies restoration in a fresh context, and saves atomically. It does not close your Chrome window or its original context. A different loopback port can be supplied with `--cdp-url http://127.0.0.1:9223`.

This is an alternative to Playwright-launched login, not a guaranteed remedy for Google's rejection. If Google still rejects the manually launched window, use a supported New Yorker direct sign-in method; the backend must not proceed without verified login.

Close the dedicated Chrome window after capture. Its temporary profile contains browser session secrets, including any Google session established during manual sign-in; delete `data/login-browser-profile` after closing Chrome if you do not want to retain it. The exported backend session contains only New Yorker state. Keep the debugging endpoint local and use this dedicated profile rather than your everyday Chrome profile. [Chrome requires a non-default profile for remote debugging](https://developer.chrome.com/blog/remote-debugging-port).

**Browser login is separate from subscriber entitlement.** New Yorker accounts can exist without an active linked subscription ([New Yorker FAQ](https://www.newyorker.com/about/faq)). Step 3 must verify subscriber access and reuse through httpx before article operations can proceed. Saving/restoring storage state follows the [Playwright authentication documentation](https://playwright.dev/python/docs/auth).

## HTTP session reuse and subscriber verification (step 3)

Step 3 uses `httpx.AsyncClient` with the saved browser cookies, preserving their domains, paths, secure flags, expiration, and duplicate names. Host-only cookies remain host-only. Identity bearer tokens are used only when the actual observed account request uses the saved access token. Fetches and redirects are limited to approved HTTPS hosts, and bearer tokens cannot follow cross-host redirects.

First discover the real account API requests. Keep the dedicated Chrome window from the capture flow running with local debugging enabled, then run:

```sh
uv run python -m app.newyorker.http_auth discover
```

Discovery restores saved state in a temporary browser context, verifies the rendered account identity, and observes first-party account JSON responses. Account IDs in observed `/users/<id>` paths are replaced with references to identity claims in the saved session; those claims select the request and do not prove authentication. It recognizes an account email and explicit active-subscriber fields, such as `isActiveSubscriber` or an active subscription status. Responses from global Condé Nast services also require a New Yorker brand marker. It does not infer subscriber access from article HTTP 200 responses, the presence of cookies, decoded JWT claims, or access-product strings in cookies.

Candidate requests are replayed through httpx with the saved session and with a separate cookie-free control. Both the identity and New Yorker subscription checks must succeed with credentials and fail to prove the same signals anonymously. Only then are the discovered endpoint/schema rules saved privately to `data/http-verification.json`. The saved rules contain no account email, token values, or raw API responses. The browser session file is not modified.

Subsequent checks use httpx only:

```sh
uv run python -m app.newyorker.http_auth check
```

Successful output includes:

```json
{
  "status": "verified",
  "authenticated": true,
  "subscriber": true,
  "http_session_reusable": true,
  "reason": "live_identity_and_subscriber_signals"
}
```

Other results distinguish missing state, rejected authentication, a signed-in non-subscriber, and unverifiable evidence. Only `verified` exits with code 0; unsuccessful checks exit with code 2. Network failures and generic HTTP 403 responses do not automatically mean an expired login. A verified client can retrieve articles; article retrieval through `get_article` remains blocked until verification succeeds on that client instance.

`NY_AUDIO_HTTP_VERIFICATION_PATH` configures the private probe file location. Completed discovery and check results, including redacted diagnostics, are saved separately to `data/http-verification-last-result.json` (configurable with `NY_AUDIO_HTTP_VERIFICATION_RESULT_PATH`). This report records the result and does not grant access. Re-run discovery if upstream endpoint/schema changes invalidate the saved rules. Discovery accepts a limited set of public query options, including brand/site, client ID, locale, and requested fields. Query parameters carrying account identifiers, credentials, unknown options, or duplicate keys remain blocked. Explicit New Yorker brand query options can scope a global provider's subscriber response.

Discovery also supports the observed New Yorker `/api/user/v2/subscriptions` lookup route, optionally under one service prefix, using POST with an empty body or a narrowly validated JSON body containing account selectors, references to saved session tokens (including the nested entitlement token), and public New Yorker selectors. Observed subscription authorization headers can reference a saved session token, including an ID or entitlement token. These headers are limited to the subscription lookup route and omitted from the initial anonymous control. If the no-credential control is unavailable, a second control can use the observed JWT with deliberately altered signature bytes. It runs without session cookies or valid token credentials. A matching active subscription response blocks verification; server errors remain inconclusive. Safe control HTTP statuses are recorded in the result file. Credential fields are removed from anonymous controls; account selectors remain. Account selectors are stored as templates and resolved from the current session. The observed `brandSlug: "the-new-yorker"` lookup is supported, including explicit active statuses in the `subscriptions.CDS` collection. Collection probes require the New Yorker brand selector and save the collection path and status field instead of private record keys. Other POST routes, mutation operations, unknown body fields, unknown authorization methods, and ambiguous entitlement schemas remain blocked. POST redirects are rejected. If it reports `unverifiable`, diagnostics include rejected endpoint descriptions and first-party schema field names, types, and fixed status categories: query values and non-standard path segments are redacted; account values, response bodies, and credentials are never printed. Discovery saves only the targeted subscription lookup request (including its authorization header) and response to `data/subscription-discovery-private.json` (mode 600 in a private directory), including values needed for local debugging. This file is ignored by Git and treated as session material. `NY_AUDIO_DISCOVERY_CAPTURE_PATH` configures its location. It is never used to grant access; live verification remains required. These diagnostics can identify an unsupported provider host, query option, or POST-based account API. Unknown response containers are traversed under redacted names so nested subscription fields remain visible. Rejected subscription lookup bodies include their encoding, redacted field structure, and whether a value matches a saved identity selector or cookie; scalar values remain hidden. Discovery checks an available identity probe even when no subscriber probe is found. In that case, a successful live identity check and anonymous control report `authenticated: true`, `http_session_reusable: true`, `subscriber: null`, and reason `no_explicit_subscriber_probe_observed`. Article retrieval remains blocked and no verification spec is saved until both identity and subscriber checks pass. Do not treat discovery or a successful browser login as a completed HTTP/subscriber check unless the result is `verified`.

## Session status and expiration (step 4)

With the service running, check subscriber access without Chrome:

```sh
curl http://127.0.0.1:8000/auth/status
```

The endpoint returns HTTP 200 with the current backend session status. A verified response has this shape:

```json
{
  "status": "verified",
  "authenticated": true,
  "subscriber": true,
  "http_session_reusable": true,
  "checked_at": "2026-10-08T18:00:00Z",
  "action": "none",
  "reason": "verified"
}
```

| Status | Meaning | Action |
| --- | --- | --- |
| `missing` | No saved session | `reconnect` |
| `verified` | Live identity, subscription, and controls passed | `none` |
| `reconnect_required` | Session rejected or redirected to sign-in | `reconnect` |
| `not_subscribed` | Identity passed; explicit inactive subscription signal | `check_subscription` |
| `unverifiable` | Network/schema failure or invalid local configuration | `retry` or `contact_operator` |

Unknown identity/subscriber fields are `null`. `checked_at` is `null` when no live check could run. The API returns no account email, tokens, endpoint rules, or discovery diagnostics, and uses `Cache-Control: no-store`.

A shared service owns the verified HTTP client. Verification results are cached for 60 seconds per server process, and simultaneous checks share one verification. `NY_AUDIO_AUTH_VERIFICATION_CACHE_SECONDS` configures the interval (0 disables caching; maximum 300). `NY_AUDIO_AUTH_REQUEST_TIMEOUT_SECONDS` configures the per-request timeout (default 10 seconds). Saved CLI reports are never used as proof of a current session.

Replacing the session or probe file invalidates the cache on the next status or article operation. Normal reconnection is manual Chrome login followed by `uv run python -m app.newyorker.auth capture`; the running server loads that replacement without a restart. Saved probe rules are reused. Login, discovery, and automatic refresh do not run inside the status API.

Future article operations use `SessionService.get_article`, which checks current verification before fetching and refuses content if the session changes during the request. A sign-in redirect or authentication rejection immediately changes status to `reconnect_required`. An upstream failure clears cached article authorization so the next operation checks again. Timeouts, generic 403 responses, and ambiguous subscription responses cannot be classified as expiration or inactivity.

Blocked article operations return HTTP 503 for missing, rejected, or unverifiable backend sessions, or HTTP 403 for an explicitly inactive subscription. Their error body carries a `newyorker_session_*` code and recovery action. These responses describe the backend's New Yorker session independently of Android app authentication. The article endpoints will be added in later steps.

## Magazine issue index (step 5)

Pull the dated issue links shown on the [archive page](https://www.newyorker.com/archive) using the verified subscriber session:

```sh
uv run python -m app.newyorker.archive sync
```

This saves a deduplicated issue list to `data/newyorker-issues.json` (configurable with `NY_AUDIO_ISSUE_INDEX_PATH`). Each entry contains the canonical issue date, the publisher's date label when available (including double issues), and the issue URL. A default sync refreshes the current archive listing and resolves its current-issue link when present; it does not claim to have downloaded the entire historical archive.

To collect older issues, follow the actual Previous navigation on issue pages:

```sh
uv run python -m app.newyorker.archive sync --history
```

Historical sync defaults to a one-second delay between issue-page requests and a maximum of 10,000 pages per run. It checkpoints after every page, reports progress periodically, and resumes from the saved cursor if interrupted. The full history can take a substantial initial run. To test or work in batches:

```sh
uv run python -m app.newyorker.archive sync --history --max-pages 20
```

Only reaching the dated first issue (February 21, 1925) marks history complete. Missing/ambiguous Previous links and changed layouts stop safely with the previous checkpoint retained. The shared session service continues to verify authentication throughout the sync; recapture login if it reports a reconnect error, then resume.

Read the saved list without making upstream requests:

```sh
uv run python -m app.newyorker.archive list
uv run python -m app.newyorker.archive list --year 2026
```

Year filtering uses the canonical issue date, so cross-year double issues belong to their starting year. `--output` on sync and `--input` on list allow a separate private index path. The index contains issue metadata; article content and playable audio are separate subsequent steps. The initial archive sync was tested locally and returned the 2026 issues.

## Issue article lists (step 5)

From the repository directory, fetch the current issue's table of contents through the verified subscriber session:

```sh
uv run python -m app.newyorker.issue sync --latest
uv run python -m app.newyorker.issue list
```

`--latest` checks `https://www.newyorker.com/magazine` directly, derives the dated issue URL from the page, and updates the issue index. To fetch another issue, use `sync --url https://www.newyorker.com/magazine/2026/10/05`. Read a saved list with `list --date 2026-10-05`.

Parsed lists are saved to `data/issue-articles/YYYY-MM-DD.json` (`NY_AUDIO_ISSUE_ARTICLES_PATH`). Entries include title, canonical URL, section, and available rubric, byline, description, and image. Galleries and puzzles are included when they appear as issue cards. Duplicate card links and navigation links are excluded.

An explicit Listen marker sets `audio_available` to true; an absent marker leaves it unknown. This hint does not establish that an audio URL can be resolved or played. Article/player metadata resolution remains the next task.

The fetched HTML is saved privately under `data/issue-pages/` (`NY_AUDIO_ISSUE_PAGE_CAPTURE_PATH`) for local layout debugging. Captures can contain private account data: do not share their contents. A layout error preserves the previous parsed list. Offline fixtures cover extraction and access failures; the current issue parser still needs a local live run.

## Magazine and article browsing API

Start the server from the repository directory:

```sh
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Browse magazines and open an issue's article list:

```sh
curl http://127.0.0.1:8000/issues
curl 'http://127.0.0.1:8000/issues?year=2026'
curl http://127.0.0.1:8000/issues/2026-10-12/articles
```

`GET /issues` returns `updated_at`, `history_complete`, and `issues`, sorted newest first. Each magazine has an `issue_date`, `label`, canonical `url`, and `cover_image_url` when available. Covers come from archive cards or the issue page; the rendered full cover is preferred over its cropped social preview. Missing covers are `null`, and previously saved cover URLs survive syncs that omit them. Rerun `uv run python -m app.newyorker.archive sync` to enrich existing archive records with covers. Optional `year` filtering uses the starting date of double issues. This serves the saved archive index; if it is missing, run archive sync first. `history_complete: false` means older magazines may still need historical sync.

`GET /issues/{issue_date}/articles` returns that issue's metadata, the article-list fetch time, and its `articles`. Article entries include title, URL, section, and available author/byline, description, image, and narration hint. A saved list is returned immediately; a known issue without a saved list has its table of contents fetched through the subscriber session and saved for subsequent requests. Unknown issue dates return HTTP 404 and invalid dates return HTTP 422. These endpoints do not resolve audio or download full articles.

Both endpoints require verified subscriber access and return `Cache-Control: no-store`. Missing/rejected/unverifiable sessions return HTTP 503 with the existing recovery action; an explicitly inactive subscription returns HTTP 403. Missing or invalid local index/list data returns a safe HTTP 503 error, and upstream retrieval failures return HTTP 502. An unavailable list is never reported as an empty successful result. Open `/docs` to inspect the response schemas and try the endpoints.

## Audio resolution (step 5)

### Playback API

When the Android user selects an article to play, call `POST /articles/audio`
with its `url` from the issue article list:

```sh
curl -X POST https://audioplayerbe-production.up.railway.app/articles/audio \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.newyorker.com/magazine/2026/10/12/why-is-illness-so-hard-to-write-about"}'
```

A successful response has this shape (values below are illustrative):

```json
{
  "article": {
    "id": "article-id",
    "title": "Article title",
    "url": "https://www.newyorker.com/magazine/2026/10/12/article-slug",
    "description": null,
    "authors": ["Author name"]
  },
  "audio": {
    "audio_id": "audio-id",
    "duration_ms": 1630000,
    "audio_url": "https://audio-resources.cnevids.com/audio/narrated/audio-id-example.mp3",
    "content_type": "audio/mpeg",
    "verified_at": "2026-10-12T21:00:00Z"
  }
}
```

Load `/issues/{issue_date}/articles` to display the issue without resolving its
audio. On Play, submit the selected article URL, display a loading state, then
give `audio.audio_url` to the Android player to stream directly. Use
`audio.duration_ms` for duration. An explicit absence of narration returns HTTP
200 with `audio: null`; show that narration is unavailable instead of starting
playback. On HTTP 502, allow retry. On HTTP 403/503, follow the existing backend
session recovery `detail.action`. An invalid or unsupported article URL returns
HTTP 422 without fetching it.

The endpoint verifies subscriber access before fetching and again before
returning the result. Responses use `Cache-Control: no-store` to prevent HTTP
caching; the backend separately saves successful results under
`data/article-audio/`, keyed by the canonical article URL. Later calls reuse that
result without fetching the article or contacting the player/CDN, while still
requiring verified subscriber access. The cache persists across deployments when
`/app/data` is on the Railway volume. `NY_AUDIO_AUDIO_CACHE_PATH` overrides its
directory. Explicit `audio: null` results are cached too; errors are never cached.

There is no automatic audio-cache expiration. If playback fails with an old URL,
call the same endpoint with `{"url":"<article-url>","refresh":true}` to resolve
again and replace the cache. A failed refresh returns an error and preserves the
previous cached result. `audio.verified_at` records the original MP3 verification
time; it is not updated by cache hits. The API does not create private article
captures. Subscriber credentials and article body content are excluded from the
response and are never sent to the media player/CDN. Re-resolve on a subsequent
play attempt if the source becomes unavailable; MP3 URL lifetime is not assumed.
The endpoint uses the existing shared backend subscriber session and currently
has no client authentication, like the browsing endpoints.

### CLI

Resolve a narrated article through the verified subscriber session:

```sh
uv run python -m app.newyorker.audio resolve https://www.newyorker.com/magazine/2026/10/12/why-is-illness-so-hard-to-write-about
```

The command returns article ID, title, URL, description, and authors, plus audio ID, duration in milliseconds, the verified MP3 URL, content type, and verification time. It saves the latest successful result to `data/audio-resolution-last-result.json`. An explicitly unnarrated article returns `audio: null`; malformed metadata, session rejection, or player/CDN failure returns an error instead.

The captured narration permalink responds with an HTTP 302 redirect directly to `audio-resources.cnevids.com/audio/narrated/...mp3`. The resolver uses this observed URL, checks that its filename matches the article's audio ID, and makes a separate `Range: bytes=0-4095` request without subscriber credentials. It accepts a bounded full-response prefix if the CDN ignores Range, and rejects invalid byte ranges, non-MP3 content, and unsafe redirects. Android can stream the resulting CDN URL directly. No MP3 filename is constructed and no full audio download is needed to resolve it.

Subscriber access is required before the article request and checked again before the result is returned. The article body, browser state, and subscriber cookies are excluded from the JSON result. Live resolution succeeded on 2026-10-08 for the captured article: the CDN returned recognizable MP3 bytes with content type `audio/mpeg`, and the resolved duration was 1,630,000 ms (27 minutes 10 seconds). Offline tests cover the complete redirect/range flow and failure cases.

Capture a subscriber article and its observed narration embed privately for resolver development:

```sh
uv run python -m app.newyorker.audio_capture
```

The default is the latest issue's "Why Is Illness So Hard to Write About?" article. An optional positional article URL selects another article. This command verifies the subscriber session, saves `data/audio-article-private.json`, and records the observed permalink-to-MP3 redirect in `data/audio-embeds-private.json`. Captures remain private and must not be shared or committed. The diagnostic command does not verify MP3 bytes; use `audio resolve` for a verified source.

Structured article state is parsed as JSON without executing page scripts. Narration metadata models and a separate media client are implemented. The media client sends no subscriber cookies or authorization to the player/CDN, restricts destinations, and verifies an observed MP3 source with a bounded byte-range request and MP3 header checks. Offline tests cover response failures, redirect restrictions, credential isolation, and byte-range verification.

The live article capture confirmed narration under `transformed.article.relatedAudio`. Article identity, header metadata, author names, and narration metadata are parsed from that primary article, excluding recommendations. The capture command preserves player status/redirect diagnostics in `data/audio-player-diagnostics-private.json` and reports incomplete captures safely. Live captures confirmed article parsing and the permalink redirect; the complete resolver's CDN byte-range check also succeeded on 2026-10-08.

## Current scope

Step 1 implements the project skeleton and `/health`. The health endpoint reports that the service is running; it does not establish or verify subscriber authentication.

Step 2 provides local interactive browser login and saved-session checks. Actual sign-in requires the subscriber to complete the browser flow. Step 3 implements HTTP session reuse and browser-observed subscriber verification. Live verification succeeded on 2026-10-08: authentication, active subscriber status, and HTTP session reuse were confirmed, with a rejected invalid-signature control. Saved probes can be rechecked using `uv run python -m app.newyorker.http_auth check` without Chrome. Step 4 adds `/auth/status`, cached session verification, capture reload, and a shared article-access gate. Step 5 includes authenticated issue-index sync with resumable historical discovery, issue table-of-contents extraction, primary article metadata parsing, and narrated-audio resolution with MP3 verification. The complete audio CLI passed its live local check on 2026-10-08. Magazine and per-issue article lists are available through `/issues` and `/issues/{issue_date}/articles`; `POST /articles/audio` resolves a selected article for direct playback on demand.
