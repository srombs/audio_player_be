# Follow-up features

## Hide articles with no narration

Status: deferred until after the narration-duration command design discussion.

Explore hiding articles confirmed to have no narration from article browsing.
Keep this separate from the current duration-enrichment work.

The New Yorker article page's explicit `relatedAudio: null` is the current
confirmation of no narration. A missing Listen badge or missing/malformed
metadata is unknown, not confirmation of absence.

Do not use a null or missing `duration_ms` alone to identify non-audio articles:
it can also mean duration discovery failed or has not run. The proposed filter
uses confirmed `audio_available: false`. Articles with known narration but an
unresolved duration must not be classified as non-audio.

Decide the filtering location, affected browsing surfaces, and treatment of
unknown narration states when this feature is explored.
