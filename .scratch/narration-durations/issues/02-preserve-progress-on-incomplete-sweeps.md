# 02: Preserve progress and report incomplete duration sweeps

**What to build:** An operator can run the same one-issue command through
partial upstream failures or subscriber access loss without losing successful
work or erasing older metadata. The exit status and report clearly distinguish
freshly checked results from unresolved articles.

**Blocked by:** 01 — Populate narration durations for one saved issue.

**Status:** ready-for-agent

- [ ] Continue after article-specific network failures, missing or malformed
  primary narration metadata, and invalid durations. Report a per-article
  unresolved outcome identifying the affected article without exposing private
  page bodies or credentials.
- [ ] On a failed fresh lookup, preserve all previously saved narration
  metadata for that article. Without a previous value, leave duration unknown.
  Do not erase a saved duration or mark an article non-audio because of a
  discovery failure.
- [ ] Persist successful per-article updates as progress is made. Results
  already completed remain available through the issue-list API when later
  articles fail, subscriber access is rejected, or the command is interrupted.
  A failed write must not corrupt the previously saved issue list.
- [ ] Subscriber access rejection stops further article requests, preserves
  completed progress, and reports the existing reconnect or access action.
  The command does not attempt an interactive login.
- [ ] Report total articles, attempted articles, fresh narrated/non-narrated
  outcomes, unresolved articles, and articles not attempted because execution
  stopped. Identify preserved previous values without counting them as fresh
  successes. Report duration provenance and whether media requests occurred.
- [ ] Return a nonzero exit status if the sweep is incomplete, including any
  unresolved or unattempted articles. A fully resolved sweep returns success;
  confirmed non-narrated articles count as resolved outcomes.
- [ ] Rerunning still fetches every article afresh; preserving progress is not
  a skip/resume cache. Document partial reports and retry behavior.
- [ ] Integration coverage proves that a mixed successful/failed run retains
  old failed-article values and exposes fresh successful values through the API;
  later authentication rejection preserves earlier work and stops requests;
  invalid metadata stays unresolved; and report totals and exit status agree
  with the saved outcomes.
