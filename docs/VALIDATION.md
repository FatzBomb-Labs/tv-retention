# Validation record

## Phase 4 dates and scheduling — 2026-09-20

The implementation passed the authoritative Linux host gate on `fatzserver-host`: **741
Python tests in 10.027s** and **31 frontend tests**, with `All required checks passed` and
exit code `0`. Worker imports and shipped module syntax checks also passed. No deployment,
live Sonarr request, media access, Docker acceptance or WSL installation was performed.

Focused Phase 4 evidence covers:

- provider safety: ambiguous TMDB external-ID matches are rejected; unique matches resolve;
  Sonarr dates are never overwritten; TVMaze preserves season `0`; provider failures leave
  missing dates unresolved; persisted provider caches are reused and expired entries refresh;
  AniList, Plex and Jellyfin cannot be enabled as retention-date providers;
- interpolation and acquisition fallback: only bounded air-date gaps are estimated;
  leading/trailing gaps remain unresolved; acquisition dates use import events rather than
  grabs, upgrades or deletions;
- scheduling: all cron fields validate, IANA zones are checked, sparse annual schedules
  catch up within the bounded ten-year search, nonexistent spring-forward times are skipped,
  repeated fall-back times receive distinct UTC occurrence IDs, and pending recovery cannot
  run twice in one tick;
- persistence/UI/image integration: settings version 14 migrates the timezone default,
  `jobs.json` persists `last_occurrence` and `pending_run`, the schedule UI round-trips the
  IANA timezone, unsupported providers are visibly disabled, and the image installs `tzdata`
  with `TZ=Etc/UTC` as its default.

AniList retention dates remain intentionally disabled because the current title-based query
does not prove season identity. Container permission, browser reload, Docker and WSL
acceptance remain outside this gate and remain open in the plan.

## Phase 2 durability completion — 2026-09-19

The Phase 2 durability work passed the authoritative Linux host gate on
`fatzserver-host`: **712 Python tests in 9.411s** and **31 frontend tests**, with
`All required checks passed` and exit code `0`. Worker imports and shipped module syntax
checks also passed. No deployment, live Sonarr request or media access was performed.

Focused evidence included:

- **15 settings-revision tests:** full-document stale writes reject; binding, sync and
  expiry writers merge only their owned fields; operator resolution preserves failure
  propagation and newer settings.
- **64 freshness tests:** re-enable behavior remains correct after commit-time merging.
- **14 mode-boundary tests:** the new held-lock regression proves `tick()` exits before
  reading settings, contacting Sonarr or writing `jobs.json` when another run owns the
  lock.
- **29 store tests:** required cache/state writes raise `StorageError`; progress markers
  stay best effort; strict configured state paths fail closed; journal evidence precedes
  compact run-state replacement and remains retryable after a replacement failure.
- **3 removal-resolution tests:** operator cancellation still leaves the queue intact when
  the settings write fails, and successful resolution remains request-scoped.

The scheduler now holds one non-blocking `run.lock` across its complete bookkeeping cycle,
with nested lock acquisition removed. Authoritative settings, intent, run history,
scheduler state, health cache and removal-ledger failures are no longer silently converted
into empty state. Background settings writers use revision-checked owned-field merges;
stale full-document writers are rejected. Phase 3 backup/restore is now implemented through
explicit staging and activation; the separately documented atomic in-progress Test Mode
transition remains open.

## Phase 3 backup creation, staging and activation — 2026-09-19

The focused backup suite passed **11 tests in 0.645s** on Windows. The slice covers:

- archive creation under the settings transaction with per-file SHA-256 manifest values;
- Windows temporary-file descriptor cleanup and same-second concurrent publication;
- retention pruning limited to owned archives while retaining the newly created archive and
  unrelated destination files;
- manifest/member/path validation, bounded expanded bytes and streamed extraction limits;
- malformed settings, tampered archive content and active-config preservation;
- restore staging with Test Mode forced on, schedules disabled, executable queues and run
  state quarantined, and a durable `restore-staging.json` marker reloadable after restart;
- explicit activation under the existing `run.lock`, typed confirmation and pending-work
  review, replacement of files absent from an older archive, and rollback after interrupted
  snapshot/install phases.
- Backup UI staging and activation controls, quarantine display, polling stop, draft discard
  and fresh-snapshot reload. The local frontend contract suite passed **166 build tests** and
  **31 executable frontend tests** after this change.

The read-only backup listing now reports the durable staged-restore marker. The legacy direct
`restore` operation remains refused; the new explicit `stage` and `activate` operations do
not contact Sonarr and always install Test Mode with schedules disabled. The authoritative
Linux host gate then passed **723 Python tests in 9.594s** and **31 frontend tests**, with
`All required checks passed`. Container permission and optional backup-mount acceptance could
not be run on the Windows development box: Docker is not installed or on `PATH`, and WSL is
not installed. No container was started. No deployment, live Sonarr request or media access
was performed.

## Build 22 deployment — 2026-09-18

The committed source checkpoint is `a18d758` (`Refresh UI after runs and fix specials
saves`) with `BUILD=22` and `VERSION=0.3.0`. The authoritative Linux host gate passed
**684 Python tests in 9.926s** and **31 frontend tests**; all required checks passed.
The build-contract test slice also passed **165 tests** after updating the assertions for
the optional forced-sync request.

The candidate passed disposable preflight as `tv-retention:dev-build22` with no network,
a copied config, scheduling disabled and Test Mode forced on. The preflight verified build
22 metadata, the corrected executor signature, the Sonarr file-delete method and a valid
settings snapshot containing 32 rules.

The candidate was deployed as `tv-retention:dev-build22`, image ID
`sha256:29e9c3b1017985673dbb50e5fca01d032afb169df646453bef09bcd04ea2d5d`.
The container uses the existing `/tmp/tvr-demo/config` volume and runtime settings, is
healthy, uses `unless-stopped`, and serves `GET /health` as `{"ok": true}`. Exactly one
`tv-retention-demo` container is running. The live image contains the new sync-status and
post-run refresh assets.

The live config remained intact: Test Mode is enabled, the configured schedule remains
enabled, and the last completed run `6c6c556a9cbf` still reports **7 deleted** with no
errors. Its completed intent was preserved through replacement; no incomplete recovery
intent was replayed at startup.

Build 21 is retained as the rollback image under both `tv-retention:rollback` and
`tv-retention:dev-build21`. The matching config archive is
`/tmp/tvr-demo/rollback-build21-config-20260918T094425Z.tar.gz` with SHA-256
`330e58d07228c7a3ea1b23e0c75de2a3303adb5591e272523d210e5bf67898d2`. Superseded TV
Retention image tags and the older build-20 rollback archive were removed; no global
container or image cleanup was used.

## Interrupted host-validation cleanup — 2026-09-18

The host-validation wrappers now create a unique lease for each disposable staging
directory. The local shell or PowerShell runner refreshes that lease while the remote
gate is alive. A remote watchdog removes the staging directory and lease after the
refresh stops, while the normal exit trap still removes both immediately. This covers
an abrupt local SSH-client loss without changing the application, its config volume or
any Sonarr state.

Evidence:

- The authoritative wrapper gate passed **684 Python tests and 31 frontend tests**, with
  `All required checks passed` and exit code `0` after the lease change.
- A disposable remote fault probe created unique `/tmp/tv-retention-interrupt.*`, lease
  and marker paths, killed the local SSH client after readiness, and observed
  `marker=expired staging=removed` within the bounded probe window. The unique marker
  was then removed; no application staging or `/tmp/tvr-demo/config` path was used.
- The probe exercises the lease/watchdog cleanup protocol directly rather than
  interrupting a full test suite mid-case. It establishes bounded stale-staging cleanup;
  it does not claim that an arbitrary remote process outside this wrapper is terminated
  by SSH loss.

## Safe-restore mutation boundary — 2026-09-18

The bounded Linux fixture matrix passed **13 tests in 0.077s**. It exercises the real
action and CLI boundaries against the scripted Sonarr transport in both Test Mode and
live mode, with exact ordered fake requests and isolated temporary config/state.

Covered entry points are manual run, scheduled run, CLI run, due tick, picker monitoring,
scope pass, recycle-bin configuration, queued series removal, preview and restore refusal.
Test Mode and preview produce no Sonarr mutation and do not create run state. Live cases
send only the expected fixture requests. Restore is refused before calling `backup.restore`
in both modes, leaves the settings document unchanged and sends no Sonarr request.

The Windows copy of this focused file cannot run because the worker imports POSIX `fcntl`;
the authoritative Linux host result is the release evidence. Safe archive activation is
still deliberately disabled until the Phase 3 backup/restore work adds maintenance
exclusion, quarantine and safe pending-work activation.

## Latest implementation — 2026-09-18 (backup status merge)

Backup archive creation now records `last` and `last_error` by merging only backup-owned
fields into the newest settings document under the short settings transaction. A settings
save that completes during archive creation is preserved rather than overwritten by the
older request snapshot.

The focused settings-revision suite passed **6 tests in 0.013s** on `fatzserver-host`.
The regression performs a valid public settings save during the archive operation and
verifies that the newer retention window and the backup timestamp both survive. This is
a bounded owned-field commit fix; other internal writers, commit-time rechecks and full
transaction ordering remain open in Phase 2.

The same focused suite now passes **7 tests in 0.014s** after adding a health-cache
transaction boundary. Operator-owned alert acknowledgement and suppression survive a
stale background health write, while the newer background instance reading is retained.
This protects the two operator-owned health fields; the broader health writer ordering
and cross-process cache durability model remain open in Phase 2.

The full authoritative gate then passed **686 Python tests in 9.676s** and **31 frontend
tests**, with all required checks passing. No deployment or live Sonarr request was made.

## Latest implementation — 2026-09-18 (strict settings and intent authority)

Read-only settings views retain a repairable document when structural validation fails, but
mutating and external-write RPCs now use a strict loader and refuse the saved authority.
The settings-save action remains available as the explicit repair path. Status exposes a
blocking `settings-invalid` alert without replacing the stored document.

The executable `run-intent.json` record now has a strict loader. A missing record remains a
normal fresh-install state; malformed JSON, unreadable content or a non-object record is a
blocking `intent-invalid` condition. Live runs validate that record before binding rules or
making any Sonarr request, so damaged recovery state cannot be mistaken for a clean start.

Focused Linux validation passed **17 store tests** and **10 settings/revision tests**. The
complete authoritative gate passed **691 Python tests in 9.213s** and **31 frontend tests**,
with all required checks passing. No deployment, live Sonarr request or media access was
made. Broader cache transaction ordering and full intent shape validation remain open in
Phase 2.

## Latest implementation — 2026-09-18 (idempotent run finalization)

Completed intents now carry their rendered summary before local bookkeeping begins. On a
restart, if `state.json` or `journal.jsonl` is missing, the worker replays only that local
finalization and returns the existing summary without contacting Sonarr or creating a new
logical run. The store-level recorder deduplicates state and journal entries by run ID under
the short local transaction.

Focused Linux validation passed **18 store tests** and **9 public-run contract tests**. The
process-interruption suite passed **5 tests in 2.908s** and the retention-retry suite passed
**5 tests in 0.042s**. The complete authoritative gate passed **693 Python tests in 9.738s**
and **31 frontend tests**, with all required checks passing. No deployment, live Sonarr
request or media access was made. Broader storage-failure ordering and full transaction
coordination remain open in Phase 2.

## Latest implementation — 2026-09-18 (strict intent shape validation)

Strict `run-intent.json` loading now validates the executable record identity, status,
collections, operation identity, operation kind/status/attempt fields, and kind-specific
fields such as episode lists, file membership, and series deletion flags. Malformed nested
operations are exposed through the blocking `intent-invalid` Status alert and refuse live
runs before any Sonarr request.

Legacy compatibility is preserved: completed intents from before summary persistence remain
recoverable through the removal ledger, and removal operations without target snapshots stay
held for the existing review-required path rather than being silently discarded.

Focused Linux validation passed **19 store tests** and **11 settings/revision tests**. The
recovery compatibility suites passed **6, 17, 6, 8 and 3 tests** respectively. The complete
authoritative gate passed **695 Python tests in 9.237s** and **31 frontend tests**, with all
required checks passing. No deployment, live Sonarr request or media access was made.

## Latest implementation — 2026-09-18 (strict run-history authority)

`state.json` now has a strict loader for authoritative run history. Malformed or unreadable
history is exposed through the blocking `state-invalid` Status alert. Run finalization and
clear-history refuse to overwrite it, and live runs validate state before binding rules or
making any Sonarr request.

Focused Linux validation passed **21 store tests** and **13 settings/revision tests**. The
complete authoritative gate passed **699 Python tests in 9.377s** and **31 frontend tests**,
with all required checks passing. No deployment, live Sonarr request or media access was
made. Scheduler bookkeeping and broader cache/storage failure ordering remain open in
Phase 2.

## Latest implementation — 2026-09-18 (strict scheduler-state authority)

`jobs.json` now has a strict loader for `pending_run`, `last_run`, and
`last_connectivity`. Malformed scheduler state is exposed through the blocking
`jobs-invalid` Status alert, and scheduler ticks refuse it instead of treating a damaged
document as an empty scheduler state that could drop a queued run.

Focused Linux validation passed **23 store tests** and **14 settings/revision tests**. The
complete authoritative gate passed **702 Python tests in 9.859s** and **31 frontend tests**,
with all required checks passing. No deployment, live Sonarr request or media access was
made. Disposable cache readers and broader storage-failure ordering remain open in Phase 2.

## Latest implementation — 2026-09-18 (strict health-cache authority)

Locked health-cache merges and operator acknowledgement/suppression updates now refuse a
malformed `health.json` rather than replacing it with an empty reading. Status exposes the
blocking `health-invalid` alert, while tolerant read-only views remain available for repair.

Focused Linux validation passed **25 store tests** and **15 settings/revision tests**. The
complete authoritative gate passed **705 Python tests in 9.863s** and **31 frontend tests**,
with all required checks passing. No deployment, live Sonarr request or media access was
made. Per-rule episode-cache integrity and broader storage-failure ordering remain open in
Phase 2.

## Latest implementation — 2026-09-18 (per-rule cache recovery)

Malformed per-rule episode readings are now covered by regression tests: normal planning
refetches them from Sonarr, while offline planning rejects them instead of treating an empty
cache as a valid plan. This preserves the existing disposable-reading contract without
turning one damaged series cache into a system-wide block.

The focused Linux freshness suite passed **64 tests**. The complete authoritative gate then
passed **707 Python tests in 9.851s** and **31 frontend tests**, with all required checks
passing. No deployment, live Sonarr request or media access was made. Broader storage-failure
ordering remains open in Phase 2.

## Executor image correction — 2026-09-18

The source executor fix is present at workspace revision `7b2ae77`: staged operations
checkpoint before dispatch, call Sonarr, and checkpoint the acknowledged result. The
authoritative host gate passed **684 Python tests in 9.407s** and **29 frontend tests**;
all required checks passed.

A corrected candidate image was built and deployed as `tv-retention:dev-build21` with
image digest `sha256:c2c3d38d57bee3f33d88b5f6deabc707f87ebffe4e077274c66a8dfed866c428`.
The replacement preserved `/tmp/tvr-demo/config`, used the same port and runtime
settings, and passed Docker health plus `GET /health` (`{"ok": true}`). The previous
image remains tagged `tv-retention:rollback`, with config archive
`/tmp/tvr-demo/rollback-build20-config-20260918T071921Z.tar.gz` and SHA-256
`4a67c5c993e8d87f6089efc2d9634ccab36b46c3da50cd14c1044d0e90dffe10`.

The incomplete `run-intent.json` from the failed old image was preserved unchanged;
its nine operations remain held for explicit recovery review. The new worker did not
replay them at startup, and Test Mode remains enabled. No new Sonarr mutation was
performed during deployment.

## Latest implementation — 2026-09-18 (sync owned-field merge)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **684 Python tests in 9.698s**,
**29 frontend tests**, all passing. Worker imports and shipped Python/JavaScript syntax
checks passed. No deployment, live Sonarr call or media access occurred.

`sync_from_sonarr()` now reloads the latest settings after a Sonarr library read and merges
only auto-reenable-owned rule fields (`enabled`, `auto_reenable` and its watermark) before
the short settings write. The fail-first regression reproduced a newer user `keep_days=90`
being overwritten to `30`; it passes with the merge. The broader freshness, revision,
recovery and retry slice passed **95 tests**.

This closes the bounded sync auto-reenable overwrite path. Other internal settings writers,
health/cache ownership, commit-time version rechecks and full transaction ordering remain
open in Phase 2.

## Latest implementation — 2026-09-18 (owned-field background merge)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **683 Python tests in 9.762s**,
**29 frontend tests**, all passing. Worker imports and shipped Python/JavaScript syntax
checks passed. No deployment, live Sonarr call or media access occurred.

`bind_rules()` now reloads the latest settings after its Sonarr read and merges only
backend-owned binding fields by rule ID before saving. The fail-first regression reproduced
the stale snapshot overwriting a newer user `keep_days` value (`90` became `30`); it passes
with the merge, preserving the user edit while updating match metadata. The broader
settings/freshness/recovery/retry slice passed **94 tests**.

This closes the bounded background-binding overwrite path. Other internal settings writers,
health/cache ownership, commit-time version rechecks and full transaction lock ordering
remain open in Phase 2.

## Latest implementation — 2026-09-18 (bounded settings transaction)

The full host gate passed **682 Python tests in 8.727s** and **29 frontend tests**, with
all worker imports and shipped syntax checks passing. No deployment, live Sonarr call or
media access occurred.

The central whole-document settings action now holds a short `settings.lock` transaction
only while it reloads, checks `settings_revision`, validates and atomically writes the
document. Logging, cache invalidation, health refresh and any Sonarr reads occur after the
lock is released. Linux coverage passed **32 focused tests**, including concurrent saves
where one revision succeeds and the stale writer is rejected. Windows filesystem-only
store tests pass; Linux directory-fsync tests are explicitly skipped there because the
platform lacks `O_DIRECTORY`.

This is bounded coordination, not the complete Phase 2 model: internal settings writers,
owned-field/background merges, a uniform lock order and commit-time version rechecks for
all background updates remain open.

## Latest implementation — 2026-09-18 (settings revisions)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **681 Python tests in 9.017s**,
**29 frontend tests**, all passing. Worker imports and shipped Python/JavaScript syntax
checks passed. No deployment, live Sonarr call or media access occurred.

Settings documents now carry `settings_revision`, defaulting to `0` for older documents.
Whole-document settings saves must echo the current revision; a stale document is rejected
before validation or mutation. Successful settings writes and internal settings writes
advance the revision, and the frontend already resubmits the current document through its
existing `Object.assign(settings(), ...)` collector. The focused revision/settings/queue/
removal slice passed **69 tests**. The stale-save regression was observed failing before
the guard because the older document could overwrite the newer keep window, then passed
after the guard.

This is conflict detection, not the complete Phase 2 transaction model: concurrent reads
and writes are not yet serialized, background updates are not merged by owned fields, and
an authorization check is not atomic with the final replace. Those remain open.

## Latest implementation — 2026-09-18 (atomic writes and validation boundary)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **679 Python tests in 9.018s**,
**29 frontend tests**, all passing. Worker imports and shipped Python/JavaScript syntax
checks passed. No deployment, live Sonarr call or media access occurred.

`core.atomic_json` now gives every write a UUID-suffixed temporary path instead of sharing
one PID-based path. The fail-first Linux regression reproduced concurrent writers deleting
one another's temporary file (`FileNotFoundError`), then passed after the change. The
focused atomic-write and existing round-trip cleanup tests passed **2 tests**; the complete
gate passed **679 Python and 29 frontend tests**.

Phase 0 validation evidence also includes two complete host gates run concurrently. Each
passed **678 Python and 29 frontend tests** with independent disposable staging and cleanup.
The later lease-watchdog fault probe killed the local SSH client and observed bounded
cleanup with `marker=expired staging=removed`; see the dedicated record above. Injected
fsync/replace failure coverage and serialized transactions remain open in Phase 2; safe
restore remains intentionally disabled until Phase 3.

## Latest implementation — 2026-09-18 (Phase 1 safety checkpoint)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **678 Python tests in 9.538s**,
**29 frontend tests**, all passing. Worker imports and shipped Python/JavaScript syntax
checks passed. No deployment, live Sonarr call or media access occurred.

Ordinary file-delete operations now retain the complete set of episode IDs sharing their
Sonarr file ID. Recovery requires the original episode row, exact file ID and unchanged
membership before it can reconcile a lost acknowledgement. Missing, replaced or changed
membership remains review-required and sends no follow-up mutation. The focused recovery,
restart and retry slice passed **16 tests in 2.940s**; the new membership regression was
observed failing before the guard (`Rejected not raised`) and passing after it.

This closes the bounded Phase 1 file-authority item. Together with the preceding Test Mode,
target-authorization, interruption and operator-resolution checkpoints, the Phase 1 P0
safety work is complete. Multi-process transaction ordering, atomic Test Mode transition,
ledger lifecycle/compaction and safe restore remain open in Phases 2 and 3.

## Latest implementation — 2026-09-17 (operator resolution)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **677 Python tests in 9.483s**,
**29 frontend tests**, all passing. Worker imports and shipped Python/JavaScript syntax
checks passed. No deployment, live Sonarr call or media access occurred.

Held queued removals now have a cancel-only operator action. `resolve-removal` requires
the current rule ID, request ID and action; it clears only a matching current queue, marks
unresolved ledger/intent operations as `resolved` with an explicit uncertainty note, and
never sends a Sonarr request. A stale request cannot cancel a requeued removal. Review
state is persisted before queue removal, and a local settings-save failure leaves the
queue available for retry. The focused regression passed **3 tests in 0.007s**.

The report exposes this action only for unresolved operation-backed removal records and
states that no Sonarr retry will occur. Retired record-only failures do not offer a stale
resolution button. `resolved` is terminal for recovery but remains distinct from successful
`done` work. This closes the operator-resolution item in Phase 1; atomic mode/dispatch,
cross-document saves and full ledger lifecycle remain open for later durability work.

## Previous implementation — 2026-09-17 (explicit removal target authorization)

Final `tools/check-on-host.ps1` gate on `fatzserver-host`: **665 Python tests in
6.525s**, **29 frontend tests** (390.432708 ms), all passing. Worker imports and shipped
Python/JavaScript syntax checks passed. Checks used disposable host staging and isolated
fixtures. No commit, deployment, container launch, live Sonarr call or media access occurred.
The pre-existing uncommitted Test Mode work is retained.

New external removal submissions through `action_settings` freeze server-owned instance ID,
normalized URL, series ID, TVDB ID and canonical path. Existing request echoes retain the
original server snapshot, ignoring client-supplied target edits. Validation preserves that
snapshot without upgrading legacy queues. Stage records copy it before reads can fail.
One verifier reloads current settings, checks request/action/target continuity, reads fresh
`series_one` identity and returns the current client for dispatch. It is used for staging,
reconciliation and dispatch. Legacy external requests/operations without matching evidence
remain held; local remove-rule needs no remote snapshot. Finalization compares the same
local target evidence without trying to reread an acknowledged deleted series. A bare 404
never completes an uncertain deletion: it reports **cannot verify absence; review required**.

Evidence:
- Fail-first full gate: **661 Python tests in 6.403s, 15 failures**, covering snapshot
  ownership, legacy external queues, uncertain-delete 404 and URL/TVDB/path/numeric-ID
  changes before staging, operation-backed retry and dispatch. Frontend: **29 passed**
  (391.262471 ms).
- The first integration gate had **45 failures and 1 error** (661 tests in 6.402s): the
  new target tests passed, while older strict fixtures lacked the new identity reads,
  realistic target snapshots or still expected 404 to complete deletion. Those fixtures
  were updated explicitly; the ordered fake transport was not relaxed. Existing cancellation,
  ownership, subset-read and no-replay assertions remain. The next full gate passed
  **661 in 6.481s**, frontend **29** (392.772742 ms).
- Post-fix additions cover record-only restaging under all four identity changes, a legacy
  operation with request ID but no target, current credentials at dispatch, acknowledged
  deletion without a final remote read, and new-request record-only recovery accepting the
  same target but refusing a changed target. These additions were not observed fail-first.
  They are included in the final 665-test gate above.

Limits: these are real action/run calls with fixture transport and same-process persisted
retry, not process-kill/restart or live-service evidence. URL/path/TVDB/numeric identity is
not proof of server provenance or protection against an indistinguishable replacement.
Verification and dispatch are not atomic; settings revisions, concurrent saves, mode-transition
coordination, checkpoint durability and full ledger validation remain Phase 2 work.
Malformed episode rows and replaced file membership are not solved by series identity.
Operation-backed uncertainty still lacks an operator resolution workflow; 404 remains held
rather than providing automatic absence reconciliation. Phase 1 is not complete and
no deployment is authorized by this gate.

## Previous implementation — 2026-09-17 (bounded Test Mode write guard)

`tools/check-on-host.ps1` on `fatzserver-host`: **657 Python tests in 5.845s**, **29
frontend tests** (367.64644 ms), all passing; worker imports and shipped Python/JavaScript
syntax checks passed. The preceding gate also passed (657 Python in 5.844s, 29 frontend).
Focused Linux suite `test_mode_boundary test_monitoring test_run_contract test_shared_files
test_sonarr`: **76 tests in 0.084s**, passing. `git diff --check` is clean. No commit,
deployment, container launch, live Sonarr request or media access occurred; host checks
used disposable staging and isolated fixtures.

All shipped application Sonarr constructors now use `main.sonarr_client`. Before every
non-GET request its guard reloads and validates saved settings, permitting only explicit
Test Mode off and failing closed on load/validation errors. Standalone `Sonarr` remains
policy-free unless supplied a guard. Settings stay editable. Scope passes return actual
applied and skipped counts, including partial passes and no-op Test Mode passes; the
editor reports the skip and does not submit its separate picker changes. Picker and
recycle-bin writes reject clearly. Restore RPC is refused in **both modes**, before
calling `backup.restore`; safe archive activation is not implemented.

Evidence and limits:
- Fail-first picker regression: **1 test in 0.035s failed**, recording an unexpected
  `PUT episode/monitor` under Test Mode. The same test passed after the guard (**0.037s**).
- Fail-first editor regression: **6 passed, 1 failed** because it still submitted picker
  changes after a skipped pass; all **7 passed** after the UI change (114.1157 ms).
- The other boundary cases were added after implementation. Real action/run transport
  tests cover settings/scope/picker/recycle, manual and scheduled runs, preview and queued
  series deletion in both modes, missing/corrupt/invalid settings, a reused client, and
  same-process retry preserving an existing intent under Test Mode. CLI parsing invokes
  the real run; a due tick invokes it with due/connectivity/background-sync decisions
  stubbed. These are not process-restart or scheduler-timing proofs.
- Toggle tests change saved mode during the first acknowledged PUT: the next write is
  blocked, the run is incomplete without a DELETE, and a partial scope pass reports only
  acknowledged flags as applied. **The check and dispatch are not atomic**: a request
  already past the check may still be sent or complete after toggle acknowledgement.
  Concurrent whole-document saves can still overwrite a newer mode. Phase 2 coordination
  remains required; Phase 1 is not complete and deployment remains blocked.
- Test Mode permits local configuration and operational writes (backup creation/listing,
  caches, health, progress, logs and job bookkeeping). Runs entered in Test Mode/preview
  do not persist executable plans or consume queued work. A live run toggled mid-flight
  retains checkpoints/error bookkeeping for work already attempted. This is not a blanket
  local read-only mode or authorization for a live smoke to invoke save/restore/Run.
- Restore tests prove RPC refusal without calling archive activation or changing settings;
  they do not prove safe restoration. No recovery-target identity changes were made.

Intermediate failures were test integration issues, not claimed safety regressions: the
first expanded focused run (73 tests) had six subtest failures from using local-only
`remove` instead of `delete-series`; the first full gate (657 in 6.331s) had 19 freshness
stub signature errors. The fixture now expects the actual series lookup/deletion, and
freshness stubs target the application factory without weakening read-only assertions.

## Previous implementation — 2026-09-17 (bounded frontend rule-save preservation)

Final combined Linux gate: **644 Python tests in 6.244s**, **28 frontend tests**, all
passing; worker imports and all shipped Python/JavaScript syntax checks passed. No live
calls or deployment. A focused shared-file follow-up passed **6 tests in 0.038s**,
including post-fix tests for unknown/future siblings and episode-count semantics.

Earlier Windows-only, fixture-backed checks:

- `node --experimental-vm-modules --test tests/frontend/*.test.js`: **28 passed,
  0 failed, 0 skipped** (1646.8144 ms), including six new editor-factory regressions.
- With `PYTHONUTF8=1`, `.venv/Scripts/python.exe -m unittest discover -s tests -p
  test_build.py`: **165 tests passed** (0.138s). No source-text assertions needed changes.
- `git diff --check`: clean.

The first five new regressions were observed failing against the original handler and
passing after the patch. They cover latest episode and whole-season exclusions plus
`auto_reenable_after`/independent metadata, actual removal Undo while the editor stays
open through redraw/save/reopen, stable-ID targeting after reordered settings/matching,
new-rule targeting by instance/series identity, and rejection of a removed existing rule
without saving or resurrecting it. A sixth post-fix test covers disappearance in the match
response: no follow-up monitoring or check may target another rule.

The save merges only form-owned fields onto the latest frontend-held rule. Queue banners
also read current settings. These tests use fake DOM/timers and fixture RPCs, not backend
persistence; a serialized fixture round-trip verifies payload preservation, not worker
validation. Transactions, multi-tab conflicts and stale in-flight responses remain open
in PLAN Phase 2. The combined Linux gate above validates the current checkout; the
shared-file gate below records the preceding checkpoint.

## Previous implementation — 2026-09-17 (shared files)

**The Linux gate passes; deployment remains blocked by PLAN's remaining P0 work.**
`tools/check-on-host.ps1` on `fatzserver-host` ran **642 Python tests in 6.237s**, all
passing, plus **22 frontend tests**, all passing. The preceding checkpoint passed
**638 Python tests in 6.221s** and **22 frontend tests**.

Shared multi-episode files now expire with their latest member episode. The cutoff
regression was observed failing before the patch: with episodes 1 and 2 sharing file 99
and only episode 2 inside the keep window, the run sent `PUT episode/monitor` for
`[101, 102]` where the fixture expected no mutations (**1 test in 0.030s**). After the
change, `evaluate()` votes per file using the newest member, an exclusion on any member
protects the file, and `process_rule()` deduplicates deletions by file, counting size
once. A member still monitored outside the run's unmonitor list blocks the file; a
failed unmonitor leaves the run incomplete with no delete. Four tests cover the cutoff,
expired deletion counted once, excluded-sibling protection and failed-unmonitor
protection. No deployment or live request occurred.

## Previous implementation — 2026-09-17

**The Linux gate passes; deployment remains blocked by PLAN's remaining P0 work.**
`tools/check-on-host.ps1` on `fatzserver-host` ran **638 Python tests in 6.221s**, all
passing, plus **22 frontend tests**, all passing. The preceding unified-routing checkpoint
passed **632 Python tests in 5.144s** and **22 frontend tests**. The preceding ledger checkpoint
passed **629 Python tests in 6.126s** and **22 frontend tests**. The preceding finalization checkpoint
passed **621 Python tests in 5.020s** and **22 frontend tests**. The preceding mixed-replan checkpoint
passed **620 Python tests in 5.514s** and **22 frontend tests**. The preceding identity checkpoint passed
**618 Python tests in 4.996s** and **22 frontend tests**. The preceding legacy-retirement checkpoint
passed **610 Python tests in 5.420s** and **22 frontend tests**. The preceding recovery-error checkpoint
passed **610 Python tests in 5.937s** and **22 frontend tests**. The canceled-removal checkpoint
also passed (**606 Python tests in 5.913s**, repeated in **5.912s**). Earlier gates for
persisted-intent retry (**605 in 5.406s**) and reporting fixes (**604 in 5.398s**) also passed,
each with **22 frontend tests**.
Every shipped worker module was
separately imported and syntax-checked; every shipped JavaScript module was syntax-checked.
No deployment, live Sonarr request, or media access occurred; staging was disposable.

The executor now owns dispatch and completion, receives its intent explicitly, and stops
the run after a failed operation. Recovery no longer dispatches the last loop variable.
Before/after checkpoint failures propagate rather than permitting another write. These
are partial fixes, not evidence of safe recovery authorization or shared-file deletion.

Evidence:
- Removal-only routing (`09ac1dc`) now uses the ledger rather than resuming a whole old
  intent. The unrelated-subset regression failed before the patch: `run(rule_ids=['r2'])`
  attempted an episode read for `r1`; **630 tests in 5.657s** had that one failure. After
  routing changed, six failures exposed five existing test contracts (the recovery-read
  test has two subcases). Cancellation and unavailable/malformed recovery now return an
  incomplete result with errors, not a raised exception; tests still require no additional
  mutations and unchanged checkpoints. Cancel/requeue also checks preservation of the new
  request. The two-removal test now checks per-request read/write order and ledger attempts
  `[2, 1]`, with exactly three writes including the initial failed one. It was renamed to
  describe persisted retry, not restart. Finalization now checks a new run ID, exact archive
  and unchanged acknowledged ledger operation instead of reusing the old run ID. Two tests
  for inconsistent completed intents were added after implementation, not observed fail-first.
  Review found no introduced routing regression. The gate passed **632 in 5.144s**.
- Record-only staging retry: the first public-run regression was observed failing before
  the patch (**1 test in 0.031s**): after a failed staging read produced no operation, retry
  skipped staging and reached the catalogue refresh instead of the expected episode read.
  That strict request mismatch proves the missing restage path, not a destructive write.
  It now completes the live request once, archives the original error, and sends no repeat
  write or completion report on the third call. Post-implementation tests cover canceled
  and deleted rules retiring without success, released ordinary retention, and failed
  restage checkpoint persistence preventing dispatch. Review identified target-owner and
  binding gaps plus stale action labels; fixes and additional tests require a fresh matched
  original target, hold legacy records, respect another rule's uncertain target in either
  batch order, and report the current action on a new staging failure. These review tests
  were added after the fixes, not demonstrated failing beforehand. The full gate passed
  **638 in 6.218s**, with **22 frontend tests**. No process-kill/restart evidence is claimed.
  Restaging records now retain request ID and instance/series identity. Existing records
  lacking this evidence remain held for review. Operation-backed canceled requests are
  unchanged; remote URL/series replacement identity, settings transactions and ledger
  lifecycle remain open. Successful ordinary-run history policy is unchanged: this work
  does not archive and force-refresh every completed ordinary run.
- `test_mixed_recovery.py` originally added eight tests for the bounded mixed-retry ledger. The first
  regression was observed failing before implementation in all four subcases: unavailable
  removal, cancellation, a new ordinary exclusion and an unrelated requested subset. All
  aborted in legacy recovery. After the change, the focused baseline plus regression
  passed **24 tests in 0.085s**. Unrelated ordinary work now uses fresh settings/readings,
  while blocked removal errors remain visible and excluded content is not deleted.
  Additional post-implementation tests cover interrupted handoff, reconciliation and
  before/after-write ledger saves; interrupted replacement-intent save; repeated calls;
  orphan acknowledged operations; missing/corrupt ledgers; changed acknowledged targets;
  replacement-rule target holds; and overlapping ownership rejection. Four checkpoint
  injection subcases show no subsequent write after save failure, one external mutation
  across recovery, and no duplicate completion report. These are exception injection and
  consecutive public calls, not process-kill/restart evidence. Exact old intents remain
  archived. Existing removal-only tests were not relaxed.
  The first full gate found only the static name scanner's closure limitation; explicit
  `nonlocal` declarations fixed that scan, and the full gate above then passed. A Windows
  run of the names module passed the scan but failed two import-dependent checks on
  unavailable `fcntl`; it is not a substitute gate. No dependencies were installed.
  Limits: removal-only recovery remains legacy; initial staging failures without operations
  stay held; canceled requests need a resolution policy; strict ledger validation is not a
  complete schema/migration or transactional durability guarantee. Instance URL changes,
  process crashes, shared-file protection and all-entry-point mode guards remain open.
- `_finish_removals` no longer saves the run's stale settings document. It reads current
  settings and removes a rule only when the current queue request ID, action, instance and
  series all match a completed removal operation. A public-run regression with an injected
  settings edit between the Sonarr acknowledgement and finalization first failed in all
  four subcases: an unrelated keep-days edit was reverted, and canceled, requeued and
  retargeted queues still had their rule removed. After the fix, all four pass: the newer
  document survives, a canceled or requeued request keeps its rule, and a retargeted rule
  is not deleted. Focused suites passed **23 tests in 0.069s**; the full gate above
  includes the regression. This is read-merge-write, not a transaction lock: a save racing
  the run can still be overwritten, and remote target identity is not re-verified here.
- Mixed-run recovery now distinguishes completed/finalized removal history from unresolved
  one-time work. A public-run regression completed queued unmonitoring for one series,
  lost the ordinary deletion reply for a second, then added an exclusion. Before the fix,
  the regression failed (**1 in 0.031s**) recording an attempted
  `DELETE /api/v3/episodefile/199` despite that exclusion. After the fix it passes with no
  further mutation, a new run ID, zero deleted/bytes, and an exact archived old intent.
  The related focused suites passed **21 tests in 0.056s**. A second test injects an
  interruption at local removal finalization after an acknowledged write: the next public
  call finalizes without repeating the external action. That test was added after the
  initial patch and passes in the full gate; it is not process-crash evidence. Completed
  requests still present in removal-only intents stay on the legacy finalization path to
  avoid freshly staging them again. The newer mixed-retry checkpoint above supplies a
  separate ledger for mixed cases. No claim of complete recovery, target authorization
  or concurrent safety.
- Queued removals now carry a persisted `request_id` (validated, 64 chars, generated when
  absent). `action_settings` refuses a submitted ID that does not match the current queue
  request or action, so a stale document cannot restore canceled work; a new queue entry
  omits the ID and receives a fresh one. `_operation` stamps removal operations with the
  queue's request ID; `_validate_removal_request` requires the current queue to hold the
  same ID and action at recovery and again immediately before dispatch. Legacy operations
  without an ID are refused. Three regressions were observed failing before the patch:
  missing ID, same-action cancel/requeue resuming the old operation, and stale save
  restoring a canceled ID. Those three then passed in **0.006s**. Two further tests cover
  legacy operations without identity and cancel-after-staging dispatch; they were added
  after the patch, not demonstrated failing beforehand. All five passed in the full gate.
  A repeat gate on committed tree `a07587b` passed **618 Python tests in 4.985s**, all
  **22 frontend tests**, and all required import/syntax checks. This is request identity, not a
  transaction lock: concurrent save/write races, uncertain deletion reconciliation,
  mixed-run separation and remote target identity remain open.
- Ordinary-only unfinished runs now archive exact operation checkpoints before staging a
  new intent, force a current series catalogue read and re-evaluate current settings. The
  first regression failed in `_resume_intent`: recovery requested episodes where the fresh
  catalogue request was expected. The unchanged baseline run/recovery/queue suites passed
  **27 tests in 0.040s**. After the patch, the initial regression passed **1 in 0.031s**;
  expanded coverage passed in the full gate above. Five subcases cover widened keep window,
  new manual exclusion, disabled rule, different requested subset and a file now absent.
  They assert no additional mutation, a different run ID, zero new deletion/byte counts
  and an exact archived copy of the prior failed intent. Archive-failure injection preserves
  the old intent and sends no mutation. Tests use consecutive calls, not process restart.
  Existing explicit-removal tests are unchanged. Mixed/removal recovery is NOT fixed by
  this checkpoint; queue identity, uncertain deletion reconciliation, unavailable/malformed
  fresh rows, archive retention, crash finalization and concurrent durability remain open.
  The original strict-request failure proves entry into the old recovery path, not an
  observed second deletion: the fixture deliberately stopped before that write.
  A subsequent read-order-independent outcome regression was run unchanged against both
  patched and HEAD (`d2acdc7`) worker sources in disposable Linux staging. Patched: **1 test
  passed in 0.029s**, no mutation. Baseline: **1 failed in 0.028s**, recording an attempted
  `DELETE /api/v3/episodefile/99` despite the new exclusion. The final full gate above
  includes this regression. No original removal-test expectations were changed.
- Legacy run execution is retired: the unused `apply_removals` helper was removed after
  caller checks (including Pylance references); `process_rule` now refuses `dry_run=False`
  before any Sonarr call. Its deletion/monitoring helpers only describe candidates.
  The refusal regression first failed in both Test Mode subcases. A source-text test
  coupled to the removed branch then failed with `IndexError`; it was replaced by a
  real `main.run()` test with exact scripted requests and persisted operation assertions.
  Successful unmonitoring precedes deletion; failed unmonitoring leaves deletion pending
  with zero attempts. Both focused tests passed (**2 in 0.012s**), then the full gate passed.
  Immediate RPC writes, shared-file safety, and recovery authorization remain open.
- The original dispatch regression failed before the change (zero calls). Its ordinary
  Linux-import replacement passed alone: **1 test in 0.000s**, verbose output `ok`.
- All **8 focused executor tests passed in 0.002s**: dispatch, before/after checkpoint
  failure, failed Sonarr write, local rule removal, unknown operation, read-only recovery,
  and empty recovery. They replace the AST-only diagnostic.
- `tests/test_run_contract.py` verifies `main.run()` for both scheduled flag values and
  both Test Mode values using the real client mapping and persistence. Queued unmonitoring
  sends exactly one PUT when enabled; Test Mode sends none and creates no state directory.
  This does not exercise the scheduler loop, CLI, or every removal disposition.
- `4ca3483` fixes run-result reporting: a failed first write leaves the next operation
  pending, preserves both queued rules, and returns `incomplete` with errors and neither
  removal reported successful. The new regression first failed on an empty `errors` list.
  Actual-versus-planned byte accounting remains open.
- `90cfcd8` adds persisted-intent retry coverage. A second `main.run()` in the same process
  re-reads both unfinished series before sending either write; it retries the failed write
  and executes the pending one, recording attempts `[2, 1]` and completion. All **7 focused
  public-run/transport tests passed in 0.022s**. This is not a process-restart or crash test,
  nor evidence of recovery permission revalidation; settings remain unchanged. The initial
  expected request order was corrected to match read-only reconciliation before dispatch.
- Recovery now rejects unfinished removals whose current queue action is missing or changed,
  before reconciliation alters the saved intent. The canceled-queue public-run regression
  first failed at an unexpected recovery GET; after the guard, **1 test passed in 0.013s**,
  with no further request, unchanged intent and the rule retained. This does not prove
  cancel-and-requeue identity, current target/exclusion/scope checks or mid-run cancellation.
  Rejection preserves the unresolved intent; an operator recovery workflow remains open.
- `tests/test_recovery_errors.py`: **4 tests passed in 0.023s**. A connection error saying
  `host not found` first reproduced false completion (the expected exception was not raised).
  `SonarrError` now preserves HTTP status; series recovery recognizes only HTTP 404 as
  absent and propagates other failures. Tests cover 401/403/404/500/503 via the real client.
  A public retry with an unavailable or malformed episode-list response sends no further
  mutation and leaves its persisted intent and queued rule intact. Malformed individual
  rows, target identity, proxy-generated 404s and actual process restart remain unproven.
- `tests/fake_sonarr.py` records exact method/path/query/body, rejects unexpected requests,
  blocks socket access, and supports changed/shared/fileless payloads and lost responses.
  Its first run exposed an invalid synthetic key; that fixture error was corrected before
  the passing gate. Older tests have not all been migrated to this isolation boundary.
- The gate now requires Node, continues independent checks after test failure, and gives
  PowerShell staging scripts unique paths with cleanup on success/failure. Concurrent
  and interrupted-SSH cleanup fault injection has not yet been exercised.

### Mutation coverage still required

| Surface | Current evidence |
|---|---|
| Public run, manual/scheduled flag | Queued unmonitor-all with Test Mode on/off; exact requests |
| Public retention run | One ordinary file: unmonitor precedes DELETE; failed unmonitor blocks DELETE; exact requests and checkpoints |
| Public run failure and retry | Failed-first-write reporting; second call reuses persisted intent with unchanged settings; no process restart |
| Executor/recovery helper | Mocked checkpoint/dispatch failures and read-only reconciliation |
| Scheduler loop, CLI, picker, scope pass, recycle-bin, other removals, process restart, restore | Full mode/permission/failure matrix remains open |

## Historical review — 2026-09-17, before the executor fix

**The gate failed at this point.** A cleanup-time rerun via `tools/check-on-host.ps1`
on `fatzserver-host` ran 591 tests in 5.877s: 590 passed and the executor regression below
failed (exit 1). The script stopped before its separate worker-import, module-syntax and
frontend checks. Source was staged under `/tmp`; no deployment or live-service checks
were performed. The earlier successful gate predates `tests/test_run_executor_review.py`.
The review recorded these failures:

| Evidence | Recorded result |
|---|---|
| Executor regression, `tests/test_run_executor_review.py` | Expected `set_monitored([101], False)` once; actual call count was 0. The staged operation was not dispatched. |
| Isolated retention-editor check | Saving retention edits lost manual exclusion fields. |
| Isolated shared-file check | File ID `99` appeared in both protected and delete lists. |
| Isolated AniList check | An existing Sonarr air date was overwritten rather than left intact. |

The executor diagnostic extracts the actual function via AST; it is not a full public
run/recovery test. The other three results are isolated reproductions, not live-service
acceptance. Other review findings in [PLAN.md](PLAN.md) are source-inspected unless
separately evidenced; concurrency, DST, container permissions and browser timing still
need targeted validation. PLAN owns the fixes and release gates.

The cleanup-time gate reconfirmed the executor failure only; the other three recorded
reproductions were not independently rerun. No deployment or live-service checks occurred.

## Historical automated gate — 2026-09-17, before the executor regression

Recorded from Windows via `tools\check-on-host.ps1` against `fatzserver-host` for the
grouped implementation pass (notification removal, connections/API keys, provider dates,
backup/restore, Status, library rows, staged runs and series automation).
The gate staged source under `/tmp` and removed it afterwards, installed nothing,
touched no `/boot` path, read no media and contacted no Sonarr.

| Check | Recorded result |
|---|---|
| `python3 -m unittest discover -s tests` | 590 tests, all pass |
| Worker imports | every module loads, `server.py` included |
| `node --input-type=module --check` over every `src/assets/*.js` | no syntax errors across all 19 shipped ES modules |
| `node --test tests/frontend/*.test.js` | 22 tests, all pass |

These are the claims recorded for that run, including import and syntax coverage, not
an independent audit of the gate or evidence that the later regression passes. Static
interface checks and isolated backend/frontend tests did not establish executor safety,
live provider correctness, concurrent persistence or real backup/restore behavior.

## Build 20 acceptance smoke — read-only, 2026-09-17

At the time of this check, `tv-retention-demo` on `fatzserver-host` was the only running
TV Retention container, healthy and serving `tv-retention:dev-build20`. This is historical
smoke evidence, not a claim about the deployment now. The operator did not turn Test Mode
off, invoke a run or invoke any write action; the scheduler catch-up was observed below.

| Check | Recorded result |
|---|---|
| `GET /health` | HTTP 200, `{"ok": true}` |
| `GET /` without a session | HTTP 303 to `/login` |
| Login flow | Wrong password HTTP 401; configured credentials land on the page |
| Authenticated snapshot | version `0.3.0`, build `20`, Test Mode `true` |
| Test Mode | local-time catch-up logged `6 planned across 32 rule(s); nothing changed`; no deletion was attempted |
| Schedule clock | container was `America/New_York` (EDT at the time); schedule fields used that local civil time. This does not prove DST-transition correctness. |
| Navigation | Series, Settings, System and Help views present; Schedule and Backup in their new locations |
| Removed/replaced UI | Exclusion Rules present; no automatic-search control; Alerts and Safety were not standalone tabs |
| Container logs | healthy startup line only; no traceback, permission error or HTTP 500 |

This smoke does not establish write safety or a general Test Mode barrier. Immediate
monitoring and recycle-bin paths have unresolved Test Mode findings in PLAN; browsing
is not equivalent to saving rules or changing monitored flags. Further acceptance follows
[ACCEPTANCE.md](ACCEPTANCE.md) and PLAN's prerequisite gates, not this historical smoke.

## Earlier evidence — historical baselines, not current acceptance

- **2026-09-16, Build 10 disposable image:** missing credentials caused clean startup
  refusal (exit 1); `/config` acquired default uid/gid 1000 ownership without operator
  `chown`. The smoke container was removed without touching the demo container. These
  narrow observations do not validate all privilege modes or restore permissions.
- **2026-09-07, read-only performance:** 3,022 series and 36 rules; source staged under
  `/tmp`, `TVR_CONFIG` away from `/boot`, every Sonarr call asserted GET. Reading the 36
  bound series took 72 calls, 1.3s and 14.8 MiB. Cached rechecks, including after keep-window
  edits, took 0 calls and 0.03s; recomputing all plans on a heartbeat took 0 calls and 0.07s.
  This is a historical cache baseline, not a benchmark of the current application.
- **2026-09-11, browser module graph:** `tv-retention:phase4` on port 18788 used a copy of
  demo settings with the schedule forced off and Test Mode on; the original config was
  not opened for writing and the container was removed afterwards. Modules and static
  imports stayed under `/assets/ad57b2472405/…`, with HTTP 200 and `immutable` caching.
  An unknown digest returned 404 with `Cache-Control: no-store`; non-allowlisted paths
  also returned 404. There were no console/page errors, traceback or HTTP 500. Opening a
  series rendered without the global busy overlay; the plan-specific Run confirmation
  named Test Mode, and cancelling sent nothing. This read-only session covered the graph
  then shipped, not all current modules or delayed-response workflows.

## Evidence still missing

No recorded live acceptance establishes deletion or monitoring-write correctness, real
optional-provider behavior (including TMDB/TVMaze/AniList date filling), API-key lifecycle,
or backup/restore. Isolated contract tests and the historical gate do not replace those
checks. The gate used no live provider keys or provider network calls.

Concurrency, recovery, Test Mode mutation coverage, DST transitions, dropped-privilege
restore and current real-browser workflows require the targeted evidence in PLAN. Keep
schedules off and Test Mode on; neither authorizes immediate write actions. Follow PLAN's
isolated acceptance ladder before any operator-approved live write. Removed outbound
notifications/webhooks are not pending tests; alerts remain in the app and System → Status.
