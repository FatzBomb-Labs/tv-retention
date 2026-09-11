# Frontend refactor handoff

Session handoff, 2026-09-10. Read this with `AGENTS.md`, `README.md`, and
`docs/PLAN.md`. This records the conversation and distinguishes approved work from
proposals still needing review. Implementation and tests outrank historical prose.

## Current state

- Repository: `/config/workspace/containers/tv-retention`.
- Earlier investigation used `/config/workspace/plugins/TV-Delete`. Do not work in
  that old directory; it retained files absent from the current repository.
- Branch: `master`.
- Latest commits:
  - `c9df46b` — Phase S: stabilization (two unresolved references fixed, frontend
    executable test suite added).
  - `c04a2f8` — Flatten the source tree for the container.
  - `b9d68e3` — Refresh the application icons.
  - Previous baseline: `c244f07` — Five views were rendering outside the layout.
- Working tree has uncommitted Phase 2 changes (see "Phase 2 — module delivery"
  below).
- `src/tv-retention/` has been flattened to `src/`:

  ```text
  src/
    assets/                 app.js, app.css, icons.css, icon PNGs
    include/interface.html
    worker/                 Python application
  tests/
  tools/check-on-host.sh
  ```

- Dockerfile, test source paths, validation script, README architecture tree, and
  the documented syntax-check path were updated for the move.
- The last missed path fixes were `tests/test_migration.py` and the unused-code
  scanner in `tests/test_names.py`: `WORKER.parents[2]` became `WORKER.parents[1]`
  when locating repository tests.
- The user removed ignored, untracked `dist/` plugin-release artifacts. That
  directory is absent in this repository.
- The user intentionally regenerated six icon PNGs. Their content updates were
  committed separately before flattening. The flattening commit also recorded
  existing executable permissions on those six PNGs (100644 → 100755). This was
  disclosed; no subsequent permission change was made.
- **Phase S (pre-refactor stabilization) landed** in `c9df46b` — see "Phase plan
  and gates" below. Phase 2 (module delivery) is done in the working tree but
  uncommitted: `app.js` is now a module loaded via `<script type="module">`, the
  release-namespace serving is fully implemented, and the import-purity test
  enforces the invariant file by file. No extraction into separate files has begun.
- Nothing has been deployed or pushed during this work.

### Validation completed

`./tools/check-on-host.sh` passed after the path fixes:

- 472 Python unit tests passed.
- Worker import check passed.
- `app.js` syntax check passed.
- `git diff --check` passed before committing.

This validates the flattening; it does not establish that all browser behavior is
correct. Most frontend tests inspect source strings rather than execute UI flows.
No Docker image build or browser interaction test was performed for the flattening.

Phase S (`c9df46b`) re-validated `./tools/check-on-host.sh` end to end, with the
new executable frontend suite now part of it:

- 473 Python unit tests passed (472 + the new static guard).
- 8 frontend runtime tests passed (`tests/frontend/`, `node:test` over a fake DOM
  with a fixture-backed `fetch` that contacts nothing).
- Worker import check and `app.js` syntax check passed.
- `git diff --check` clean.

Phase 2 (uncommitted) added:

- 482 Python tests (473 + 9 release-namespace and asset-serving tests in
  `test_server.py`; the static-guard count shifted slightly during the merge).
- 9 frontend runtime tests (8 original + the import-purity test in
  `module-purity.test.js`).
- Module-syntax check now covers every `src/assets/*.js` as ES modules.
- `app.js` converted from IIFE to `<script type="module">` entry.
- `server.py` serves a versioned release namespace (`/assets/<digest>/<file>`).
- Flat asset names kept as a 1-week compatibility path.
- `AGENTS.md` records the new constraints (release namespace, import purity).

### Commit identity

The user supplied:

- Author: Keith Litfin
- Nick: FatzBomb
- Company, as supplied: FatzBomb Entertianment
- Email: fatzbomb75@yahoo.com

The two commits used `Keith Litfin <fatzbomb75@yahoo.com>` as author and committer
through command-scoped environment variables. Git configuration was not changed.
Earlier commit attempts failed because identity was missing; those attempts created
no commits. Do not assume persistent Git identity is now configured.

## User goals and constraints

Refactor toward cohesive single-responsibility modules, with `app.js` ultimately
being the composition root/bootstrap rather than a collection of feature bodies.

- Preserve behavior during extraction; stabilize known defects separately first.
- No framework rewrite, new functionality, arbitrary splitting, or one-function-per-file.
- No dependencies or build system without unusually strong justification.
- Every incremental phase must remain runnable and testable.
- Preserve retention/deletion semantics, confirmation gates, monitoring intent,
  exclusions, cache-age presentation, and queued-removal undo behavior.
- Do not silently turn proposed bug fixes into approved semantics.
- Do not change production services as part of refactoring.

The user approved and completed icon commits and source flattening. They asked for
another review/question before beginning frontend work. Creating this handoff is
the current authorization; it is not authorization to start stabilization or extraction.

## Architectural findings

`src/assets/app.js` currently owns all of the following:

1. Root/API/CSRF setup; DOM construction and formatting helpers.
2. Busy-overlay depth, RPC transport, timeout handling, notices, error guarding,
   session-expiration redirect, and shared dialog lifecycle.
3. Snapshot/settings/health/alert state and derived queries.
4. Sequential per-rule checks, forced checks, sweep polling, heartbeat and banners.
5. Full-page rendering and sidebar navigation/remembered sections.
6. Header counts, run states, scheduled-change menu, preview and run confirmation.
7. Shared change summaries, episode rows, preview dialogs and result presentation.
8. Library loading/cache invalidation, filters, sorting, bands, list/grid cards,
   posters, size controls, theme and browser preferences.
9. Alert cards, acknowledgment, quick fixes, and connection navigation.
10. Preset editing and shared keep-condition fields.
11. Monitoring and exclusion trees (deliberately separate semantics).
12. Series editor identity, scope counts, draft retention forms, exclusions,
    one-time monitoring passes, details pane and draft preservation.
13. Queued removal, typed destructive confirmation and undo.
14. Schedule auto-save, connections, automation, air-date providers, notifications,
    alert preferences, logging settings, whole-document collection and saving.
15. Stats, run history, About and live-log polling.

### State ownership concerns

- Shared application data: `snapshot`, `settings`, `monitoring`, `alertsByRule`,
  `systemAlerts`.
- Catalogue data: `library`, `seriesCache`, `libraryLoading`.
- Check machinery: `checking`, `forced`, `checkQueue`, `checkRunning`,
  `bulkChecking`, `pollTimer`, `watchStamp`.
- Navigation/preferences: `currentView`, `libraryFilter`, `layout`, `scale`,
  `showSleeping`; persisted `tvr.*` localStorage keys.
- Editor-local state: `editing`, `drafts`, per-form controls, trees, debounce timers,
  remembered pre-edit scope, read timestamps and exclusion selections.
- Settings-local collectors: radio/input maps, phrase-list readers, `airOrder`,
  `airEnabled`.
- Transport/log-local state: `busyDepth`, `logTimer`, `logOffset`.

Do not gather all of these into a universal store merely because they used to be
globals. Feature-local state should stay private. A small application-data object
or `createState()` is sufficient for genuinely shared data; no reactive store is
required. Preserve existing alias/replacement behavior first: `settings` is assigned
from `snapshot.settings`, not an independently deep-cloned document.

### Important contracts

- `server.py` builds the page wrapper with `#tv-retention`, `data-api`, and
  `data-csrf`. Currently it loads one classic script at the end of the body.
- RPC uses form POST `/api`: `csrf_token` plus JSON `payload` containing `action`.
  Preserve action names, payloads, timeout/quiet flags, and response handling.
- `actions.py` owns dispatch; backend validation remains authoritative. There are
  25 registered actions in the reviewed surface (earlier replies said 26).
- `/poster` is a separate authenticated GET, keyed by instance, series and artwork
  stamp; the browser does not receive Sonarr API keys.
- HTML IDs, `data-view`, `data-section`, `data-section-head`, CSS state classes,
  custom size properties, hidden attributes, and localStorage keys are contracts.
- The library's three navigation views share one physical panel.
- Native dialog `close` handling, removal confirmation words (`DELETE`,
  `DELETE ALL`), button defaults and event propagation must survive extraction.
- Static controls have listeners attached during current script evaluation;
  dynamic controls acquire listeners as they are constructed. Editor-body and
  settings-view input/change listeners depend on bubbling. Phrase removal emits
  a synthetic bubbling change event.
- Do not change render fan-out casually: full `render()` rebuilds settings controls
  but does not generally rebuild the editor or fetch Stats.
- Settings saves collect fields across several views into one document. Schedule
  and enable switches also go through this path; separating views must preserve it.
- Preserve editor save ordering: settings → match → outside-scope monitoring pass
  → chosen monitored-flag differences → queued check. Do not parallelize writes.
- `tests/test_build.py` heavily matches exact source strings and function blocks;
  `test_server.py` and `test_migration.py` also read JavaScript source directly.
  RPC reachability, icons and HTML/CSS contracts are checked this way.

## Revisions requested by the independent review

The user explicitly challenged four parts of the first proposal:

1. Versioning only the entry script does not version transitive ES-module imports.
2. `state.js` must not contain mutable UI/render callback registries.
3. A roughly 2,200-line `series.js` is not justified by library/editor coupling.
4. Imports execute before the entry body; its root guard cannot protect import-time
   DOM access, timers, storage access, or event binding.

These concerns stand. The subsequent assistant response did not fully resolve all
of them. The following is the corrected direction to review before implementation.

### Explicit orchestration, not a registry

- `app.js` constructs features and passes named callbacks/capabilities directly.
- No exported mutable `ui.render` table, event bus, subscriber registry, or generic
  service locator in `state.js` (or simply relocated to another module).
- Library receives `onOpenSeries`, selection information and preview/alert actions;
  it does not import the editor.
- Editor receives save/check/removal actions and `onSelectionChanged`/completion
  callbacks; it does not import library render functions directly.
- Checks report specific lifecycle updates to callbacks assembled in `app.js`.
  The composition root performs the required targeted renders in their original order.
- State mutation functions do not render. In particular, the existing render side
  effect in `applyAlerts` must become an explicit orchestration call at all callers.
- Pass narrow functions, not a single object containing every feature instance.

### Cohesive series decomposition

The last proposal still put trees and destructive removal into a roughly 1,250-line
editor. A better candidate for review is:

- `library.js`: catalogue loading/invalidation, browsing, bands, cards and controls.
- `series-editor.js`: form/details lifecycle, drafts, identity, scope counts and save.
- `episode-trees.js`: both tree builders, independently implemented; no RPC or saves.
- `series-removal.js`: queued removal dialog, typed confirmation, optional monitoring
  selection, queue/undo presentation. Uses the monitoring tree through an explicit API.

The monitoring tree returns episode-ID differences; the exclusion tree returns
season/episode-number selections, including whole-season entries. Sharing a file
does not mean merging their semantics into a generic checkbox tree.

### Side-effect-free imports and initialization

Module evaluation may define functions/constants; it must not query DOM, read
localStorage, start timers/network calls, mutate shared runtime state, or bind events.

Recommended lifecycle:

1. Entry checks for the root, installs startup error reporting.
2. Create DOM/dialog/feedback and transport instances from the root and API/CSRF.
3. Create shared application data and feature instances without starting them.
4. Wire narrow callbacks in `app.js`; constructors must not invoke callbacks while
   peer instances are still being assigned.
5. Explicitly initialize/bind static controls once; apply theme/layout preferences.
6. Start heartbeat/visibility handling at the deliberate bootstrap point. Preserve
   initial snapshot guards and timer intervals.
7. Load snapshot, apply health/alerts, perform the original render sequence, then
   queue stale checks or observe existing progress.

Keep initialization order separate from import order. Per-form timers and callbacks
need ownership documented; cancellation/race fixes are separate behavioral changes
unless specifically approved. A root-absent import/bootstrap test should prove that
no DOM access, fetch, interval or event registration leaks through imports.

## Module caching: unresolved proposal corrected

Current `server.py` hashes only `app.js`, `app.css`, `icons.css`; PNG content does
not change that key. Earlier claims that icon edits automatically bust it were wrong.
Assets currently receive a one-week cache lifetime. Asset serving rejects names
containing `/` or `\\`.

Neither `app.js?v=KEY` nor dynamically importing only its immediate children with
`?v=KEY` solves unversioned static imports inside those children. The earlier
dynamic-import recommendation was incomplete. Do not implement it as recorded in
the conversation.

A complete, no-build candidate is a versioned URL namespace:

```text
/assets/<release-digest>/app.js
/assets/<release-digest>/dom.js
/assets/<release-digest>/series-editor.js
```

Static `import './dom.js'` then resolves within the same digest namespace at every
depth. Source files can remain flat in `src/assets/`.

Requirements before adopting this design:

- Digest a deterministic manifest of asset filenames and bytes, not just the entry.
- Serve each digest only from exactly the bytes used to compute it (for example,
  an immutable in-memory asset snapshot created at server startup).
- Emit entry and stylesheet URLs from that same snapshot; version relevant icons
  too if promising icon cache invalidation.
- Allow only the exact `<digest>/<allowlisted-flat-filename>` route shape; retain
  traversal protection, correct MIME types and `nosniff`.
- Give successful digest-addressed responses long-lived immutable caching.
- An unavailable old digest must fail with a non-cacheable response, never redirect
  to or silently serve the current release. An upgrade mid-load may require reload,
  but must not assemble a mixed-version graph.
- Existing flat asset URLs need an explicit compatibility policy; the new module
  graph must not use them. No runtime regex rewriting of JavaScript imports.
- Test transitive import resolution, changed-child invalidation, old-key rejection,
  MIME/cache headers and traversal. Test a partially cached old graph across upgrade.

An import map covering every transitive specifier is another viable design, but no
caching design was finally approved. Prefer reviewing the namespace design rather
than adding query strings only to the first level of dynamic imports.

## Pre-refactor stabilization: confirmed defects, fixes still to settle

Resolved in `c9df46b`; retained below as the record of the defects and of the
testing approach that justified the fix.

These line numbers remain valid in the moved `src/assets/app.js`.

### Undefined `renderStats()` — lines 281 and 305

Confirmed in `drainChecks`' per-rule `finally` and `startPolling`'s tick. The defined
`renderStatsView()` at line 3281 is a different responsibility: it fetches `/api`
`stats` for the Stats page on navigation.

Historical evidence: old `renderStats()` updated the overview counts/badges; the
sidebar redesign (`42b8522`) replaced full-render calls with `renderCounts()` and
`renderTopBar()`. The Stats page appeared in `2a265bf`. The two asynchronous callers
were left behind.

Current effects:

- Queue: exception in the inner finally exits the drain after its first completed
  rule; outer finally resets running/bulk flags, leaving remaining queued work.
- Polling: exception is caught as a poll failure, clears the interval and hides the
  progress banner before normal completion handling.

Do not blindly rename these calls to `renderStatsView()`; that adds unrelated RPCs.
The later advice to simply delete both calls also missed the historical badge
refresh responsibility. The likely minimal repair is `renderCounts()` at these
sites, preserving overview updates without fetching Stats. Review and test this
before approving it. Do not fabricate a fresh aggregate plan: these responses do
not themselves return a replacement `snapshot.plan`.

Regression tests should execute the flows with controlled responses/timers:

- Two queued checks both complete, alerts/header/sidebar counts update, queue
  state clears, no rejection and no `stats` RPC.
- A running progress tick retains polling and renders its banner; a later finished
  tick clears it. Verify count updates and quiet requests.

### Undefined `planText(plan)` — line 838

Confirmed reference with no definition in current source. Run with a truthy
`plan.actionable` fails before `window.confirm`; `guarded` reports the error and no
run request is issued.

Intended responsibility: describe the actual pending deletion, monitoring and
removal plan inside the confirmation. Reuse `changeSummary(plan)`'s semantics and
removal descriptions rather than introducing another independent description table.

The earlier proposed `changeSummary(...).map(row => row.text).join('; ')` cannot
simply follow `This will`: rows are already complete sentences (e.g. "2 series will
..."). Decide a grammatical confirmation framing, such as a labelled multiline
summary, and review Test Mode wording. Keep no-actionable, blocked, and no-runnable
branches, confirmation cancellation, and run RPC ordering intact.

Regression tests must execute the Run handler for mixed actions and Test/live
modes: confirmation includes counts/bytes/removal consequences; cancel sends no
`run`; accept sends one; blocked routes to attention; no-work follows its existing
branch. Do not exercise real Sonarr writes.

The earlier claim that `planText` never existed anywhere in history was not proven
by the searches performed. Only its absence in current source is established here.

### Testing approach

Add meaningful executable regression coverage before moving these flows. Node's
built-in test/assert/vm facilities and narrowly scoped DOM/timer/fetch fixtures are
a no-dependency option; choose the harness explicitly. Source-string assertions
alone did not catch either crash. A regex over `render*` names cannot catch
`planText`, imported aliases or general runtime name errors.

When modules arrive, adapt existing tests to their owning files or to the actual
reachable module manifest. Blindly concatenating arbitrary JS files alphabetically
can hide unresolved imports/dead modules and break block-slicing assertions. Retain
the safety assertions and supplement with executable flow/import-graph checks.
Syntax-check every shipped module in ES-module mode; do not assume a wildcard
passed to `node --check` checks every file or that every Node version treats `.js`
as ES modules automatically.

## Revised target tree — four of these exist; the rest are candidates

`dom.js`, `format.js`, `episode-trees.js` and `changes.js` shipped in phase 3. The
one deviation from the sketch below: the dialog, notice and feedback factories did
*not* go into `dom.js`. They call the RPC surface and raise the busy overlay, which
makes them transport concerns rather than construction, so they are phase 4's.
`dom.js` holds element construction and controls only, and imports nothing.

```text
src/assets/
  app.js              composition root, explicit full/targeted render orchestration
  dom.js              DOM construction and controls
  format.js           formatting helpers (bytes, dates, plurals)
  transport.js        RPC, timeouts, session handling; injected busy feedback
  state.js            shared application data and derived queries; no UI callbacks
  changes.js          plan descriptions, change rows, preview/result presentation
  checks.js           check queue, polling, heartbeat and lifecycle callbacks
  navigation.js       sections/views; explicit enter/leave callbacks
  topbar.js           counts, run state, menu and run/sync intents; theme controls
  library.js          browsing, catalogue cache, cards, bands and layout preferences
  series-editor.js    details/form lifecycle, draft scope and save orchestration
  episode-trees.js    independent monitor and exclusion tree implementations
  series-removal.js   typed confirmation, queue/undo and removal monitoring UI
  alerts.js           alert presentation, acknowledgment and quick-action flows
  settings.js         cohesive render/collect/save of global settings panels
  presets.js          preset CRUD and reusable keep-condition form fields
  connections.js      instance CRUD/test and TMDB test controls
  activity.js         Stats, history and live-log lifecycle
```

The exact small-helper grouping and settings boundaries can be adjusted after
review. Do not use line-count quotas. Shared keep fields should not force editor
to import a preset controller; they may be exported as a side-effect-free builder
or placed with shared controls.

Dependency direction:

```text
app.js → feature factories + shared data/transport
features → shared helpers / passed data and narrow actions
series-editor, series-removal → episode-trees
library, editor, activity, topbar → shared change presentation where needed
state → no views, DOM, transport, or app.js
```

Cross-feature interaction is wired at the composition root, not by importing
`app.js` or mutually importing library/editor/alerts/settings controllers.

## Phase plan and gates

Completed:
- Separate icon update and source-flattening commits, validated above.
- **Stabilization (Phase S) — `c9df46b`:** the two unresolved references are
  fixed, the `node:test`/`vm` runtime suite is added, and `tools/check-on-host.sh`
  runs it. Baseline: 473 Python tests + 8 frontend runtime tests, all green.
- **Module delivery (Phase 2) — uncommitted:** `app.js` converted from IIFE to
  `<script type="module">` entry (top-level strict, `start(root)`, trailing root
  lookup). `server.py` serves a versioned release namespace
  (`/assets/<digest>/...`) computed over every allowlisted file's name and bytes;
  flat names kept as a 1-week compatibility path. Import-purity test enforces the
  invariant file by file. `check-on-host.sh` syntax-checks every shipped module as
  ES modules. 482 Python tests + 9 frontend tests, all green. Browser smoke test
  of the module graph has not been run.

Before implementation: review the module tree and callback boundaries with the
user. (Stabilization and module delivery are settled; they are no longer open
choices.)

1. **Stabilization — landed (`c9df46b`).**
2. **Module delivery — landed.** The entry is a module, the release namespace is
   served, the purity test enforces import-time invariants, and `check-on-host.sh`
   checks every shipped file.
3. **Leaf helpers — landed.** Four modules out of the entry, in this order and each
   validated before the next: `format.js` and `dom.js` (no imports at all),
   `episode-trees.js` (imports `el`), `changes.js` (imports `el`, `bytes`,
   `plural`). `app.js` went 3,496 → 3,123 lines. Behavior preserved exactly:
   definitions were dedented two spaces and moved verbatim, with the explanatory
   comments carried across.

   Two things this phase taught, worth repeating in phase 4. **Comments strand.**
   Twice, a banner-to-closing-brace span did not line up with the comment blocks
   around it, and prose belonging to moved code was left behind in the entry — once
   above the wrong function. Read the seam in *both* files after every extraction.
   **Local names can shadow a new import.** `seriesAlertCard` and
   `renderAlertSettings` each declare a local `const options`, which now shadows the
   one imported from `dom.js`. Neither scope calls `options(...)`, so it is inert —
   but it is inert by luck, and renaming those two locals is cheap cleanup whenever
   their surrounding code moves.
4. **Transport/data boundaries:** extract RPC and genuinely shared state with
   explicit update notifications wired locally in `app.js`, not a registry.
5. **Peripheral features:** activity, presets, connections and settings; preserve
   whole-document collection and masked-key semantics.
6. **Alerts/navigation/topbar:** explicit view transitions, action callbacks and
   confirmation flows; maintain draft-discard and log-start/stop rules.
7. **Checks:** move queue/poll/watch as a unit with tested lifecycle callbacks.
8. **Series features:** removal UI, then editor and library using the established
   callback boundaries. Preserve scope/save ordering, selection and drafts.
9. **Finish composition root:** remove transitional wiring/aliases only after
   callers and tests have migrated; verify no import-time side effects or cycles.

Each phase: inspect diff, run `./tools/check-on-host.sh` plus relevant executable
frontend tests, then review/commit only that phase. Browser smoke checks must use
isolated fixtures or a copied configuration with schedule off; never permit a live
check to act on production Sonarr. Follow project guidance for any host/container use.

Highest risks: module cache coherence, settings replacement versus open drafts,
listener duplication/bubbling, queue/poll timer lifetime, loss of confirmation
gates, selection/editor callback cycles, and changing write order through async
reorganization. Preserve observed timing first; treat improvements as separate work.

## Other observations to keep separate

- `renderAlerts` has a trivial `matches = () => true` filter left over.
- Dirty tracking refers to nonexistent `#tvr-view-settings-schedule`; schedule is
  already excluded by the surrounding `.tvr-view:has(.tvr-save)` selection.
- `renderRules` aliases `renderLibrary`; retain until callers are migrated.
- Documentation and static HTML contain outdated plugin/Test Mode wording. Backend
  actions and current tests must be traced before assuming every documented
  invariant is enforced by every manual action.
- Do not fix unrelated defects, redesign scheduling/automation, or perform the
  planned backend staged-run work as part of module extraction.

## Next-session starting point

1. Verify current directory, Git status/log and applicable guidance.
2. Read this handoff, then inspect implementation for any decision being acted on.
3. Ask for approval of the outstanding module tree and callback boundaries.
4. Begin only the approved phase. Phases S, 2 and 3 are landed; the next is
   Transport/data boundaries (Phase 4).

Phase 4 moves, in `app.js` order: `busy`, `TIMEOUTS`, `DEFAULT_TIMEOUT` and `api`
under the `// -- transport` banner, then `notice`, `guarded` and `dialog`.
`showResult` and `changeList` sit next to them but call `dialog`, so they travel
with it or stay; decide that from the seam rather than in advance. Unlike phase 3
this is not pure leaf code — `busy` touches the DOM and `api` touches the network —
so the purity test is the thing to watch: neither may run at import, only when
called.
