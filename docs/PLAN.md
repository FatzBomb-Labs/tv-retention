# TV Retention plan of record

Updated September 21, 2026. What is left before release. Completed work belongs in git
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

### 1. UI state correctness — closed

Done, and this section is now closed:

- concurrent tabs — the save round-trips `settings_revision` and the server rejects a
  stale one (`test_settings_revision`);
- late scope-count responses — a generation guard in `series-editor.js`;
- the one-time API-key reveal surviving a background render;
- drafts surviving a look at another series;
- the Run confirmation on an untrustworthy plan, which read "No changes are currently
  expected" — a plan nobody had finished reading, stated as fact. The run always reads
  every series fresh regardless, so this was the confirmation's honesty rather than a
  safety hole;
- **restore reload through a background refresh.** A `sync` already in flight when a
  restore activated came back carrying the settings from before it, and `applySync`
  would have put that replaced document straight back into the page — including
  `test_mode`, which a restore deliberately forces on. The page would then have shown
  Test Mode off and the next save would have written it back. `stopPolling()` did not
  help: it clears the poll timer and the check queue, not a request already away. The
  entry now stamps its documents, every background sync captures that stamp before
  asking, and a reply describing a document that has since been replaced is discarded.

Accessibility and responsive layout: audited 2026-09-21, and the four things it found
are done. Scoped deliberately to primary actions; this was not a general WCAG pass,
because an unbounded one is how this section grows rather than closes.

- **A series could only be opened with a mouse** — the headline. `library.js` built each
  card as a `<div>` with a click handler, `openEditor` had no other caller, and there was
  no `tabindex` anywhere in the interface, so retention settings, exclusions and Delete
  all sat behind a target no keyboard could reach. The card could not itself become the
  button: it already contains buttons, and interactive elements cannot nest. The title is
  now a real button carrying the name, the focus and an `aria-expanded` saying whether
  the pane beside it is open; the whole-card click stays for pointers.
- **The season caret** now has an accessible name and a live `aria-expanded`, in both
  tree builders.
- **The busy overlay and the sweep banner** are `role="status" aria-live="polite"`.
- **The dialog** takes its accessible name from the heading its body builds.

Already correct before the audit, recorded so nobody checks them twice: the notice
region is a polite live region; destructive confirmations use native `showModal()`, so
focus trapping, Escape and focus restoration come free; sidebar section heads are real
buttons carrying `aria-expanded`; no stylesheet resets the focus ring, and the custom
switch styles `:focus-visible` explicitly; responsive breakpoints exist at 560, 700,
860, 900, 1100 and 1250px, including one that reflows the top bar holding Run, and
`prefers-reduced-motion` is honoured.

Fix concrete state failures in the existing modules. Do not expand the frontend
architecture.

### 2. Container verification — closed

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

Verified 2026-09-21, each against a real container rather than by reading the code:

- **`UMASK`** reaches the files the app creates — `022` gives 644/755 and `027` gives
  640/750 under `/config`. The mount point itself keeps the host's mode, which is
  correct: the app does not own it.
- **An unwritable backup mount** fails closed and says so. `backup.create` refuses with
  "Backup destination is not writable", writes nothing and leaves no partial archive,
  and a `backup-unavailable` alert appears in Status without anyone having to try a
  backup first. Checked as uid 99: the first attempt ran under `docker exec`, which is
  root, and wrote the archive happily — a false pass worth naming.
- **Startup refuses a missing login** with the full message and exit 1, refuses a
  password under eight characters, and starts with `TVR_AUTH=none`.
- **Readiness as distinct from liveness** — this one needed a fix, and found a real
  failure behind it. `/health` returned `{"ok": true}` unconditionally, which says only
  that the HTTP thread can answer a socket; the worker is a daemon thread, so it could
  be dead behind a container reporting itself healthy. Demonstrated: a settings file
  that would not parse killed the worker at startup and the container stayed `healthy`
  forever. Now the loop survives a document it cannot read, reports itself stuck after
  three failed ticks, and `/health` answers 503 with the reason — so the container goes
  unhealthy (measured: t+180s) instead of lying, and recovers on its own when the file
  is repaired, without a restart (measured: 503 → 200). See VALIDATION.

Closed on 2026-09-22 by the read-only smoke (step 4 of §3) on the running instance: login
and its refusal, the version/build display, one asset digest with unheld digests refused,
browser refresh, sync ages and config/state permissions. It found a recycle bin that is
not there — see §3.

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
rather than the operator's own library — see §2.

Shared multi-episode files need no live evidence and should stop being carried as a gap: a
file is judged by its latest member, and any undated or future-dated member keeps it, which
`tests/test_shared_files.py` exercises through the real mapping.

Recycle-bin recovery is not a gap in coverage but a gap in the target. Sonarr's
`recycleBin` is empty on the instance this points at, so a deletion there is permanent and
the 2705 MiB above was not recoverable. That needs a decision — configure it, or accept
permanent deletion — before any further live run.

**4. Read-only smoke on the target.** Copied config under `/tmp`, schedules off, Test Mode
on. Verify login, version/build display, asset loading and browser refresh, library and
cache ages, read-only Sonarr refreshes, and config/state permissions. Do not save rules,
queue removals, run retention, change monitoring, alter the recycle bin, change API keys
or restore during this step.

Done 2026-09-22 against the running instance, read-only, schedules off and Test Mode on.
One deviation: it ran against the instance's own config rather than a `/tmp` copy, so it
establishes the read paths and the real volume's permissions rather than isolation. See
VALIDATION for what it found.

Docker is unavailable on the Windows development machine, so 2-4 run on the Linux host.

## Ship criterion

The safety list holds, §1 is fixed (it is), §2 is verified against a real container (it
is), the container starts non-root and survives a restart (it does), and one real run
against one real show does what it said it would (it did, 2026-09-20). What is left of 1.0
is one decision rather than one piece of work: the target Sonarr has no recycle bin, so a
deletion there is permanent. Shared multi-episode files need no live evidence.

## Explicitly unresolved

- The target Sonarr has no recycle bin, so nothing it deletes is recoverable. Configure it
  or accept that, deliberately — and do not run against a library whose files matter until
  that is settled.
- Large-library performance, and any browser *write* path — a save, a queue, a run — still
  need practical evidence. The read paths have some now; do not build a new frontend
  architecture to obtain the rest.
- No live target deletion or production canary is authorized by this document.

## Out of scope

Radarr/movie management, watched-state retention, new provider integrations, outbound
notifications/webhooks, direct media access, media mounts, filesystem auditing, frontend
framework rewrites, and AniList as a retention-date source.
