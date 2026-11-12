# 01: Populate narration durations for one saved issue

**What to build:** An operator can explicitly select one locally saved issue,
run a fresh sweep of its article-page narration metadata, and then see those
durations in issue article summaries before any playback. This first slice
delivers the complete successful command-to-browse path. Resilient partial
failure handling and preservation across subsequent issue sync are separate
dependent tickets.

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] The command requires an explicit single issue selector. Before upstream
  requests or data mutations, require that the issue is in the local catalog
  and has a valid saved article list. Missing or invalid local inputs produce
  a nonzero exit status and an actionable error; missing data directs the
  operator to sync first. Do not create the issue, fetch a table of contents,
  or synchronize the catalog.
- [ ] Fetch fresh metadata for every article in the selected saved list,
  including articles without Listen badges, those with saved positive
  durations, and those previously confirmed without narration. Playback-cache
  results do not substitute for fresh lookups. Another run repeats the entire
  sweep.
- [ ] Use the existing verified subscriber access flow and validate that
  metadata belongs to the requested primary article, excluding recommendations
  or unrelated embedded articles.
- [ ] Treat a positive JSON integer in primary `relatedAudio.durationInMs`
  as the publisher's duration in milliseconds. Save it with confirmed
  `audio_available: true`. Reject booleans, strings, fractional, zero, and
  negative durations as unresolved metadata.
- [ ] An explicit primary `relatedAudio: null` saves
  `audio_available: false` and clears a previous duration. A missing or malformed
  field is unresolved, never confirmation of no narration.
- [ ] Persist successful sweep results so `GET /issues/{issue_date}/articles`
  exposes optional `duration_ms` before first playback. Known durations are
  positive integers; unknown and confirmed non-audio articles omit the field or
  return null. `audio_available: false` never accompanies a positive duration.
- [ ] Retain response version, article identifiers, order, and all articles.
  Legacy saved lists and clients continue to work, and playback resolution
  remains on demand through `POST /articles/audio`.
- [ ] Browsing never triggers per-article duration discovery, including when
  its existing table-of-contents cache-miss path runs. The command makes no
  narration permalink, player, CDN, or audio-file requests.
- [ ] A completed successful sweep reports the selected issue, total articles,
  newly checked narrated/non-narrated counts, duration source
  `relatedAudio.durationInMs`, and that no media requests were needed. Document
  how to invoke it and interpret the successful result.
- [ ] Offline integration coverage exercises command invocation through saved
  results and API output, legacy saved lists, fresh lookups despite prior
  metadata, explicit no narration, local precondition failures, and the absence
  of media requests or browse-time article fetching. Use synthetic metadata
  rather than private subscriber captures in tests.
