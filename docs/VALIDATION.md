# Validation record

## Latest implementation — 2026-09-17

**The Linux gate passes; deployment remains blocked by PLAN's remaining P0 work.**
`tools/check-on-host.ps1` on `fatzserver-host` ran **621 Python tests in 5.020s**, all
passing, plus **22 frontend tests**, all passing. The preceding mixed-replan checkpoint
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
  requests still present in the queue stay on the legacy finalization path to avoid
  freshly staging them again. Unresolved removals, missing completion records, and local
  finalization cases still need separate ledgers; they may block or replay ordinary work.
  No claim of complete mixed-run recovery, target authorization or concurrent safety.
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
