# TV Retention plan of record

Updated September 20, 2026. What is left before release. Completed work belongs in git
history, not here; delete from this file as things land.

## Current state

- Sonarr owns media metadata, monitoring and deletion. No media mounts.
- Test Mode, queued removals, exclusion protection, shared-file checks and durable
  recovery are implemented.
- Backup creation, restore staging and activation are implemented.
- Provider date safety and civil-time scheduling are implemented. AniList contributes no
  retention dates.
- The Linux gate passes. [VALIDATION.md](VALIDATION.md) holds the current counts; never
  quote one from memory.

Not release approval: no unattended destructive schedule until the work below is done.

## Non-negotiable safety

This is the part that is earned. Everything else on this page is scheduling.

- Sonarr is the only authority for media metadata, monitoring and deletion.
- A rule must resolve to exactly one current Sonarr series before it can act.
- Exclusions protect monitoring state and every file containing excluded content.
- A file cannot be deleted until every affected episode is successfully unmonitored.
- Ordinary retention never monitors episodes. Monitoring from a queued removal is separate
  operator intent.
- Test Mode blocks every external Sonarr mutation, including manual and scheduled paths.
- Queued removals remain undoable until execution starts and cannot return through stale UI
  state or recovery data.
- Stale, partial or unresolved readings never become deletion permission.

## Remaining work

### 1. UI state correctness

Done: concurrent tabs (the save round-trips `settings_revision` and the server rejects a
stale one — `test_settings_revision`), late scope-count responses (a generation guard in
`series-editor.js`), the one-time API-key reveal surviving a background render, drafts
surviving a look at another series, and the Run confirmation on an untrustworthy plan
(it read "No changes are currently expected" — a plan nobody had actually finished
reading, stated as fact; the actual run always reads every series fresh regardless, so
this was the confirmation's honesty rather than a safety hole).

Left:

- Restore reload behaviour through a background refresh.
- The accessibility and responsive-layout fixes that affect primary actions. Name them
  before starting — "finish the a11y fixes" is not a list, and an unbounded one is how
  this section grows rather than closes.

Fix concrete state failures in the existing modules. Do not expand the frontend
architecture.

### 2. Container verification

Shown incidentally by the build 23 deployment, on the running instance:

- the worker drops to `PUID`/`PGID` — its PID 1 runs as uid 99, gid 100, and everything
  under `/config` is written `nobody:users`;
- three `docker stop -t 30` / start cycles recovered with settings, state and queues
  intact, no run in flight;
- the health endpoint answers on a remapped port (`18787` → `8787`).

Done: graceful shutdown. It did not previously exist — a container is PID 1, and the
kernel applies no default action to an unhandled signal for PID 1 the way it would for
any other process, so `docker stop` was silently running out its full timeout and
falling back to SIGKILL every time. Confirmed against a real container (idle: 10.19s,
exit 137 → 0.21s, exit 0) before and after a SIGTERM handler was added. Also confirmed,
against a synthetic Sonarr and a genuinely interrupted delete: a hard kill mid-write
checkpoints correctly (`in-progress`, not falsely `done`) and a subsequent run resumes
and completes without duplicating the deletion or losing the unmonitor that already
succeeded. See VALIDATION.

Still unverified, and none of it is shown by a container that merely stays up:

- `UMASK`, and permissions on an optional separate backup mount;
- readiness as distinct from liveness, and how a failed worker is reported;
- startup refusing missing or too-short credentials.

Keep the standard-library design. No framework, no database.

### 3. Acceptance

Never against a production library. Use a copied configuration throughout. A read-only
smoke is not permission to run a write-enabled operation.

**1. Deterministic fixtures.** The focused backend/frontend checks and the Linux gate:
worker imports, shipped-module syntax, no skipped mandatory checks. Record the result in
VALIDATION.

**2. Isolated container**, fake Sonarr and a disposable config:

- startup refuses missing or invalid credentials;
- login, configurable port and health endpoint work;
- settings, rules, exclusions, queues and migrations survive restart;
- UI saves and background refreshes do not lose newer edits;
- Test Mode sends no Sonarr mutations;
- backup, restore staging and activation preserve the safety defaults;
- shutdown and restart recover without replaying completed work.

Record exact external requests for anything allowed to write here.

**3. Real run, bounded.** One throwaway series with junk files on a reachable Sonarr,
Test Mode off, one rollback image and one matching config archive. Verify monitoring
changes, shared multi-episode files, recycle-bin behavior, partial failure and restart
recovery, and truthful run history, journal and byte totals. Restore Test Mode on and
schedules off when finished.

Done in part: a bounded run on 2026-09-20 (2 planned, 2 deleted, 2705 MiB, 0 errors,
`test_mode: false` in the journal — see VALIDATION) exercised the deletion path itself.
Restart recovery interrupted mid-run is now also covered, against a synthetic Sonarr
rather than the operator's own library — see §2. Not covered by either: shared
multi-episode files and recycle-bin recovery, both of which need the real library.

**4. Read-only smoke on the target.** Copied config under `/tmp`, schedules off, Test Mode
on. Verify login, version/build display, asset loading and browser refresh, library and
cache ages, read-only Sonarr refreshes, and config/state permissions. Do not save rules,
queue removals, run retention, change monitoring, alter the recycle bin, change API keys
or restore during this step.

Docker is unavailable on the Windows development machine, so 2-4 run on the Linux host.

## Ship criterion

The safety list holds, §1 is fixed, the container starts non-root and survives a restart,
and one real run against one real show does what it said it would. That is 1.0.

## Explicitly unresolved

- Readiness and backup-mount permissions need an image-level check; the privilege drop,
  plain restart recovery, graceful shutdown and mid-run interruption recovery no longer do.
- Real-browser interaction and large-library performance need practical evidence. Open a
  browser; do not build a new frontend architecture to obtain it.
- No live target deletion or production canary is authorized by this document.

## Out of scope

Radarr/movie management, watched-state retention, new provider integrations, outbound
notifications/webhooks, direct media access, media mounts, filesystem auditing, frontend
framework rewrites, and AniList as a retention-date source.
