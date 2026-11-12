# 02: Preserve progress and report incomplete duration sweeps

**What to build:** An operator can run the same one-issue command through
partial upstream failures or subscriber access loss without losing successful
work or erasing older metadata. The exit status and report clearly distinguish
freshly checked results from unresolved articles.

**Blocked by:** 01 — Populate narration durations for one saved issue.

**Status:** complete

- [x] Continue after article-specific network failures, missing or malformed
  primary narration metadata, and invalid durations. Report a per-article
  unresolved outcome identifying the affected article without exposing private
  page bodies or credentials.
- [x] On a failed fresh lookup, preserve all previously saved narration
  metadata for that article. Without a previous value, leave duration unknown.
  Do not erase a saved duration or mark an article non-audio because of a
  discovery failure.
- [x] Persist successful per-article updates as progress is made. Results
  already completed remain available through the issue-list API when later
  articles fail, subscriber access is rejected, or the command is interrupted.
  A failed write must not corrupt the previously saved issue list.
- [x] Subscriber access rejection stops further article requests, preserves
  completed progress, and reports the existing reconnect or access action.
  The command does not attempt an interactive login.
- [x] Report total articles, attempted articles, fresh narrated/non-narrated
  outcomes, unresolved articles, and articles not attempted because execution
  stopped. Identify preserved previous values without counting them as fresh
  successes. Report duration provenance and whether media requests occurred.
- [x] Return a nonzero exit status if the sweep is incomplete, including any
  unresolved or unattempted articles. A fully resolved sweep returns success;
  confirmed non-narrated articles count as resolved outcomes.
- [x] Rerunning still fetches every article afresh; preserving progress is not
  a skip/resume cache. Document partial reports and retry behavior.
- [x] Integration coverage proves that a mixed successful/failed run retains
  old failed-article values and exposes fresh successful values through the API;
  later authentication rejection preserves earlier work and stops requests;
  invalid metadata stays unresolved; and report totals and exit status agree
  with the saved outcomes.

## Validation

- Full offline suite: 189 passed, 9 browser tests skipped, 88 subtests passed.
- Mypy passes for `app/newyorker/durations.py` with imports followed silently.
- Integration tests cover mixed HTTP/connection and metadata failures, fresh
  retries of every article, access rejection after visible progress, interruption
  and task cancellation, and atomic-write failure preserving the last saved list.
- Successful reports go to stdout with exit 0; incomplete reports go to stderr
  with exit 2 (130 for interruption during article processing).
- Live subscriber coverage and independent duration checks remain the rollout
  prerequisite described in the parent README; this validation uses synthetic
  responses and makes no live subscriber or media requests.
