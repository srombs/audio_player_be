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

## Investigate how to populate durations

The current issue parser records a Listen badge. The playback resolver already
reads `durationInMs` from an article page's `relatedAudio` metadata before
resolving the stream. Check representative issue and article pages and the
existing backend audio cache to see where reliable durations can be obtained.

The backend may need to load each narration URL once to discover its duration
when the cache and page metadata cannot supply it. Determine during backend
exploration whether that step is necessary and what request is sufficient. Reuse
known durations and persist the results so subsequent issue-list requests can
serve the saved metadata. Run any per-article fetching as part of sync or
backfill, outside the issue-list request path.

## Backend acceptance

- A narrated article that has never been played returns a positive
  `duration_ms` after its metadata has been populated.
- Issue summaries with unavailable durations still load with the field omitted
  or `null`. Older clients can continue to read the response.
- Record the source used to populate durations, whether narration URLs had to
  be loaded, and what coverage the sync or backfill achieved for existing issues.
