# Narration durations

Tracker: local Markdown tickets in this repository.
Status vocabulary: `ready-for-agent`; work starts when blocking tickets are complete.

Source: [agreed backend duration handoff](../../BACKEND_DURATION_HANDOFF.md).

## Tickets

| Ticket | Delivers | Blocked by |
| --- | --- | --- |
| [01: Populate durations](issues/01-populate-saved-issue-durations.md) | One saved issue, fresh article metadata, persisted durations, and API output | None |
| [02: Preserve progress](issues/02-preserve-progress-on-incomplete-sweeps.md) | Partial success, preserved previous values, access failures, and coverage reports | 01 |
| [03: Preserve metadata on sync](issues/03-preserve-narration-metadata-on-sync.md) | Enriched durations and narration findings survive normal issue sync | 01 |

Start with 01. After it is complete, 02 and 03 can proceed independently.

## Scope

The command requires an existing locally indexed issue and its saved article
list. Each invocation fetches fresh primary article-page narration metadata
for every article, including articles with previous results. Valid publisher
`relatedAudio.durationInMs` supplies duration; explicit `relatedAudio: null`
confirms no narration. The command does not sync issues or inspect audio files.

## Release validation

Before rollout, use a working subscriber session to run the completed command
on a saved real issue, inspect coverage/provenance and failure reporting, and
independently spot-check a small sample of narration lengths. The previously
available session was rejected, so live coverage and cross-article metadata
accuracy are not established. This is an operational validation prerequisite,
not a requirement to add media measurement to the duration command. Record
remaining gaps rather than assuming complete coverage.

## Deferred feature

[Hiding confirmed non-audio articles](../../FOLLOW_UPS.md) remains separate.
A missing or null duration is not sufficient evidence for filtering an article.
