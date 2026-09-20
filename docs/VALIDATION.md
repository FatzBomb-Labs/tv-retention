# Validation record

The current gate result and what it does not cover. Per-change history is in git; this
file records only what is true now. Never quote a test count from memory — read it here,
or re-run the gate.

## Current gate — 2026-09-20

Authoritative Linux host gate on `fatzserver-host`: **631 Python tests in 8.990s** and
**33 frontend tests**, `All required checks passed`, exit code `0`. Worker imports and
shipped-module syntax checks passed.

Down from 741: the `test_build.py` trim removed 100 assertions about appearance, and
flooring migration at version 13 removed the tests for twelve upgrade steps no surviving
document can reach. No behavioral coverage was removed; the count has since risen again
with the scheduler, integrity and UI-state work.

## Build 24 deployment — 2026-09-20

Replaced `dev-build23` with `dev-build24`: the upgrade-catch-up seed, the scope-count
generation guard, the API-key reveal surviving a background render, and the sync-reply
consolidation. Config already at version 14, so this migrated nothing — a plain image
swap.

- Preflight against a `/tmp` copy, `--rm`: version 14, `America/Detroit`, 32 rules and
  4 presets, Test Mode on, `integrity_errors` clean, and `seed_last_occurrence` a
  confirmed no-op because the occurrence was already recorded.
- All four fixes verified present in the image before it replaced anything.
- After replacement: healthy, worker PID 1 running as uid 99, schedule still
  "Every day at 01:00 (America/Detroit)", and **no catch-up run** — unlike the build 23
  upgrade, which is the behaviour the seed exists to make permanent.
- Rollback is `tv-retention:rollback` (build 23) plus `pre-build24-20260920-1733.tar.gz`.

Three config archives are retained rather than pruned to one. A bounded real run with
Test Mode off is the next step, and pruning the safety net immediately before the first
deletion this application has ever performed is the wrong order to do things in.

## Build 23 deployment — 2026-09-20

Replaced `tv-retention:dev-build22` with `dev-build23` on the demo instance: config on
`/mnt/user/appdata/tv-retention`, `TZ=America/Detroit`, Test Mode on, schedules on.

- Preflight against a `/tmp` copy, `--rm`: migrated 13 → 14, timezone inherited as
  `America/Detroit` rather than `Etc/UTC`, 32 rules and 4 presets intact,
  `integrity_errors` clean.
- Schedule unmoved: `last_occurrence` resolves to `2026-09-20T05:00:00+00:00` under both
  builds — 01:00 EDT before and after. This is what `_to_v14` inheriting the container
  zone exists to guarantee; defaulting to `Etc/UTC` would have read 21:00.
- Rollback held as one `tv-retention:rollback` tag plus
  `pre-build23-20260920-1205.tar.gz`. Predecessor container and superseded tag removed.

**One-time upgrade artifact:** `last_occurrence` did not exist in build 22's `jobs.json`,
so build 23 read the 01:00 occurrence as unanswered and fired one catch-up test run at
12:07. That is the documented catch-up contract behaving correctly, and it was harmless
under Test Mode — but the same upgrade on an instance with Test Mode off would start a
real run within a minute of starting. Tracked in PLAN.

No live Sonarr mutation, media access or deletion occurred.

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
