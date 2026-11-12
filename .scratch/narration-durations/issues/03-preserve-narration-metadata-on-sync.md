# 03: Preserve enriched narration metadata through normal issue sync

**What to build:** An operator can refresh an issue's table of contents after
populating durations and continue browsing its saved narration metadata.
Refreshing article details does not discard authoritative duration findings
or add duration discovery to normal sync or browsing.

**Blocked by:** 01 — Populate narration durations for one saved issue.

**Status:** complete

- [x] Match refreshed article summaries to saved entries by canonical article
  identity and retain enriched positive durations and confirmed narration
  availability for matching articles.
- [x] Preserve confirmed no-narration outcomes as well as positive durations.
  Fresh table-of-contents hints or missing Listen badges do not override an
  explicit article-page finding. Do not treat all existing null hints as
  confirmed absence.
- [x] Refresh ordinary table-of-contents metadata and membership normally.
  Newly encountered articles start with unknown duration, and saved metadata
  is never assigned to a different article or used to restore an article
  removed from the refreshed list.
- [x] Syncing a legacy issue list without enrichment remains compatible.
  A failed table-of-contents refresh preserves the existing successful list and
  its enriched metadata.
- [x] Normal sync and browsing still make no per-article duration-discovery or
  media requests. `GET /issues/{issue_date}/articles` returns retained durations
  with the unchanged API version after table-of-contents refresh.
- [x] Integration coverage exercises successful duration enrichment, normal
  sync with changed article details and membership, and subsequent API reads;
  includes known durations, confirmed non-audio articles, legacy unknowns, and
  failed sync preservation. Run the relevant existing browsing/sync tests.

## Validation

- Full offline suite: 197 passed, 9 browser tests skipped, 88 subtests passed.
- Relevant duration, issue-sync, and browsing tests: 72 passed, 10 subtests passed.
- Mypy passes for `app/newyorker/issue.py` with imports followed silently.
- Integration coverage runs duration enrichment, both latest and dated issue
  sync, and API reads through synthetic HTTP responses. It checks canonical URL
  normalization, changed details/order/membership, confirmed absence overriding
  a Listen hint, and legacy unknowns and hints. Only the table of contents is
  fetched during sync; browsing uses the saved result without article or media
  requests.
- Upstream failure, subscriber rejection, unsupported layout, mismatched issue,
  and atomic list-write failure preserve the saved enriched list and API output.
- Live subscriber coverage and independent duration checks remain the rollout
  prerequisite in the parent README. This validation makes no live subscriber
  or media requests.
