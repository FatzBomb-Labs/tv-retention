# Validation record

The current gate result and what it does not cover. Per-change history is in git; this
file records only what is true now. Never quote a test count from memory — read it here,
or re-run the gate.

## Current gate — 2026-09-20

Authoritative Linux host gate on `fatzserver-host`: **650 Python tests in 9.574s** (1
skipped — this host's trimmed zone database lacks `America/Detroit`) and **34 frontend
tests**, `All required checks passed`, exit code `0`. Worker imports and shipped-module
syntax checks passed.

Down from 741: the `test_build.py` trim removed 100 assertions about appearance, and
flooring migration at version 13 removed the tests for twelve upgrade steps no surviving
document can reach. No behavioral coverage was removed; the count has since risen again
with the scheduler, integrity and UI-state work.

## Graceful shutdown and mid-run interruption recovery — 2026-09-21

PLAN §2 listed "shutdown during a run" and readiness as unverified. Investigating it found
a real, previously invisible defect: `server.py` runs as PID 1, and the Linux kernel does
not apply a default action to an unhandled signal for PID 1 the way it would for any other
process. Nothing here had ever registered a SIGTERM handler, so `docker stop` was silently
running out its full timeout and falling back to SIGKILL on every stop — including every
deployment earlier in this session. Measured against a real, otherwise-idle container:

| | before | after |
|---|---|---|
| `docker stop -t 10` | 10.19s, exit 137 (SIGKILL) | 0.21s, exit 0 |

Fixed by moving `serve_forever()` onto its own thread and having the main thread wait on
an `Event` a SIGTERM handler sets, then call `shutdown()` — `HTTPServer.shutdown()`
deadlocks if called from the same thread already running `serve_forever()`, which is why
the two cannot share a thread.

Separately, and precisely the scenario PLAN's item was asking about: does an interrupted
run recover safely? Built a disposable rig for this — a synthetic HTTP fixture standing in
for Sonarr (one series, one episode, its delete endpoint deliberately slow) and a throwaway
`tv-retention` container on an isolated Docker network, entirely disconnected from the
operator's real Sonarr and real library. A run was triggered through the real HTTP RPC path
(login, CSRF, `POST /api`) so the work executed on the container's actual PID 1, then the
container was hard-killed (`docker kill`, SIGKILL, uncatchable) at the instant the delete
request was sent, confirmed via the fixture's own log:

- the on-disk intent correctly recorded `set-monitored: done` and `delete-episode-file:
  in-progress` with no `finished_at` — an accurate, non-falsified checkpoint of exactly
  how far execution had gotten;
- on restart, a fresh run re-read the fixture from scratch, resumed cleanly, and finished
  `complete` with both operations `done` and `attempts: 1` each — no duplicate deletion,
  no lost unmonitor, no re-execution of already-terminal work;
- the same result held whether the container was killed with `docker kill` (SIGKILL) or,
  after the shutdown fix, with `docker stop` mid-run (prompt exit, 0.54s, exit 0) — the
  fix makes shutdown responsive without changing what an interruption leaves behind,
  because `shutdown()` stops the accept loop but still does not wait for an in-flight
  request, which continues to rely on the same checkpoint durability either way.

The rig (network, fixture container, throwaway `tv-retention` instance, scratch config)
was fully torn down afterward. The operator's Sonarr, library and production container
were not reachable from it and were not touched at any point.

A regression test (`test_server.GracefulShutdown`) checked in both directions on the host:
fails within 3s against the pre-fix code with a clear message, passes in 0.07s against the
fix. It mocks `signal.signal` rather than sending a real OS signal to the test process — a
real SIGTERM to the shared test runner, mistimed, terminates the whole gate rather than
failing one test, which would be a worse outcome than the bug it is meant to catch.

## Build 28 deployment — 2026-09-21

Replaced `dev-build27` with `dev-build28`: the SIGTERM fix. Config already at version
14, so nothing migrated.

- Preflight against a `/tmp` copy, `--rm`: version 14, 32 rules, 4 presets, Test Mode
  on, schedule off, `integrity_errors` clean.
- Before touching production, timed `docker stop -t 10` against a disposable container
  running this exact built image, config-copy mounted, otherwise idle: **0.24s, exit
  0** — confirming the fix was genuinely present and working in the artifact about to
  ship, not just in source.
- The `docker stop -t 30` that preceded the build27→build28 swap itself, still on the
  unfixed build27 image, was not specially timed but is consistent with the ~10s+
  SIGKILL-fallback behaviour recorded above.
- After replacement: healthy, worker uid 99, safety posture and stored timezone
  unchanged. A direct stop/start cycle against the live production container
  afterward: **0.55s, exit 0**, config intact on restart.
- Rollback is `tv-retention:rollback` (build 27) plus `pre-build28-<timestamp>.tar.gz`.

## Build 27 deployment — 2026-09-20

Replaced `dev-build26` with `dev-build27`: an untrustworthy plan (an enabled rule with
no reading yet) now states the preview may be incomplete instead of asserting "No
changes are currently expected." Config already at version 14, so nothing migrated.

- Preflight against a `/tmp` copy, `--rm`: version 14, 32 rules, 4 presets, Test Mode
  on, schedule off, `integrity_errors` clean.
- The fix text confirmed present in the built image before it replaced anything.
- After replacement: healthy, worker uid 99, safety posture and stored timezone
  unchanged.
- Rollback is `tv-retention:rollback` (build 26) plus `pre-build27-<timestamp>.tar.gz`.

## Build 26 deployment — 2026-09-20

Replaced `dev-build25` with `dev-build26`: the timezone control clusters by actual
offset behaviour (486 raw names down to 58 for this image) instead of listing every
IANA identifier, adds a search box, and now genuinely saves on its own -- it was never
in `wire()`'s autosave array, so an edit alone reverted on the next heartbeat. Config
already at version 14, so nothing migrated.

- Preflight against a `/tmp` copy, `--rm`: version 14, 32 rules, 4 presets, Test Mode
  on, schedule off, `integrity_errors` clean, 58 zones offered, `America/New_York`
  present as the canonical pick for its cluster, and the stored `America/Detroit`
  correctly appended even though it is not that cluster's own preferred name.
- The three fix components (`common_zones`, the search wiring, `PREFERRED_ZONE_NAMES`)
  confirmed present in the built image before it replaced anything.
- After replacement: healthy, worker uid 99, safety posture unchanged (Test Mode on,
  schedule off), stored timezone unchanged.
- One preflight redo mid-deploy: a bash quoting error aborted the first copy command
  under `set -e` before `cp -a` ran, and the resulting empty directory was checked
  against by mistake, reporting 0 rules until the copy was redone and re-verified
  present before drawing any conclusion from it.
- Rollback is `tv-retention:rollback` (build 25) plus `pre-build26-20260920-2323.tar.gz`.

## Build 25 deployment — 2026-09-20

Replaced `dev-build24` with `dev-build25`: the timezone control is now a grouped
dropdown built from the container's own zone database. Config already at version 14,
so this migrated nothing.

- Preflight against a `/tmp` copy, `--rm`: version 14, `America/Detroit` still resolvable
  from the offered list (482 zones, America/Detroit among them), 32 rules and 4 presets,
  Test Mode on, schedule off, `integrity_errors` clean.
- The dropdown code confirmed present in the built image (`available_zones`,
  `renderTimezones`, the `<select id="tvr-timezone">` element) before it replaced
  anything.
- After replacement: healthy, worker uid 99, safety posture unchanged (Test Mode on,
  schedule disabled) — the deploy touched behaviour, not configuration.
- Rollback is `tv-retention:rollback` (build 24) plus `pre-build25-20260920-1752.tar.gz`.

## First operator-approved live run — 2026-09-20

Run `63b8ab8c8b96`, 21:43:19 UTC, on build 24 against the operator's real Sonarr with a
bounded rule: **2 planned, 2 deleted, 2705 MiB freed, 0 errors**, `status: complete`.
The journal records `test_mode: false`, `dry_run: false`, `scheduled: false` — a real
manual run, not a dry one. Test Mode was restored afterwards and the schedule left off.

This is acceptance step 3 for the deletion path itself. Not yet evidenced by it: shared
multi-episode files, recycle-bin recovery, partial failure and restart recovery during a
run, and whether the confirmation text matched the plan. Those remain open.

Correction to the build 24 entry below: it called the coming run "the first deletion this
application has ever performed". That was wrong. The journal shows completed live runs on
2026-09-16 and twice on 2026-09-18, and the four series missing from the current config
were removed through the application's own queued `delete-series-files` on 2026-09-16.

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

One bounded live run has now exercised the deletion path end to end (above). It did not
cover shared multi-episode files, recycle-bin recovery, partial failure or restart
recovery during a run, and it says nothing about whether the confirmation text matched
the plan.

Still without practical evidence: monitoring-write correctness beyond that run, real
provider behaviour with live keys, API-key lifecycle, backup and restore against a real
configuration, `UMASK` and backup-mount permissions, dropped-privilege restore, readiness
as distinct from liveness, real-browser workflows and large-library performance.

Isolated contract tests do not replace those checks. Keep schedules off and Test Mode on,
and follow the acceptance ladder in [PLAN.md](PLAN.md) before any live write.
