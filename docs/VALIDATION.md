# Validation record

The current gate result and what it does not cover. Per-change history is in git; this
file records only what is true now. Never quote a test count from memory — read it here,
or re-run the gate.

## Current gate — 2026-09-21

Authoritative Linux host gate on `fatzserver-host`: **656 Python tests in 9.900s** (1
skipped — this host's trimmed zone database lacks `America/Detroit`) and **36 frontend
tests**, `All required checks passed`, exit code `0`. Worker imports and shipped-module
syntax checks passed.

Down from 741: the `test_build.py` trim removed 100 assertions about appearance, and
flooring migration at version 13 removed the tests for twelve upgrade steps no surviving
document can reach. No behavioral coverage was removed; the count has since risen again
with the scheduler, integrity and UI-state work.

## Read-only smoke on the target — 2026-09-22

PLAN §3 step 4, against the running demo instance (`tv-retention-demo`, `dev-build31`),
config at `/mnt/user/appdata/tv-retention`, `America/Detroit`, Test Mode on, schedule off.
Read-only throughout: no save, no rule, no monitoring change, no recycle-bin change, no API
key, no restore and no run. The evidence for the last of those is the state directory itself
— `journal.jsonl`, `run-intent.json` and `state.json` all still carry their Sep 20 17:43
mtime. The page's own background refresh did rewrite the local cache and health files,
which is the reading path doing its job rather than an external write.

- **Login.** `/` answers `303 → /login` unauthenticated, and the demo credentials sign in
  and land on the shell.
- **Version and build display.** The banner reads `v0.3.0` beside a `TEST MODE` badge, and
  Status carries all three — `0.3.0 · 31`, `built 9/22/2026`. The banner stays on the
  semantic version alone.
- **Assets.** One digest (`1490c09dae12`) across every script and stylesheet, so a module
  graph cannot straddle releases. `app.js` (19146 B), `app.css` (66314 B) and `icons.css`
  (6988 B) serve `200`; a missing file inside a held digest and a digest the server does
  not hold are both `404` — the second refused rather than served from the current release
  — and the non-public extensions (`interface.html`, `app.js.map`) are `404`.
- **Browser refresh.** A reload re-establishes the session with no login prompt and
  re-runs a background sync; the library repaints from the cached reading.
- **Ages.** The Sonarr sync reads `just now` under a `Cached reading` label, and the
  connections row shows the live instance (`Sonarr 4.0.19.3009 · 3019 series`).
- **Config and state permissions.** `/config` and `/config/state` are `755`, owned by
  uid 99, gid `users`; Status reports `Config readable: yes` and `State writable: yes`.
  Nothing is root-owned and no manual `chown` is needed.

Not done as written: step 4 asks for a *copied* config under `/tmp`, and this ran against
the instance's own config. It therefore establishes the read paths and the permissions of
the real volume, not isolation. No write path was exercised.

### The target Sonarr has no recycle bin

The Status pane carries a live `Sonarr has no recycle bin` alert — "Sonarr-Series deletes
files outright; nothing is recoverable". It is correct, and it was checked at the source
rather than believed: `GET /api/v3/config/mediamanagement` on that instance returns
`recycleBin: ""`, with `recycleBinCleanupDays: 7` and `deleteEmptyFolders: true`.

Two things follow. PLAN §3's recycle-bin item cannot be closed on recollection — on this
instance deletions are permanent, and the 2026-09-20 live run's 2705 MiB was not
recoverable, which nothing recorded at the time. And Unraid's own per-share recycle bin
does not cover it either: that intercepts deletions over SMB, while Sonarr runs in a
container unlinking files directly.

### Mode bits on this volume are wider than the code asks for

Measured and not explained, so recorded as measured: the JSON files under `/config`
(`settings.json`, `catalogue.json`, `health.json`, `jobs.json`, `state.json`, `sync.json`,
`tmdb-cache.json`, `run-intent.json`) are `0666`, while `tv-retention.log` and
`journal.jsonl` are `0644`. The application contains no `chmod`, and `server.py` declares
`0o22` as the default mask, under which the `open(..., 'w')` that `core.atomic_json` uses
yields `0644`. The earlier `UMASK` check measured `644/755` on a fresh directory, so it
measured the request; this volume is on Unraid's FUSE `/mnt/user` and every file on it
carries an ACL. The cause is not established here and this is not a claim of a defect. It
is a fact about the target that matters to anyone reading permissions off it: the mode on
the settings document reads as group- and world-writable.

### One tick counted lock contention as a failure

`[ERROR] tick failed: A TV Retention run is already in progress.` at 00:25:52, then
`sync (opened)` at 00:25:57 and a `/health` reading `{"ok": true, "worker": "running"}`.
The collision cleared on its own, so nothing was stuck. Worth naming because of how the
count works: any exception out of `tick()` increments `WORKER['failures']`, and three
consecutive failures mark the worker stuck and take the container unhealthy — so three
ticks in a row landing on a legitimate lock holder would do that to a container that is
working correctly. `actions.py` already tolerates that exact message on the action path.
Not reproduced; recorded so it is not rediscovered later from a health blip.

## Container verification: UMASK, backup mounts, credentials, readiness — 2026-09-21

PLAN §2's remaining items, each checked against a real container rather than by reading
the code. Three passed as they stood; the fourth found a real failure.

**`UMASK` reaches the files the app creates.** `022` gives 644/755 under `/config`,
`027` gives 640/750. The mount point itself keeps the host's mode, which is right — the
app does not own it.

**An unwritable backup mount fails closed and says so.** `backup.create` refuses with
"Backup destination is not writable", writes nothing and leaves no partial archive, and
Status carries a `backup-unavailable` alert without anyone having to attempt a backup
first. Worth recording how nearly this was a false pass: the first attempt ran through
`docker exec`, which is root, and wrote the archive happily. Re-run as uid 99 — the
identity the worker actually has — it refused. A test that runs as the wrong user proves
nothing about the right one.

**Startup refuses a missing login** with its full message and exit 1, refuses a password
under eight characters, and starts with `TVR_AUTH=none`.

**Readiness was not distinct from liveness, and the gap was real.** `/health` returned
`{"ok": true}` unconditionally: it proved the HTTP thread could answer a socket, which
it does perfectly well with the scheduler dead behind it. The worker is a daemon thread,
and `log_line(load_settings(), ...)` sat *outside* its try — so a settings file that
would not parse killed it before the loop started. Demonstrated on a real container:
`running / healthy`, `/health` 200, `worker started` never logged, nothing scheduled
ever again. Only `docker logs` showed the traceback.

Fixing it took two passes, and the first was wrong in an instructive way. Moving the
startup log inside the guard kept the thread alive — but it also swallowed the only
signal there had been, leaving a container that was healthy, silent, and useless. That
is quieter than the crash it replaced, not better. So the loop now also counts what it
is getting done:

- a tick that fails increments a counter and prints the reason to stderr the first time,
  because the log lives in the state directory and the settings say where that is — the
  failures most worth reporting are exactly the ones that cannot be logged;
- three consecutive failures (ninety seconds at a thirty-second tick) marks the worker
  stuck, long enough not to flap on one bad read;
- `/health` answers 503 with `worker: "stuck"` and the reason, and 200 only when the
  thread is alive *and* getting somewhere.

Measured end to end on a real container with a corrupt settings file: `/health` 503 with
the parse error immediately, `tick failed:` in `docker logs`, and Docker's own health
going `healthy → unhealthy` at t+180s (three failed ticks, then three failed probes).
Then, with the file repaired and **no restart**, `/health` returned to 200 within a
tick. The recovery is the point of keeping the thread alive, so it is measured rather
than asserted.

## Restore reload through a background refresh — 2026-09-21

The last item in PLAN §1, and the one with real consequences behind it. A `sync` already
in flight when a restore activates comes back carrying the settings from *before* it.
`applySync` writes `settings` wholesale, so that reply would have put the replaced
document straight back into the page — including `test_mode`, which a restore
deliberately forces on. The page would then show Test Mode off, and the next save would
write that back to disk, quietly undoing a safety guarantee the restore had just
established.

`restoreActivated` already called `stopPolling()`, which looks like it covers this and
does not: it clears the poll timer, the check queue and the checking sets, none of which
is a request already away. There was no cancellation and no staleness check on the
reply, and nothing in the payload says it is stale — only knowing it was asked for
against a document that no longer exists does.

The entry owns the shared documents, so it now stamps them: a counter moved on whenever
something replaces them wholesale, which a full refresh does and therefore a restore
does. Both background sync callers — `checks.requestFreshness` and the top bar's
refresh-all — capture the stamp before asking and discard a reply that no longer
matches. Same shape as the scope-count generation guard, one level up.

Two notes from doing it:

- The first version made `readingGeneration` a `const` arrow. The feature factories are
  constructed above where it is defined, so it was still in its temporal dead zone when
  they took it, and the page threw on load. `applySaved` and `applySync` only work there
  because they are hoisted declarations; this now is one too. The frontend harness
  caught it on the first run, which is exactly what that harness is for.
- The test drives the reload through a completed run rather than through the restore UI.
  A run calls the same `refresh()` a restore does, so it exercises the same seam, and a
  stale sync landing after a run is a real case in its own right.

Checked both ways: with the guard removed from the background sync the test fails on
"the superseded sync must not reinstate the document the reload replaced", and passes
with it.

## Accessibility, scoped to primary actions — 2026-09-21

PLAN had carried "the accessibility and responsive-layout fixes" as an unnamed
placeholder through several passes. Audited and closed; four findings, all fixed.

The one that mattered: a series could only be opened with a pointer. `library.js` built
each card as a `<div>` with a click handler, `openEditor` had no other caller anywhere,
and there was no `tabindex` in the interface at all — so retention settings, exclusions,
the monitoring controls and Delete sat behind a target no keyboard could reach. It is
worth recording how that happened, because it was not carelessness: an earlier change
deliberately removed a read-only card's Edit button as a step that "only ever had one
answer," which was right for pointers and silently took away the only focusable way in.

The card could not simply become a button — it already contains buttons, and interactive
elements cannot nest. The title is now the control, as in any card with a linked
heading: it carries the accessible name, takes focus, and reports `aria-expanded` for
the pane it opens. The whole-card click is unchanged for pointer users, and its handler
already stood aside for anything landing on a real control, so the two do not collide.

The other three were small: the season caret had no accessible name and no expanded
state (two instances of one control); the busy overlay and sweep banner announced
nothing; the dialog had no accessible name.

Recorded so it is not re-audited — already correct beforehand: the notice region is a
polite live region, destructive confirmations use native `showModal()` and inherit focus
trapping, Escape and focus restoration, sidebar heads are real buttons with
`aria-expanded`, nothing resets the focus ring, the custom switch styles
`:focus-visible`, six responsive breakpoints exist including one reflowing the top bar
that holds Run, and `prefers-reduced-motion` is honoured. The placeholder had implied
considerably more outstanding work than actually existed.

Two things the work turned up on the way:

- The CSS nearly repeated a bug the suite already guards against. `font: inherit` on the
  new title button would have reset the `line-height` its two-line clamp is drawn
  against — exactly what `test_buttons_do_not_inherit_the_font_shorthand` exists to
  prevent, just in a new rule its regex did not reach. That test now checks every
  `#tv-retention button…` rule rather than only the bare one.
- Three frontend harnesses had `FakeElement`s with no `setAttribute`, and one with no
  `classList`, so production code that legitimately uses both could not be exercised
  there. Added for real rather than stubbed to no-ops: a `classList.add` that silently
  discards cannot tell a selected card from a plain one.

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

## Build 31 deployment — 2026-09-22

Replaced `dev-build30` with `dev-build31`: the readiness work. Config already at version
14, so nothing migrated. Preflight against a `/tmp` copy, `--rm`: version 14, 32 rules,
4 presets, Test Mode on, schedule off, integrity clean. Stop took **0.37s**. After
replacement: healthy, worker uid 99, posture and timezone unchanged, and `/health` now
answers `{"ok": true, "worker": "running"}` rather than a bare `{"ok": true}`. Rollback
is `tv-retention:rollback` (build 30) plus `pre-build31-<timestamp>.tar.gz`.

## Build 30 deployment — 2026-09-21

Replaced `dev-build29` with `dev-build30`: the stale-sync guard. Config already at
version 14, so nothing migrated. Preflight against a `/tmp` copy, `--rm`: version 14,
32 rules, 4 presets, Test Mode on, schedule off, integrity clean, and the guard
confirmed present in the built image. Stop took **0.18s**. After replacement: healthy,
worker uid 99, posture and timezone unchanged. Rollback is `tv-retention:rollback`
(build 29) plus `pre-build30-<timestamp>.tar.gz`.

## Build 29 deployment — 2026-09-21

Replaced `dev-build28` with `dev-build29`: the accessibility work. Config already at
version 14, so nothing migrated.

- Preflight against a `/tmp` copy, `--rm`: version 14, 32 rules, 4 presets, Test Mode
  on, schedule off, `integrity_errors` clean; the card-title button and the dialog's
  `aria-labelledby` both confirmed present in the built image before it shipped.
- The `docker stop` preceding the swap took **0.46s** rather than running out its 30s
  grace period — the first deployment to benefit from the SIGTERM fix, and incidental
  confirmation of it in production rather than on a test rig.
- After replacement: healthy, worker uid 99, safety posture and stored timezone
  unchanged.
- Rollback is `tv-retention:rollback` (build 28) plus `pre-build29-<timestamp>.tar.gz`.

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

One bounded live run has now exercised the deletion path end to end (above). Restart
recovery interrupted mid-run has since been covered against a synthetic Sonarr, and a
read-only smoke now covers the browser read paths and the volume's permissions (above).

Still without practical evidence: monitoring-write correctness beyond that one run, real
provider behaviour with live keys, API-key lifecycle, backup and restore against a real
configuration, and any browser *write* path — a save, a queued removal, a run. Large-library
performance has a reading, not a measurement: 3019 series render in the list at 385 shown,
and nothing has timed the interaction.

Two items are now *known* rather than missing, and both want a decision rather than a test:

- **Recycle-bin recovery is not available on the target.** Sonarr's `recycleBin` is empty,
  so deletions there are permanent and the earlier run's 2705 MiB was not recoverable.
  Configure Sonarr's recycle bin or accept permanent deletion — but this item cannot be
  recorded as verified.
- **Shared multi-episode files need no live evidence.** A file is judged by its latest
  member (`_order_key` orders by effective date, with season and episode number only
  breaking ties), and a file with any undated or future-dated member votes `unknown`, which
  keeps it under both `combine` modes — no retention mode turns missing information into
  permission to delete. `tests/test_shared_files.py` exercises that through the real Sonarr
  mapping. The one caveat is the documented one: with `retention.allow_estimated_dates` on,
  an episode with no air date is dated by its import instead.

Isolated contract tests do not replace live checks. Keep schedules off and Test Mode on,
and follow the acceptance ladder in [PLAN.md](PLAN.md) before any live write.
