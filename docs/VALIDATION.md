# Validation record

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
An attempted interruption completed normally, so no SSH-loss or interrupted-cleanup claim
is made. Injected fsync/replace failure coverage and serialized transactions remain open
in Phase 2; safe restore remains intentionally disabled until Phase 3.

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
