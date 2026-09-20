# TV Retention Final Plan

Plan of record, updated September 20, 2026. This document tracks the work still needed
before a release. Completed implementation history belongs in [VALIDATION.md](VALIDATION.md),
not in this checklist.

## Current state

The working baseline is in place:

- Sonarr owns media metadata, monitoring and deletion. The application has no media mounts.
- Test Mode, queued removals, exclusion protection, shared-file checks and durable recovery
  are implemented.
- Backup creation, restore staging and activation are implemented.
- Provider date safety and civil-time scheduling are implemented. AniList remains disabled
  for retention dates because its current lookup cannot prove season identity.
- The authoritative Linux gate passes: **741 Python tests** and **31 frontend tests**.

This is not release approval. No unattended destructive schedule is authorized until the
remaining acceptance work below is complete.

## Non-negotiable safety

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

### 1. Finish UI state correctness

Required before release:

- Keep Run and confirmation tied to a current, complete plan and current source readings.
- Prevent stale saves, late responses and concurrent tabs from overwriting newer edits.
- Preserve drafts, one-time API-key display and restore reload behavior through background
  refreshes.
- Finish the small accessibility and responsive-layout fixes that affect primary actions.

Do not expand the frontend architecture. Fix concrete state or usability failures in the
existing modules.

### 2. Verify the supported container

Run the application in an isolated container and verify:

- login, configurable port, health/readiness and worker failure reporting;
- malformed HTTP/RPC input is rejected cleanly;
- graceful shutdown and restart recover safely;
- root and non-root startup, `PUID`/`PGID`/`UMASK`, `/config` and optional backup permissions;
- published-image instructions, version/build metadata and base-image renewal.

Keep the existing standard-library design. Do not add a framework or database for this work.

### 3. Complete acceptance in order

1. **Deterministic fixtures:** run the focused backend/frontend checks and the Linux gate.
2. **Isolated container:** exercise the UI-to-worker path with fake Sonarr, including saves,
   restart, recovery, migration and backup/restore. Record exact external requests.
3. **Disposable Sonarr:** use synthetic media only; Test Mode off is allowed only here.
   Verify monitoring, recycle-bin behavior, file outcomes and restart recovery.
4. **Target read-only smoke:** use a copied config under `/tmp`, schedules off and Test Mode
   on. Verify login, display, freshness, permissions and read-only Sonarr access.
5. **Operator canary:** only after steps 1-4 and explicit approval. Use one bounded plan,
   one rollback image and one matching config archive.

Docker and WSL are unavailable on the current Windows development machine, so steps 2-5
must run on the Linux host or another approved environment.

### 4. Keep documentation aligned

- Update README, AGENTS and SECURITY-REVIEW when behavior changes.
- Keep Test Mode, media-filesystem and notification wording accurate.
- Record only current validation counts and environment details in VALIDATION.
- Do not add a new test for every checklist line. Add tests for concrete regressions,
  safety boundaries or data-loss risks; otherwise use the smallest focused check.

## Explicitly unresolved

- AniList cannot contribute retention dates until it can prove series, season and episode
  identity without relying on title similarity.
- Container permissions, HTTP hardening, graceful shutdown and readiness still need an
  actual image-level check.
- Browser reload, real-browser interaction and large-library performance still need practical
  acceptance evidence; do not build a new frontend architecture to obtain it.
- No live target deletion, deployment or production canary is authorized by this document.

## Out of scope

Radarr/movie management, watched-state retention, new provider integrations, outbound
notifications/webhooks, direct media access, media mounts, filesystem auditing and frontend
framework rewrites.
