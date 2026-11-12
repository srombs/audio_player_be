# 03: Preserve enriched narration metadata through normal issue sync

**What to build:** An operator can refresh an issue's table of contents after
populating durations and continue browsing its saved narration metadata.
Refreshing article details does not discard authoritative duration findings
or add duration discovery to normal sync or browsing.

**Blocked by:** 01 — Populate narration durations for one saved issue.

**Status:** ready-for-agent

- [ ] Match refreshed article summaries to saved entries by canonical article
  identity and retain enriched positive durations and confirmed narration
  availability for matching articles.
- [ ] Preserve confirmed no-narration outcomes as well as positive durations.
  Fresh table-of-contents hints or missing Listen badges do not override an
  explicit article-page finding. Do not treat all existing null hints as
  confirmed absence.
- [ ] Refresh ordinary table-of-contents metadata and membership normally.
  Newly encountered articles start with unknown duration, and saved metadata
  is never assigned to a different article or used to restore an article
  removed from the refreshed list.
- [ ] Syncing a legacy issue list without enrichment remains compatible.
  A failed table-of-contents refresh preserves the existing successful list and
  its enriched metadata.
- [ ] Normal sync and browsing still make no per-article duration-discovery or
  media requests. `GET /issues/{issue_date}/articles` returns retained durations
  with the unchanged API version after table-of-contents refresh.
- [ ] Integration coverage exercises successful duration enrichment, normal
  sync with changed article details and membership, and subsequent API reads;
  includes known durations, confirmed non-audio articles, legacy unknowns, and
  failed sync preservation. Run the relevant existing browsing/sync tests.
