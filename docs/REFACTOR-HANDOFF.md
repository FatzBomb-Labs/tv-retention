# Frontend refactor handoff

The plan of record for breaking up `src/assets/app.js`. Read it with `AGENTS.md` and
`README.md`. Implementation and tests outrank anything written here.

Phases land one at a time, each validated and committed before the next begins.
**Delete from this file as work lands**, the same rule `docs/PLAN.md` follows: what
is left should describe what is left. Sections recording resolved arguments have
been removed; the decisions they reached are stated as decisions in AGENTS.md.

## Current state

Branch `master`, working tree clean as of 2026-09-11. Phases S, 2, 3 and 4 are
landed; **Phase 5 (peripheral features) is next.** See "Phase plan and gates".

```text
src/assets/
  app.js            3,035 lines — entry, six imports, everything not yet extracted
  format.js         bytes, when, plural, ago, range — imports nothing
  dom.js            $, el, text, toggle, field, options — imports nothing
  episode-trees.js  EXCLUDED_WHY, exclusionTree, monitorTree — imports el
  changes.js        REMOVAL_SUMMARY, changeSummary, changeLines, changeRows
  transport.js      busy, resetBusy, createApi, TIMEOUTS, DEFAULT_TIMEOUT — imports $
  feedback.js       notice, guarded, dialog — imports $, el
```

The entry began at 3,496 lines. Phases 3 and 4 moved six modules out of it without
changing behavior: definitions dedented two spaces and carried across verbatim.

The gate is `./tools/check-on-host.sh`, or `tools\check-on-host.ps1` from Windows;
both send the same remote script. Last green run 2026-09-11: 482 Python tests,
worker imports, seven assets parsing as ES modules, 9 frontend runtime tests.

Nothing has been pushed during this work; there is still no remote and no tag. The
split module graph *has* now been exercised in a browser: 2026-09-11, against an image
built on the host from the phase-4 tree, pointed at a copy of the settings with the
schedule forced off. Seven modules under one digest, every import resolving, no console
error, the busy overlay never raised by a per-show read. See
[VALIDATION.md](VALIDATION.md). The long-running `tv-retention-demo` container is
unrelated to this work and still runs `c9df46b`.

### Commit identity

`Keith Litfin <fatzbomb75@yahoo.com>`, passed command-scoped —
`git -c user.name=... -c user.email=... commit`. Git configuration holds no
identity and should not be given one. Commits also carry the `Co-authored-by`
trailer for the assistant.

`core.filemode` is set `false` in this clone. On Windows, Git otherwise reads
NTFS's fake `100644` off every file and reports `tools/check-on-host.sh` as
modified forever, and a commit made from Windows silently drops its exec bit.

## User goals and constraints

Refactor toward cohesive single-responsibility modules, with `app.js` ultimately
being the composition root/bootstrap rather than a collection of feature bodies.

- Preserve behavior during extraction; stabilize known defects separately.
- No framework rewrite, new functionality, arbitrary splitting, or one-function-per-file.
- No dependencies or build system without unusually strong justification.
- Every incremental phase must remain runnable and testable.
- Preserve retention/deletion semantics, confirmation gates, monitoring intent,
  exclusions, cache-age presentation, and queued-removal undo behavior.
- Do not silently turn proposed bug fixes into approved semantics.
- Do not change production services as part of refactoring.

Each phase is approved on its own before it starts. Landing one is not authorization
to begin the next.

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
- `actions.py` owns dispatch; backend validation remains authoritative. `ACTIONS`
  holds 25 entries, and a test asserts the interface can reach every one of them.
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

## Settled design directions

Four objections to the original proposal shaped these, and all four were sustained.
Two are now enforced by shipped code: asset versioning covers transitive imports
(the release namespace, phase 2) and import purity is a test that runs file by file
(phase 2). The other two are constraints on phases still to come — no mutable
callback registry in a state module, and no 2,200-line `series.js`. What follows is
the direction, not an open question.

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

## Landed before extraction began

**Phase S (`c9df46b`) fixed two crashes** that would have been extracted along with
their bugs. `drainChecks` and `startPolling` both called an undefined
`renderStats()` — a casualty of the sidebar redesign, which replaced it with
`renderCounts()` and `renderTopBar()` but missed the two asynchronous callers. The
queue exited after its first completed rule; the poll tick cleared its own interval
and hid the progress banner early. Both became `renderCounts()`, which restores the
overview without fetching Stats. Separately, the Run confirmation called an undefined
`planText(plan)`, so a run with an actionable plan failed before `window.confirm`
and issued nothing; it now builds its confirmation from `changeSummary`'s rows.

Neither was caught by source-string assertions, which is why the executable frontend
suite exists: a regex over `render*` names cannot see `planText`, an imported
alias, or any other runtime name error. That suite is the regression record for both.

**Phase 2 settled asset delivery.** The release namespace, its digest and the
import-purity rule are stated as constraints in `AGENTS.md` and enforced by
`test_server.py` and `module-purity.test.js`. The arguments that produced them —
why `app.js?v=KEY` cannot version a transitive static import, and why a per-file
digest joined and truncated follows only the first file — are recorded there too.

## Revised target tree — six of these exist; the rest are candidates

`dom.js`, `format.js`, `episode-trees.js` and `changes.js` shipped in phase 3;
`transport.js` and `feedback.js` in phase 4. Two deviations from the sketch below.
The dialog, notice and feedback factories did *not* go into `dom.js` — `dom.js`
holds element construction and controls only, and imports nothing. And what the
sketch draws as one `transport.js` with "injected busy feedback" shipped as two
modules, because the code holds two clusters with no call edge between them:
`api` → `busy`, which needs the page's endpoint and CSRF token, and
`dialog` → `guarded` → `notice`, which needs nothing but `dom.js`. Neither imports
the other, and no feedback injection was required.

```text
src/assets/
  app.js              composition root, explicit full/targeted render orchestration
  dom.js              DOM construction and controls
  format.js           formatting helpers (bytes, dates, plurals)
  transport.js        RPC, timeouts and the busy overlay
  feedback.js         notice, guarded, dialog
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

1. **Stabilization — landed (`c9df46b`).** Two unresolved references fixed, the
   `node:test`/`vm` runtime suite added and wired into the gate.
2. **Module delivery — landed.** `app.js` converted from IIFE to a
   `<script type="module">` entry (top-level strict, `start(root)`, trailing root
   lookup). `server.py` serves the release namespace, computed over every
   allowlisted file's name and bytes; flat names remain as a one-week compatibility
   path. The purity test enforces import-time invariants file by file, and the gate
   syntax-checks every shipped module as an ES module. Exercised in a real browser
   on 2026-09-11, after phase 4: the namespace, the depth-one import graph and the
   refusal of an unheld digest all behave on the wire.
3. **Leaf helpers — landed.** Four modules out of the entry, in this order and each
   validated before the next: `format.js` and `dom.js` (no imports at all),
   `episode-trees.js` (imports `el`), `changes.js` (imports `el`, `bytes`,
   `plural`). `app.js` went 3,496 → 3,121 lines. Behavior preserved exactly:
   definitions were dedented two spaces and moved verbatim, with the explanatory
   comments carried across.

   One hazard worth repeating in phase 4: **comments strand.** Twice, a
   banner-to-closing-brace span did not line up with the comment blocks around it,
   and prose belonging to moved code was left behind in the entry — once above the
   wrong function, where it read as an explanation of something it had nothing to do
   with. Read the seam in *both* files after every extraction.
4. **Transport/feedback — landed.** `transport.js` (`busy`, `resetBusy`,
   `createApi`, `TIMEOUTS`, `DEFAULT_TIMEOUT`) and `feedback.js` (`notice`,
   `guarded`, `dialog`). `app.js` went 3,121 → 3,035 lines. Two closures had to be
   broken: `api` read `API`/`CSRF` off `root.dataset`, so the module exports
   `createApi(endpoint, csrf)`, called once at start-up and returning a function
   still named `api` — all 34 call sites unchanged. And `busyDepth` was reset
   directly by three sites outside `busy()`, so `resetBusy()` is exported for them.

   Shared state was *not* extracted, though the phase as planned bundled it with
   RPC: the transport half is cohesive on its own, and shipping it alone kept the
   diff mechanical. `state.js` is still a candidate, now unscheduled — no phase
   below depends on it, so it can be taken whenever the shared data is clear.

   Two things this phase learned. A mechanical rewrite must **assert the old symbol
   is absent** afterwards — `str.replace()` fails silently, and the `busyDepth` miss
   (two of the sites are indented, the pattern assumed column zero) was caught only
   by that assertion. And exported constants use a trailing `export { … }` list like
   every other module, not `export const`: the constant-declaration test scans for
   `const NAME`, and `export const NAME` reads as undeclared.
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
- `seriesAlertCard` and `renderAlertSettings` each declare a local `const options`,
  which shadows the one imported from `dom.js`. Neither scope calls `options(...)`,
  so it is inert — but by luck. Rename when their surrounding code moves.
- Documentation and static HTML contain outdated plugin/Test Mode wording. Backend
  actions and current tests must be traced before assuming every documented
  invariant is enforced by every manual action.
- Do not fix unrelated defects, redesign scheduling/automation, or perform the
  planned backend staged-run work as part of module extraction.

## Next-session starting point

1. Verify current directory, Git status/log and applicable guidance.
2. Read this handoff, then inspect implementation for any decision being acted on.
3. Ask for approval of the outstanding module tree and callback boundaries.
4. Begin only the approved phase. Phases S, 2, 3 and 4 are landed; the next is
   Peripheral features (Phase 5).

Phase 5 takes activity, presets, connections and settings. These are not leaf code
and not a mechanical move: each is a render/collect/save cluster reached from the
settings view, so the boundary to find is what the entry must still call and what
can become private to the module. Preserve whole-document collection and
masked-key semantics — a settings save reads the whole document rather than one
panel, and a masked key left unedited must not be written back as its mask.

Read the cluster before deciding its module count. Phase 4's plan said one module
and the code said two; the call graph is the authority, not the sketch.
