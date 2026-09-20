# Validation record

The current gate result and what it does not cover. Per-change history is in git; this
file records only what is true now. Never quote a test count from memory — read it here,
or re-run the gate.

## Current gate — 2026-09-20

Authoritative Linux host gate on `fatzserver-host`: **641 Python tests in 10.082s** and
**31 frontend tests**, `All required checks passed`, exit code `0`. Worker imports and
shipped-module syntax checks passed.

Down from 741 because the `test_build.py` trim removed 100 assertions about appearance —
colours, wording, control placement, icon geometry — and kept 66 structural checks. No
behavioral coverage was removed.

No deployment, live Sonarr request, media access or Docker acceptance was performed.

## What the gate covers

- **Retention and exclusions:** keep-window decisions against fixtures, one-pass exclusion
  causes, shared multi-episode files protected by any excluded member, deletion refused
  until every affected episode is confirmed unmonitored.
- **Test Mode boundary:** manual, scheduled, CLI, picker, recycle-bin and restore paths all
  refuse external writes; a reused client re-checks the saved mode and fails closed.
- **Durability:** revision-checked owned-field merges, stale full-document writes rejected,
  one non-blocking `run.lock` across the scheduler's bookkeeping cycle, authoritative state
  failures raised rather than silently emptied.
- **Backup:** creation, staging and explicit activation; activation holds the run lock,
  forces Test Mode on and schedules off, and keeps a rollback journal.
- **Dates and scheduling:** ambiguous TMDB matches rejected, Sonarr dates never overwritten,
  only bounded air-date gaps estimated, all cron fields and IANA zones validated,
  spring-forward times skipped, fall-back occurrences distinctly identified, pending
  recovery cannot run twice in one tick.
- **Interface structure:** every addressed element exists, no duplicate ids, every control
  on a settings panel is filled and saved, no RPC action the page cannot reach.

## Evidence still missing

No recorded live acceptance establishes deletion or monitoring-write correctness, real
provider behavior with live keys, API-key lifecycle, or backup and restore against a real
configuration. Container permissions, dropped-privilege restore, graceful shutdown,
readiness, real-browser workflows and large-library performance have no practical
evidence.

Isolated contract tests do not replace those checks. Keep schedules off and Test Mode on,
and follow the acceptance ladder in [PLAN.md](PLAN.md) before any live write.
