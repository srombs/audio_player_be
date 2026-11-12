# Backend handoff for ticket 06

Implement this work in `audio_player_be`. [Android ticket 06](https://github.com/srombs/audio_player_fe/blob/eba21e6/.scratch/article-browsing-android-auto/issues/06-consume-summary-durations.md)
will use the result to show narration lengths in phone and Android Auto issue
lists before the listener plays an article. The coverage goal is a duration for
every narrated article served by the issue catalog, including existing issues.

## API contract

- Add `duration_ms` to each article summary in `articles` returned by
  `GET /issues/{issue_date}/articles`. The field is an optional JSON integer
  containing the positive narration duration in milliseconds. For example,
  `"duration_ms": 1440000` represents 24 minutes.
- Omit the field or return `null` when the duration is unknown or the article
  has no narration. A summary with `audio_available: false` must omit the field
  or return `null`.
- Keep the existing response version and article identifiers. The summary
  carries duration metadata. `POST /articles/audio` continues to resolve the
  stream for playback on demand.

## Agreed first implementation

Provide an explicit one-off duration command targeting one saved issue. The
issue must already exist in the local catalog and have a saved article list.
If either is missing, return a clear error directing the operator to sync the
issue first. The duration command does not fetch a table of contents, create an
issue, or synchronize the catalog.

Each invocation performs a clean sweep of every article in that saved list,
including articles with saved durations, articles previously confirmed without
narration, and articles without Listen badges. Fetch fresh primary article
metadata from the New Yorker using the existing subscriber access checks.
Do not use playback-cache results to skip or replace these fresh lookups.

Use the article page's `relatedAudio.durationInMs` as the duration source:

- Valid positive integer duration: save the fresh duration and confirmed
  narration availability.
- Explicit `relatedAudio: null`: save `audio_available: false` and clear any
  previous duration.
- Missing or malformed metadata, invalid duration, or an article-specific
  fetch failure: report the article as unresolved and preserve its previous
  saved metadata. With no previous duration, leave it unknown.

The first implementation trusts valid publisher duration metadata. It does
not resolve narration URLs or inspect/download audio files to measure duration.
Missing durations remain visible as unresolved results in the command report.

Persist successful results even if other articles fail, and continue processing
after article-specific failures. Stop on subscriber access rejection while
retaining completed progress. Report per-article failures and overall coverage;
return a nonzero exit status when the sweep is incomplete. Preserved values
after failed lookups must not be counted as freshly verified results.

Save the results for the issue-list API and preserve enriched metadata during
later normal issue syncs. All per-article duration discovery stays outside the
issue-list request path. In particular, the existing list endpoint can call
`sync_issue` when its article list is missing, so enrichment must remain a
separate operation.

Article filtering is deferred to [the follow-up feature](FOLLOW_UPS.md). This
command retains the issue's articles, including those without narration.

## Exploration evidence and limits

- The issue parser currently treats a Listen badge as `audio_available: true`;
  absence of a badge is unknown. All 25 articles in the one locally saved issue
  have unknown availability, so badge-only selection would inspect none.
- One stored primary article contains `relatedAudio.durationInMs: 1630000`.
  The stored issue page contains no `relatedAudio` or `durationInMs` fields.
- The playback resolver already reads this article metadata before media
  resolution. Its saved duration comes from the same field, so a matching
  playback result is not independent validation of metadata accuracy.
- A bounded 128 KiB request to the saved narration's public MP3 succeeded but
  did not expose a usable Xing/Info/VBRI frame-count header for an independent
  exact duration comparison. No full audio download was performed.
- A broader live article sample could not be retrieved because the existing
  subscriber session was rejected and requires reconnection. Accuracy and
  coverage across multiple articles remain unverified. Compare a small sample
  against independently measured playback lengths before rollout; revisit
  audio-based measurement if actual metadata gaps or discrepancies warrant it.

## Backend acceptance

- A narrated article that has never been played returns a positive
  `duration_ms` after its metadata has been populated.
- Issue summaries with unavailable durations still load with the field omitted
  or `null`. Older clients can continue to read the response.
- Record the source used to populate durations, whether narration URLs had to
  be loaded, and what coverage the sync or backfill achieved for existing issues.
- A missing local issue or saved article list fails without creating or syncing
  it. Every article in a valid selected issue receives a fresh lookup on each
  run, regardless of previous duration or narration status.
- A failed refresh preserves previous metadata, reports an unresolved article,
  and does not erase successful work on other articles. A confirmed no-audio
  result clears a previous duration.
- Normal issue sync preserves enriched narration metadata, and browsing never
  starts per-article duration discovery.
