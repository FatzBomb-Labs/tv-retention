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

- Keep Run and confirmation tied to a current, complete plan and current source readings.
- Prevent stale saves, late responses and concurrent tabs from overwriting newer edits.
- Preserve drafts, one-time API-key display and restore reload behavior through background
  refreshes.
- Finish the accessibility and responsive-layout fixes that affect primary actions.

Fix concrete state failures in the existing modules. Do not expand the frontend
architecture.

### 2. Seed the scheduler's bookkeeping on upgrade

Upgrading from a build that predates `last_occurrence` makes the scheduler read the most
recent occurrence as unanswered and fire a catch-up immediately. Build 23 did exactly
that on the demo instance, harmlessly, because Test Mode was on. With Test Mode off it
would be an unscheduled real run a minute after start.

When `jobs.json` has no `last_occurrence` but does have `last_run`, seed it from the
latest occurrence at or before `last_run` rather than treating it as never answered. An
absent file on a genuinely fresh install must still mean "no history", not "nothing due".

### 3. Container verification

Run the application in an isolated container and verify:

- root and non-root startup, `PUID`/`PGID`/`UMASK`, `/config` and optional backup permissions;
- graceful shutdown and restart recover safely;
- health/readiness and worker failure reporting on a configurable port.

Keep the standard-library design. No framework, no database.

### 4. Acceptance

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

- Container permissions, graceful shutdown and readiness need an actual image-level check.
- Real-browser interaction and large-library performance need practical evidence. Open a
  browser; do not build a new frontend architecture to obtain it.
- No live target deletion or production canary is authorized by this document.

## Out of scope

Radarr/movie management, watched-state retention, new provider integrations, outbound
notifications/webhooks, direct media access, media mounts, filesystem auditing, frontend
framework rewrites, and AniList as a retention-date source.
