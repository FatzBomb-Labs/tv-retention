# Production-readiness plan

Plan of record, revised 2026-09-17 after the application review. This document specifies
work; it does not authorize changes to a live library or a deployment. Remove completed
items as they land, recording their evidence in [VALIDATION.md](VALIDATION.md). Keep the
frontend architecture contracts in [REFACTOR-HANDOFF.md](REFACTOR-HANDOFF.md); this is not
another module-extraction project.

## Objective and release policy

Ship a focused Sonarr retention application whose safety, recovery, persistence and UI
promises are demonstrated by behavior tests and isolated container acceptance. Freeze
new integrations and optional features until the release blockers below are resolved.
Do not promote to 1.0 merely because the feature checklist is complete.

Priority meanings:

- **P0:** blocks enabling destructive operation or releasing the corrected executor.
- **P1:** blocks production release: durability, operational correctness, supported
  workflows, defensive hardening and usable access to primary controls.
- **P2:** measured improvements or optional product polish; may be explicitly deferred
  if they do not conceal incorrect state or weaken safety.

Evidence baseline and latest implementation results are in [VALIDATION.md](VALIDATION.md).
The executor dispatch regression is fixed, including explicit intent and read-only recovery
reconciliation. The current passing gate does not establish the remaining safety contracts.
Review checks confirmed exclusion-field loss, a shared file appearing in protected and
delete lists, and AniList overwriting a Sonarr date; those remain open. Other findings are
source-inspected; concurrency, DST, container permissions and browser timing consequences
need targeted validation. Test counts are evidence, not release criteria.

### Progress baseline

**OVERALL PLAN (0/94 complete)** uses the 94 unchecked implementation items present at
this recovery-policy checkpoint as a fixed remaining-work baseline. Earlier completed
work is recorded in VALIDATION and is not reconstructed into this denominator. Increment
only when an entire baseline item is retired with evidence; partial fixes do not count.
If scope changes, record the denominator change explicitly. This is checklist progress,
not a production-readiness percentage.

### Non-negotiable boundaries

- Sonarr owns media and deletion. No media mounts, path mappings or direct media access.
- Exactly one confirmed series identity per rule; ambiguous or unavailable identity fails
  closed, including queued removals and resumed work.
- Exclusions protect both monitoring state and every file containing excluded content.
- No deletion proceeds without confirmed unmonitoring of all affected episodes.
- Ordinary retention runs never decide to monitor. Explicit queued removal dispositions
  that monitor remain separately identified operator intent, not retention policy.
- Test Mode prevents external Sonarr mutations on every entry point. Application settings
  must remain editable; document the precise allowed local operational writes rather than
  weakening the external-write guarantee. Preview must not create executable intent.
- Removal remains queued and undoable until execution starts. A canceled or newly
  protected operation must not return through a stale draft or recovered intent.
- Cached, stale and partial plans never masquerade as complete current plans.
- Per-show reads remain background work; transaction safety must not become a global
  network-length lock or a global busy overlay.
- Standard-library runtime remains the default. No framework/database rewrite is required
  by this plan; any added dependency needs a specific benefit and maintenance rationale.

## Delivery sequence and dependencies

1. **Phase 0:** reproducible baseline and regression harness.
2. **Phase 1:** P0 execution, protection and mutation-policy fixes.
3. **Phase 2:** concurrent persistence and durable recovery foundations.
4. **Phase 3:** safe backup/restore, dependent on Phase 2 transaction controls.
5. **Phases 4–6:** provider/scheduler correctness, UI state and container/HTTP operation.
6. **Phase 7:** release evidence, disposable acceptance and controlled deployment.

Small fixes may be developed in parallel, but the executor must not be deployed alone:
its corrected write path must ship only with the P0 guards and recovery/persistence tests.
Frontend revision handling depends on backend revision semantics. Cosmetic work does not
hold up safety work, and no production-media test substitutes for an isolated regression.

## Phase 0 — Establish the test boundary (P0)

Primary areas: `tests/`, `tools/check-on-host.*`, `docs/VALIDATION.md`.

The normal-import executor tests, public-run coverage, scripted Sonarr fixture and
mandatory checks are in place; evidence and current matrix limits are in VALIDATION.

- [ ] Add a mutation matrix covering manual run, scheduled run, CLI, monitoring picker,
      scope pass, recycle-bin changes, removal, restart and restore, with Test Mode both
      on and off. Assert exact permitted requests, not only result counts.
- [ ] Use fresh temporary config/state for every backend test and isolated schedules.
      Continue the Linux host gate; do not patch Linux path or `fcntl` behavior to make
      the Windows box impersonate the container.
- [ ] Fault-test concurrent and interrupted host validations: script/source paths are
      now unique and cleanup is scoped, but SSH loss and cleanup failures need evidence.

**Exit gate:** the regression harness detects the known failures without external service
access, and the required validation matrix is documented and repeatable.

## Phase 1 — Restore the safety contract (P0)

### 1.1 Execute and recover operations correctly

Primary areas: `src/worker/main.py`, `actions.py`, `sonarr.py`, executor tests.

Dispatch/completion now belongs to the executor, with explicit intent and read-only
recovery reconciliation. Failed operations stop the current run conservatively. Run
summaries now expose incomplete operations and failed/unattempted removals. Persisted-intent
retry is covered through a second public run with unchanged settings, not a process restart
or recovery-authorization test; evidence is in VALIDATION. Recovery now refuses unfinished
removals when the current queue action is missing or changed, preserving the intent for
review. Same-action cancel-and-requeue, mid-run cancellation and the wider permission
checks below remain open; no operator recovery-resolution workflow exists yet. Recovery
uses structured HTTP status for absent series and stops on failed reads rather than
replaying writes after an unavailable or malformed episode-list response.

**Agreed unattended recovery policy:** keep confirmed completed work, preserve failures
and uncertainty, and calculate ordinary retention anew from current settings and fresh
Sonarr readings at the next manual/scheduled run. Never replay unfinished ordinary
operations or require routine operator acknowledgement after an outage. Missing files
are not newly reclaimed space. No rollback of completed retention is attempted.
Explicit queued removals remain separate one-time intent: persist request identity, verify
current target and queue generation, reconcile uncertain results, and never revive a
cancellation or transfer old confirmation to a replacement series.

Ordinary-only unfinished intents now follow that policy: exact checkpoints are archived
under `state/run-history/` before replacement, and retries refresh the catalogue and
re-evaluate current rules. Archive failure stops replacement/writes. VALIDATION records
the bounded coverage. Mixed/removal intents still use the legacy recovery path above;
they can still block ordinary work and are not evidence of the full accepted policy.
Archive lifecycle, crash-history finalization and durability remain Phase 2 work.

- [ ] Restrict execution to currently eligible operations. Separate mixed-run removal
      recovery from newly planned ordinary retention so failed/canceled explicit work
      cannot globally block unattended retention or revive its old decisions.
- [ ] Define explicit operation states and dependencies. Failed or uncertain unmonitoring
      prevents the dependent file deletion. Independent series may continue only under a
      documented policy that preserves the failure in the overall result.
- [ ] Prove durable before/after checkpoints through public-run interruption tests; if a
      required checkpoint cannot be saved, do not send the next write. Helper-level failure
      propagation is covered, but process crashes and multi-operation durability remain open.
- [ ] Report actual rather than planned byte totals, including partial success and retry,
      without counting failed, unattempted or already-completed work as newly reclaimed space.
- [ ] Before explicit removal retries, verify current instance/series/file identity and
      queue generation. Changed instance URLs, same-action cancel/requeue and a different
      requested run scope must not silently revive old work. Freeze original decisions for
      audit. Ordinary retention always re-plans; special operator resolution is reserved
      for unprovable one-time removal permission, not routine retention failures.
- [ ] Prove missing/replaced-file recovery uses complete, authoritative target readings,
      including malformed individual rows and changed file membership. HTTP status is now
      structured, but a proxy-generated 404 must not substitute for verified target identity.

**Tests:** first run; multiple operations; no operations; unmonitor failure; delete failure;
request accepted but acknowledgement lost; crash before/after each checkpoint; interrupted
removal; changed file ID; unavailable Sonarr; changed rule/instance; canceled work; subset
run with an unrelated old intent; repeated restart. No unsafe replay, false completion,
empty/global intent corruption or duplicate success reporting is acceptable.

### 1.2 Preserve exclusions and removal intent

Primary areas: `series-editor.js`, `series-removal.js`, `actions.py`, `core.py`.

- [ ] Merge retention edits into the latest authoritative rule by ID, preserving manual
      exclusions and independently owned metadata. Do not infer the saved rule from its
      position at the end of an array.
- [ ] Remove queue ownership from retention drafts. Read the current queue on render/save;
      Undo must remain undone after editing, switching shows, background refresh and save.
- [ ] Add backend revision/conflict protection in Phase 2 so multiple tabs cannot restore
      removed protection or canceled queues with a stale whole-document save.

**Tests:** exclusion -> edit -> save -> reload; whole-season exclusions including future
members; Undo -> edit -> save; rule removed during an in-flight operation; reordered rule
list; existing auto-reenable metadata preserved.

### 1.3 Consolidate episode decisions into safe file operations

Primary areas: `core.py`, `sonarr.py`, `main.py`, mapping and retention tests.

- [ ] Keep condition votes episode-specific rather than sharing votes by path.
- [ ] Group physical file actions by `(instance_id, file_id)` after episode decisions.
      Require complete membership and permission from every constituent episode; any keep,
      exclusion, unknown or inconsistent mapping protects the entire file.
- [ ] Unmonitor all affected episodes before deleting their shared file. Deduplicate writes,
      sizes and success reporting while retaining per-episode explanations in the UI.

**Tests:** shared excluded/deletable episodes; mixed keep/delete under Any and All; unknown
or future sibling; cross-season file; duplicate mappings; missing IDs; equal IDs across
instances. No file can appear in both protected and executable delete sets.

### 1.4 Apply Test Mode consistently

Primary areas: `actions.py`, `main.py`, `sonarr.py`, `settings.js`, `series-editor.js`.

- [ ] Enforce one external-mutation policy across every write entry point, not just `run()`.
      A UI-disabled button is not the enforcement boundary.
- [ ] Keep configuration saves usable while skipping or refusing external monitoring and
      recycle-bin changes with explicit wording. Do not claim a skipped pass was applied.
- [ ] Define a safe in-progress Test Mode transition: an acknowledged toggle must prevent
      subsequent writes, while accurately reporting a request already in flight.
- [ ] Resume, manual run, schedule and restore must all obey the current mode. Preview must
      neither persist executable work nor activate queued work on a later run.

**Exit gate for Phase 1:** all P0 regression paths pass through the real action/run surface
against fake Sonarr, including zero external mutations under Test Mode. Do not deploy yet
without Phase 2's checkpoint and concurrency guarantees.

## Phase 2 — Make state durable under concurrency (P1, prerequisite for deployment)

Primary areas: `core.atomic_json`, `store.py`, `actions.py`, `main.py`, frontend transport.

- [ ] Allocate a unique temporary file per write, retain same-filesystem atomic replacement,
      file/directory fsync and cleanup. Verify errors do not silently masquerade as success.
- [ ] Introduce short serialized read/validate/merge/write transactions. Define lock order
      and cover both HTTP/worker threads and supported CLI processes. Keep Sonarr reads
      outside transaction locks and re-check the state version before committing results.
- [ ] Add a monotonic settings revision and reject stale whole-document submissions with a
      useful conflict response. Preserve masked-secret behavior and migrated settings.
- [ ] Merge background updates by owned fields/rule ID, preserving newer settings, other
      shows' results, acknowledgements and suppression state. Never let cache refresh
      overwrite user-owned fields.
- [ ] Distinguish authoritative state from disposable caches. Fail closed on damaged
      settings or intent; do not silently reset an unsafe configuration or replay unknown
      work. Expose storage failures in Status.
- [ ] Make run identity/history finalization idempotent across restart so one logical run
      is not counted as multiple successful runs or duplicated reclaimed space.

**Tests:** simultaneous writers/readers; two tabs; slow binding plus settings save;
concurrent show checks; CLI plus HTTP; journal/finalization restart; disk-full, permission,
replace/fsync failures; malformed and interrupted intent. Use deterministic barriers rather
than timing sleeps. Readers see complete documents and newer user intent is never lost.

## Phase 3 — Backups that can actually restore safely (P1)

Primary areas: `backup.py`, `store.py`, `actions.py`, `settings.js`, Compose and docs.

- [ ] Define and document a separate optional persistent `/backups` mount; `/config` remains
      the only required volume and no media mounts are added. Make ownership under PUID,
      PGID and explicit `user:` predictable without unbounded ownership changes.
- [ ] Stage restore inside an application-writable location. Test actual dropped-privilege
      container permissions rather than relying on host unit-test permissions.
- [ ] Create a consistent backup snapshot under transaction coordination. Define included
      authoritative files, caches, runtime locks and temporary-file exclusions. Either
      constrain active state to `/config` or explicitly support backing up external state;
      never silently omit it.
- [ ] Reserve archive names atomically and order retention by reliable timestamp/sequence
      metadata, not lexicographic filenames. Never prune the archive just created. Restrict
      cleanup to owned archives and test concurrent/same-second creation.
- [ ] Keep path/member checks, and add bounded entry count, expanded bytes and available-space
      checks. Treat archives as credential-bearing data; avoid secret output and use
      appropriate permissions. A manifest is structural validation, not authenticity.
- [ ] Validate format, settings, migrations and file set before activation. Reject unsupported
      future formats without modifying live files.
- [ ] Restore under maintenance exclusion with recoverable installation/rollback semantics.
      Define replacement behavior for absent files, preserve recovery data on failure and
      handle interruption during activation—not only during extraction.
- [ ] Quarantine restored/present executable intents and queues until explicitly reviewed;
      do not replay historical work. Start restored settings with Test Mode on and schedules
      off until the operator re-arms them.
- [ ] On restore success, stop frontend polling, discard drafts and reload a fresh snapshot
      before permitting any action. On failure, report the actual state and recovery path.

**Tests:** fresh container restore as non-root; older archive missing newer files; pending
intent; enabled schedule in archive; concurrent save/run; partial copy/activation failure;
restart during restore; corrupt/incompatible archive; same-second backups with keep=1;
backup persistence after container replacement. Verify secrets remain usable without being
printed. No restore may contact Sonarr or automatically enable a write.

## Phase 4 — Dates and scheduling with defensible semantics (P1)

### 4.1 Provider identity, precedence and estimates

Primary areas: `anilist.py`, `tvmaze.py`, `tmdb.py`, `sonarr.py`, `main.py`, `core.py`.

- [ ] Fix AniList to fill blanks only. Require unambiguous series/season/episode mapping;
      title similarity alone must not authorize retention dates. If that mapping cannot be
      made reliable for 1.0, disable its retention contribution and label the limitation.
- [ ] Preserve provider precedence and authoritative Sonarr dates. Reject ambiguous TMDB
      external-ID results instead of taking the first. Confirm cross-provider numbering
      with Sonarr-shaped fixtures, including anime and specials.
- [ ] Preserve numeric season zero in TVMaze lookups.
- [ ] Confirm and fix trailing-date interpolation that can turn unknown forthcoming
      episodes into historically aired episodes. An undated future season must not consume
      past retention slots or lose monitoring because of a neighboring date.
- [ ] Define estimates and provenance explicitly. Verify that acquired-date fallback uses
      actual acquisition/import events rather than any earliest history event; do not
      confuse a grab, upgrade or deletion with first acquisition.
- [ ] Test provider failure, cache persistence/expiry, partial data and unresolved handling.
      Do not hide a provider mismatch as successful enrichment. Preserve the choice to
      exclude/block unresolved files rather than inventing dates.
- [ ] Remove Plex/Jellyfin from active date-provider choices until they supply usable dates;
      if connection checks remain, label them connectivity-only.

**Gate:** higher-priority dates never change; uncertain identity or dates never produce
new deletion permission. Tests verify downstream deletion and monitoring, not just fill
counts. Correct the P0-confirmed AniList overwrite before any release that enables it.

### 4.2 Civil-time scheduling

Primary areas: `schedules.py`, `core.py`, `main.py`, schedule tests/UI.

- [ ] Parse and validate every custom-cron field independently of whether a sample instant
      happens to match an earlier field.
- [ ] Use timezone-aware civil-time calculations with an IANA timezone, not the current
      fixed offset applied retrospectively. Confirm timezone data exists in the image.
- [ ] Specify spring-forward behavior and one execution per intended fall-back occurrence;
      keep a durable occurrence identity so repeated wall-clock times do not double-run.
- [ ] Replace the fixed 40-day catch-up assumption for supported sparse custom schedules,
      or explicitly bound/reject unsupported schedules with visible migration guidance.
- [ ] Preserve catch-up and Sonarr-unreachable retry semantics across restart without
      replaying completed runs. Cover clock changes and long downtime.
- [ ] Offer Off/Daily/Weekly as the simple UI, but preserve and clearly display existing
      advanced schedules until an explicit conversion; never silently rewrite them.

**Tests:** real DST transitions, leap years/month ends, annual custom schedule after long
downtime, invalid later fields, timezone change, repeated tick, interrupted catch-up and
unavailable Sonarr. Freeze time in fixtures; no real waiting.

## Phase 5 — Make the interface reflect authoritative state (P1 unless marked P2)

Primary areas: `app.js`, `settings.js`, `series-editor.js`, `series-removal.js`, `topbar.js`,
`connections.js`, `checks.js`, `library.js`, `activity.js`, `dom.js`, markup and CSS.

### State, freshness and write feedback

- [ ] Couple plans to the settings revision and source reading metadata. Invalidate on
      retention, exclusions, queue, instance and mode changes; never suppress Run with a
      stale empty plan.
- [ ] Make confirmation show complete/current versus partial/stale/unknown coverage. Never
      describe uncertain zero counts as 'no changes expected'. Specify whether confirmation
      is advisory or authorizes a bounded plan; if fresh execution materially changes the
      proposed destructive scope, require renewed confirmation rather than silently growing it.
- [ ] Use one eligibility definition for Run that includes queued removals on disabled
      rules; keep ordinary disabled-rule retention inactive.
- [ ] Serialize UI saves, track draft/request generations, and handle backend revision
      conflicts. An earlier response cannot clear newer edits or overwrite a later save.
- [ ] Guard asynchronous editor scope/tree/count responses with request generations; ignore
      responses for old form values, a closed pane or a different series.
- [ ] Keep backup drafts and loaded archive state through background renders. Distinguish
      not loaded, loading, failed and truly empty. Apply the same dirty-state discipline
      to backup settings as other forms.
- [ ] Bind connection verification to the exact tested URL/key/TLS values. Editing them
      invalidates verification; stale test responses cannot stamp newer values verified.
- [ ] Preserve a newly revealed API key until deliberate dismissal/copy acknowledgement;
      background renders must not erase its one-time display. Do not persist the full key
      in browser storage.
- [ ] Remove global library replacement during bulk checks. Update only affected shows,
      with no global overlay or unrelated-show blocking.
- [ ] Serialize log polling and invalidate late responses after stopping/changing views.
      Preserve ordered byte offsets without duplicate/reordered lines.
- [ ] Show omitted counts or pagination for change lists above the current 300-row cap;
      summaries must never appear to be a complete list when truncated.

### Accessibility and comprehensibility

- [ ] Give the primary series editor action a native keyboard-accessible control, with
      visible focus and no nested interactive controls. Test modal focus entry, containment,
      escape and restoration; preserve focus during background updates.
- [ ] Set ARIA attributes through actual attributes/appropriate DOM properties, not arbitrary
      hyphenated properties on elements. Verify expanded state in the accessibility tree.
- [ ] Correct Any/All help and remove obsolete Earliest/Latest descriptions. Use one set of
      policy definitions for editor labels, explanations and help.
- [ ] Fix and visually verify the light-theme menu token/fallback, text contrast, focus and
      disabled states. Exercise phone-width layout, zoom, long titles and reduced motion.
- [ ] Verify which automation-panel consolidation is already implemented; finish only the
      missing clarity around inheritance, per-series values, exclusions and the separately
      labeled immediate monitoring pass. Keep expandable exclusion trees.
- [ ] **P2:** add New preset from the editor without losing the draft; move secondary prose
      into concise accessible help where it improves comprehension.

### Scale and browser evidence

- [ ] Add real-browser workflow tests alongside fake-DOM tests. Exercise selectors/listeners,
      keyboard behavior, dialogs, dirty forms and delayed/out-of-order responses.
- [ ] Measure cold load, background refresh, filtering and editor interaction with fixtures
      representative of the previously measured ~3,000-series library. Establish explicit
      budgets on a recorded reference machine before calling performance acceptable.
- [ ] **P2 unless budgets fail:** index rules by bound identity and preserve keyed cards where
      measurement shows repeated scans/full DOM replacement are costly. Keep lazy posters
      and bounded rendering; do not replace the architecture merely to optimize it.

**Gate:** browser tests prove exclusion preservation, Undo persistence, current-plan gating,
conflict-safe saves and restore reload. No silent data loss, misleading destructive
confirmation, inaccessible primary action or unrelated-show blocking remains.

## Phase 6 — Harden the supported container and HTTP boundary (P1)

Primary areas: `server.py`, `Dockerfile`, `docker-compose.yml`, `tools/`, HTTP tests/docs.

- [ ] Compare credentials as consistently encoded bytes, supporting valid Unicode values
      through startup/login. Handle invalid comparison inputs as controlled errors.
- [ ] Validate body framing/size and RPC envelope/action types before dispatch, inside a
      controlled error boundary. Return structured errors and close unusable connections;
      do not let malformed input escape as an unhandled handler exception.
- [ ] Bound accepted-connection timeouts, concurrent work and aggregate login admission.
      Retain session expiry, CSRF, constant-time comparison, safe asset allowlisting and
      non-cacheable rejection of unknown release digests.
- [ ] Document/test the supported trusted-network and HTTPS reverse-proxy deployment modes.
      Make cookie Secure behavior consistent with configured transport; never trust arbitrary
      forwarding headers as authentication or TLS evidence. No public exposure by default
      recommendation, and no security assurance based only on having no dependencies.
- [ ] Distinguish HTTP liveness, worker heartbeat and operational readiness in health/Status.
      A dead scheduler thread must not look healthy; Sonarr downtime must not cause a
      restart loop. Verify health checks honor supported port configuration.
- [ ] Handle termination deliberately: stop accepting new work, stop scheduling, checkpoint
      in-flight work and exit within a documented grace period. Forced termination must
      still recover safely. Test signal delivery and restart in the actual image.
- [ ] Verify root-start/drop-privileges and explicit `user:` startup, PUID/PGID/UMASK behavior,
      non-root config/backup permissions and existing volume ownership. Honor UMASK on
      supported startup paths; never traverse arbitrary host ownership.
- [ ] Document a tested container-hardening profile where compatible (read-only application
      filesystem, writable required paths, no-new-privileges, minimal capabilities and
      resource limits). Verify privilege-drop needs before recommending capability removal.
- [ ] Establish base-image renewal with an explicit fresh-base build and image scan; floating
      tags alone do not force updates. Record the resulting base/image digests. Pinning is
      optional until there is automated digest renewal, not a substitute for patching.
- [ ] Make published-image versus local-build instructions accurate and verify version,
      build/date labels, startup refusal and upgrade behavior in the actual release image.

**Gate:** isolated image tests cover login, permissions, configurable port, worker failure,
TLS-proxy assumptions, graceful/forced shutdown and persistent restore. HTTP tests stay
local and defensive; no live-target exploitation or load testing is part of acceptance.

## Phase 7 — Evidence, acceptance and controlled release (P1)

### Documentation and CI

- [ ] Update README, AGENTS, SECURITY-REVIEW, acceptance and refactor contracts to match the
      implemented Test Mode, revisions, recovery, backup mounts and schedules. Remove stale
      PHP/cron/monitoring-mode comments and unsupported guarantees, not just append caveats.
- [ ] Reconcile references to deletion-percentage guards with actual behavior. If no such
      guard exists, make an explicit product decision: implement/test a documented bound,
      or remove the claim and obsolete acceptance step. Do not imply absent protection.
- [ ] Clarify 'no filesystem access' as no **media** filesystem access; the app does write
      config/state. Reconcile notification-removal prose and connected/watching terminology.
- [ ] Replace hard-coded test-count promises with current recorded runs and checks by area.
      Mark historical security/validation records as historical, not current assurance.
- [ ] Add automated Linux CI for behavioral Python/frontend suites, module checks and isolated
      container smoke, without requiring the private validation host. Keep host scripts as
      an operator entry point. Add real-browser workflows with dev-only tooling as needed.
- [ ] Record commit, image/base digest, VERSION/BUILD, test results, browser/environment,
      migration/restore evidence and remaining limitations in VALIDATION for each candidate.

### Acceptance ladder (do not skip levels)

- [ ] **A — Deterministic fixtures:** all P0/P1 behavioral regressions pass. No expected
      failures or skipped mandatory checks, including the executor regression.
- [ ] **B — Isolated container + fake Sonarr:** complete UI-to-HTTP-to-worker-to-Sonarr flow,
      concurrent saves, restart/checkpoint fault injection, backup/restore and migrations.
      Keep all config and service endpoints disposable; assert exact external writes.
- [ ] **C — Disposable real Sonarr/media:** dedicated instance and synthetic expendable files,
      including a multi-episode file. Explicitly authorize Test Mode off only in this
      isolated environment; verify actual file outcomes, monitoring, recycle-bin behavior,
      partial failures and restart without risking a user's library.
- [ ] **D — Target read-only smoke:** stage under `/tmp`, use a copy of settings away from
      `/boot`, force schedules off and Test Mode on. Verify upgrade/UI/freshness/permissions
      and read-only Sonarr behavior. Do not infer write correctness from this level.
- [ ] **E — Operator-approved canary:** only after A–D, create rollback image/config archive,
      approve one small plan on a deliberately selected series and disable unrelated work.
      Explicitly turn Test Mode off, verify outcomes, then return to the safe state while
      reviewing evidence. Automatic general scheduling requires a separate operator choice.

### Release and rollback gate

- [ ] No unresolved P0/P1 findings. Suspected findings are either verified/fixed with a
      regression or closed with recorded counter-evidence; no silent omission.
- [ ] Run a documented soak over repeated schedule, refresh, save and restart cycles; verify
      bounded resources, no lost edits, no duplicate writes and truthful reporting. Choose
      cycle counts/duration before running and record them, rather than declaring a single
      healthy HTTP response a soak test.
- [ ] Exercise upgrade from supported settings versions and rollback using the matching
      prior config archive; old binaries must not consume incompatible upgraded state.
- [ ] Keep exactly one running application container and no stopped predecessors after a
      successful replacement. Retain one rollback image tag plus matching config archive;
      clean only TV Retention artifacts and remove preflight containers on success/failure.
- [ ] Increment BUILD for deployed code changes. Promote VERSION to 1.0.0 only after the
      release gates pass; a plan or documentation-only edit does not justify a build bump.
- [ ] Document recovery actions and remaining supported limits so operators can distinguish
      offline Sonarr, storage failure, incomplete work and intentionally blocked operations.

## Completion rule for every implementation item

1. Confirm the path and add the smallest behavior test that fails for the right reason.
2. Fix the responsible layer; avoid broad refactors mixed with safety changes.
3. Run focused checks, then the required Linux gate and relevant browser/container checks.
4. Review the safety and migration impact, including concurrent and restart behavior.
5. Record evidence in VALIDATION and remove completed work here. Keep deployed changes
   behind the release gates, even if an individual development commit passes its tests.

## Explicitly deferred or out of scope

- Radarr/movie management, watched-state retention and new provider integrations.
- External notifications/webhooks and public API operations beyond the existing lifecycle
  until their contracts have a separate product decision.
- Direct media deletion, filesystem auditing or media mounts. Optional folder auditing is
  deferred beyond 1.0; any future design must remain read-only and never feed deletion.
- Another frontend decomposition, event bus/service locator or framework rewrite.
- P2 polish may move to a later release only with explicit documented deferral; this never
  applies to misleading state, lost protection, keyboard access or failing scale budgets.
