# Frontend refactor handoff

The plan of record for breaking up `src/assets/app.js`. Read it with `AGENTS.md` and
`README.md`. Implementation and tests outrank anything written here.

Phases land one at a time, each validated and committed before the next begins.
**Delete from this file as work lands**, the same rule `docs/PLAN.md` follows: what
is left should describe what is left. Sections recording resolved arguments have
been removed; the decisions they reached are stated as decisions in AGENTS.md.

## Current state

Branch `master` as of 2026-09-11. Phases S, 2, 3, 4, 5, 6, 7 and 8
are landed. **Phase 6 is complete**: `changeList` moved into `changes.js` long ago,
phase 8 took the editor state that stood in its way, and the alerts cluster,
navigation and the top bar have now been cut into `alerts.js`, `navigation.js` and
`topbar.js`. See "Phase plan and gates", and read the phase-6 entry for how the three
cuts were sequenced. Since then `storage.js`, `presets.js` and `connections.js`
have been taken out too — the first cuts belonging to no numbered phase. The
independent theme control has since moved into `topbar.js`, and the catalogue,
cards, bands and display preferences have moved into `library.js`. **The frontend
module refactor is complete.**

```text
src/assets/
  app.js            349 lines — composition root, fourteen imports
  format.js         bytes, when, plural, ago, range — imports nothing
  dom.js            $, el, text, toggle, field, options — imports nothing
  storage.js        remember, remembered — the per-browser preferences, wrapped
                    because localStorage throws in a private window; imports nothing
  episode-trees.js  EXCLUDED_WHY, exclusionTree, monitorTree — imports el
  changes.js        REMOVAL_SUMMARY, changeSummary, changeLines, changeRows,
                    changeList — imports el, bytes/plural, dialog
  transport.js      busy, resetBusy, createApi, TIMEOUTS, DEFAULT_TIMEOUT — imports $
  feedback.js       notice, guarded, dialog — imports $, el
  activity.js       createActivity — stats, run reports, history, live log
  settings.js       createSettings — schedule, notifications, automation, air
                    dates, alert preferences, About
  checks.js         createChecks — the background check queue, sweep polling and
                    the heartbeat; imports $, el, text
  series-removal.js createRemoval — the queued-removal banner and the dialog that
                    queues it; imports $/el/toggle/field/options, plural/ago,
                    monitorTree, guarded/dialog
  series-editor.js  createSeriesEditor — the details pane and the rule form, which
                    are one closure and cut as one module; imports
                    $/el/text/toggle/field/options, bytes/plural/ago,
                    exclusionTree/monitorTree, changeLines/changeList,
                    notice/guarded/dialog
  alerts.js         createAlerts — the series and system alert cards, the quick
                    actions on them, the alerts tab and the re-check-all button;
                    imports $/el/field, ago/plural, dialog/guarded/notice
  navigation.js     createNavigation — which view is on screen, which sidebar
                    section is open, and the library's three-views-one-panel
                    filter; imports $, guarded, remember/remembered
  topbar.js         createTopBar — the age of the reading, the changes menu, theme,
                    sidebar counts, the Run button's three states and the one
                    overview of everything wrong; imports $/el, ago/plural,
                    changeSummary/changeList, dialog/guarded/notice,
                    remember/remembered
  library.js        createLibrary — the stored catalogue and series cache, filters,
                    bands, cards, layout and scale preferences; imports
                    $/el/text/toggle, bytes/when/plural/ago,
                    changeLines/changeList, guarded, remember/remembered
  presets.js        createPresets — the preset list and its editor, plus the two
                    keep-window helpers the series editor borrows
                    (conditionFields, presetSummary); imports $/el/field/options,
                    plural, dialog/guarded
  connections.js    createConnections — the Sonarr instance cards and the
                    test-before-save dialog; imports $/el/field/toggle, plural,
                    dialog/guarded
```

The entry began at 3,496 lines. Phases 3 and 4 moved six modules out of it without
changing behavior: definitions dedented two spaces and carried across verbatim.

The gate is `./tools/check-on-host.sh`, or `tools\check-on-host.ps1` from Windows;
both send the same remote script. Last green run 2026-09-11: 482 Python tests,
worker imports, nineteen assets parsing as ES modules, 13 frontend runtime tests.

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
  service locator anywhere — including relocated into another module.
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
  storage.js          per-browser preferences (remember/remembered)
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
   diff mechanical. `state.js` was left a candidate, unscheduled. It has since been
   **struck from the target tree**: every phase after this one passed accessors into
   factories instead, and that has held across sixteen modules. A `state.js` would
   put back the ambient mutable binding the accessor pattern removed, and nothing
   asks for it.

   Two things this phase learned. A mechanical rewrite must **assert the old symbol
   is absent** afterwards — `str.replace()` fails silently, and the `busyDepth` miss
   (two of the sites are indented, the pattern assumed column zero) was caught only
   by that assertion. And exported constants use a trailing `export { … }` list like
   every other module, not `export const`: the constant-declaration test scans for
   `const NAME`, and `export const NAME` reads as undeclared.
5. **Peripheral features — landed.** The sketch named four modules; the call
   graph cut it to two. `saveSettings` is a hub — `deleteSeries`, `editInstance`,
   `editPreset`, `queuedBanner`, `renderInstances` and `ruleForm` all call it, and
   it calls `render` — so three of the four sketched modules would have imported it,
   two of them (presets, connections) for no other reason. Those two stayed in the
   entry that phase; presets has since been cut, taking `saveSettings` as a single
   passed-in capability, which is cheap enough that the objection no longer holds. `activity.js` went first because it is the only cluster with no
   `saveSettings` edge; `settings.js` follows.

   **`activity.js` — landed.** `renderStatsView`, `showResult`, `renderHistory`,
   `startLog`, `stopLog`, plus a `wire()`. `app.js` went 3,035 → 2,900 lines.

   Two things shaped it, and both apply to `settings.js`. First, this is the first
   module that needed *mutable entry state*: `snapshot` and `settings` are `let`
   bindings reassigned at six sites, so a plain import would have frozen the panes
   on whichever document was current at load. It exports a factory —
   `createActivity({ api, refresh, getSnapshot, getSettings })` — taking accessors,
   not values. Second, **the wiring is the real hazard, and the plan did not mention
   it.** Nine top-level `addEventListener` statements sit *between* cluster
   functions; they are statements, not declarations, so a function-by-function
   extraction leaves them in the entry bound to functions that have moved, with no
   error. Two were activity's (`tvr-clear-history`, `tvr-log-clear`) and became
   `wire()`, which the entry calls at start-up — before the first `refresh()`, which
   is where module evaluation used to attach them. They cannot run at import: the
   purity test enforces that for every file but the entry.

   **`settings.js` — landed.** 457 lines, `app.js` 2,900 → 2,505. The sketch was
   wrong a second time: the cluster is ~29 declarations, not nine, and nearly all of
   them are cluster-private. It came out as two non-contiguous spans, cut bottom-up,
   because `renderInstances`/`editInstance` sit between them and stay in the entry.
   Exports `renderSchedule`, `renderSettings`, `renderAlertSettings`, `renderAbout`,
   `renderAirProviders`, `saveSettings`, `collectSettings` and `wire()`. Two
   predictions in the sketch did not survive contact: `showResult` turned out to be
   unused here, so there is no edge to `activity.js` at all, and `renderAirProviders`
   moved rather than staying behind — nothing outside the cluster referenced it.

   Three things are worth knowing about it. **The entry keeps ownership of the
   state.** `saveSettings` *writes* `settings`, `snapshot.settings` and
   `snapshot.schedule_text`, which accessors cannot do, so the entry passes
   `applySaved(data)` — the one hook that assigns them, keeping every write to those
   bindings in one file. **`saveScheduleNow` gained an `overrides` argument rather
   than a second save path**: it used to set `settings.schedule` locally and then
   save, relying on `collectSettings` spreading the mutated document. Passing the
   schedule through as an override posts the identical payload without leaving local
   state changed when a save fails. **The `options` shadow is gone** — it had to be:
   `renderAlertSettings`'s local `const options` and `renderSchedule`'s use of the
   imported `options()` landed in the same file, where luck runs out. It is now
   `alerts`.
6. **Alerts/navigation/topbar:** explicit view transitions, action callbacks and
   confirmation flows; maintain draft-discard and log-start/stop rules.
   **Landed, in four cuts.** `changeList` is out — 69 lines into `changes.js`, verbatim
   but for the dedent, verified line-for-line against the previous commit. It was
   the one cut here with *no* entry state at all: it reads only the result it is
   handed and calls `dialog`, `el`, `changeLines` and `changeRows`, all already
   imports. Four callers, now on the import.

   The blocker is now cleared. Navigation used to read `editing`, `forgetDrafts`
   and `renderDetails` directly; phase 8 took that state, and what navigation sees
   today is final shape: `forgetDrafts()` and `closeEditor()` from the entry, and
   one edge in the other direction — `openLibraryView()`, the callback the editor
   is handed in place of `if (!LIBRARY[currentView]) showView('series-all');`.
   That collapsed three names about one intent into one, so navigation has a
   single edge to honour rather than three.

   **Alerts is out** — 232 lines into `alerts.js`, verified line-for-line against
   the previous commit at 209/209 after undoing every mechanical rewrite. It went
   first because it was the only one of the three that was contiguous (one span,
   no interleaving), and because cutting it *shrinks* the remaining problem rather
   than deferring it. Fourteen names arrive from the entry; `settings`, `snapshot`,
   `monitoring` and `systemAlerts` as accessors, because the entry replaces all
   four wholesale and a captured value would go stale the first time a document
   came back. `ALERT_TAG`, `ACTION_LABEL`, `alertItem` and `runAlertAction` had no
   callers outside the span, so they stayed private rather than being re-exported.

   The important move was **brokering navigation as two intents**. A card's "Show
   in Series" button did `showView('series-all')`, set the search box and called
   `renderRules()`; the open/test-instance action did `showView(…)`, found the
   instance and called `editInstance`. Those are now `showSeriesInLibrary(rule)`
   and `openInstance(id)`, implemented in the entry. That removes `showView`,
   `renderRules` and `editInstance` from the surface entirely, which means the
   navigation module has **no edge into alerts at all** — the same pattern as
   phase 8's `openLibraryView`, and the reason alerts was worth doing first.

   `wire()` is needed here, unlike the editor cut: `tvr-recheck-all` is a node in
   `interface.html`, not one the module makes. Every other listener in the cluster
   attaches to an element it creates, so the rest needed nothing.

   **Navigation is out** — 116 lines into `navigation.js`, verified 88/88 against
   the previous commit. It went as its own cut rather than joined to the topbar,
   and measuring is what decided it: navigation and the topbar are adjacent in the
   entry but they call *nothing* of each other's, so adjacency was the only
   argument for joining them, against a 21-name surface and a 33-name one. Ten
   names arrive, `settings` as an accessor; three leave. `remember`/`remembered`
   stayed in the entry and are passed in, because series, presets and instances
   use them too — promoting them to a `storage.js` is a separate question and one
   this cut did not have to answer. It was answered afterwards, in the affirmative:
   `storage.js` exists and `navigation.js` imports it directly.

   Three names became two accessors on the way out. `LIBRARY[currentView]` had two
   readers outside the span and `libraryFilter` had two more; exporting the tables
   would have handed out the internals, so `isLibraryView()` and
   `getLibraryFilter()` answer the questions instead. The cut also confirmed the
   editor's `openLibraryView` was the right shape: it needed no change at all, only
   `isLibraryView()` in place of the table lookup.

   **The topbar is out** — 234 lines into `topbar.js`, verified 201/201 against
   the previous commit; `app.js` 1,278 → 1,109, which closes phase 6. Eighteen
   names arrive, four of them accessors — `settings`, `snapshot`, `systemAlerts`
   and `library` are all reassigned wholesale in the entry — and five leave:
   `renderTopBar`, `renderCounts`, `renderRunButton`, `syncedAgo` and
   `showEverythingNeedingAttention`. `RUN_STATES`, `runState` and `setBadge` had
   no caller outside the span and stayed private.

   The one tie was `showEverythingNeedingAttention`, which calls `seriesAlertCard`
   and `systemAlertCard`. Sibling modules cannot import each other here, so the
   entry brokers those two, and **that fixes the order the factories are
   constructed in** — `createAlerts` before `createTopBar`. Nothing in the code
   stated that constraint and a later edit would have silently broken it, so both
   factory blocks now say so in a comment.

   `$('tvr-refresh-all')` wrote `settings` and `snapshot.settings` directly; it is
   now `applySaved(data)`. That is behaviour-neutral rather than merely tidier, and
   checking is what established it: `applySaved` also writes
   `snapshot.schedule_text = data.schedule_text || snapshot.schedule_text`, and
   `action_sync` in [worker/actions.py](../src/worker/actions.py) returns no
   `schedule_text`, so the `||` falls through to the held value every time.

   Three renders left as three rather than one, because two of them answer to
   things that do not go through the bar: `renderRunButton` works out its own
   "nothing to do" so `applyAlerts` can call it on the alert schedule, and
   `renderCounts` answers to the check queue, which finishes per series. Folding
   them would make every one of those a full redraw.

   `renderCounts` reads `library`, `ruleFor` and `seriesAlertList`, which are
   library concerns, and it went here anyway: it is the sidebar's counts and the
   sidebar is the bar's sibling. Worth revisiting when `library.js` is cut — the
   same is true of `syncedAgo`, whose one outbound caller is `alertBadge`.

   **Phase 7 was taken ahead of the rest of this one**, because it was the only
   remaining cluster with no stake in that argument.
7. **Checks — landed.** `checks.js`, 164 lines; `app.js` 2,436 → 2,308. Moved as
   a unit, as planned: `queueChecks`, `drainChecks`, `startPolling`, `watchTick`,
   `renderCheckBanner` and `isChecking`, plus `CHECK_PHASE` and `WATCH_SECONDS`.

   This is the cleanest cluster in the file and the reason is worth stating: all
   seven of its mutable bindings — `checking`, `forced`, `checkQueue`,
   `checkRunning`, `bulkChecking`, `pollTimer`, `watchStamp` — are read and
   written *only* here. Nothing outside touched one. So unlike phases 5 and 6,
   the state did not have to be brokered; it simply went with the code, which is
   what makes the factory worth having. Nine callbacks in, four names out
   (`isChecking`, `queueChecks`, `startPolling`, `bulkChecking`).

   `bulkChecking` is the one shape change. It was a bare `let` read by
   `renderLibrary`; it is now a `bulkChecking()` accessor, because a value
   exported at construction would have frozen at `false` forever. The other
   direction needed the same care in reverse: `monitoring` is *reassigned* by
   `applyHealth` on every reading, so it is passed as `getMonitoring()` rather
   than as the object — the module only ever writes into it, never replaces it.

   `renderCheckBanner` turned out to be entirely private — three callers, all
   inside the cluster — so it is not exported at all. The two wiring statements
   (`setInterval(watchTick, …)` and the `visibilitychange` listener) became
   `wire()`, called from the entry's start block beside `activity.wire()` and
   `settingsView.wire()`; they cannot run at import, and the purity test says so.
   Timing is unchanged: both registered inside `start(root)` before the first
   `refresh()` in the old code too, and `watchTick` still returns early until a
   snapshot exists.

   Every executable line was diffed against the previous commit and is identical
   modulo the dedent and those two accessor rewrites. The gate's first two
   frontend tests cover this code directly — the queue draining and the sweep
   poll — so the move has behavioural coverage, not just a syntax check.
8. **Series features:** removal UI, then editor and library using the established
   callback boundaries. Preserve scope/save ordering, selection and drafts.
   **Started — the removal UI has landed.** `series-removal.js`, 148 lines;
   `app.js` 2,308 → 2,179. `REMOVAL_ACTIONS`, `REMOVAL_CONFIRM`, `REMOVAL_SONARR`,
   `REMOVAL_FILES`, `queuedRemoval`, `queuedBanner` and `deleteSeries` moved as a
   block — they were already contiguous, lines 2041–2165, with the constants
   immediately above their only readers.

   The surface is the narrowest of any cut so far: **four callbacks in**
   (`api`, `getSettings`, `saveSettings`, `renderDetails`), **three names out**
   (`queuedRemoval`, `queuedBanner`, `deleteSeries`). All four constants proved
   private — zero references outside the span — so they went with the code rather
   than being re-exported. `queuedRemoval` has four outside callers, all cheap
   predicates (`scheduledFor`, `posterNode`, a band filter, `changeMarks`).

   `getSettings()` rather than `settings`, for the reason established in phase 5:
   the binding is reassigned wholesale every time a document comes back, so a
   captured object would go stale the first time a save succeeded. `saveSettings`
   is passed as the function it is — it is never reassigned, only destructured
   from `settingsView` above.

   **No `wire()` here**, and the difference from phase 7 is worth knowing. All
   four of this module's `addEventListener` calls attach to elements the module
   itself just created, inside the functions that create them. Nothing binds to a
   document-level or pre-existing node, so there is nothing to register at start
   and nothing that could double-bind on a re-render.

   Verified by reconstruction rather than by filtering: the module body was
   re-indented and the accessor rewrite undone, then compared to the original
   span — 125 lines identical byte-for-byte, comments included. The entry
   remainder diffs to exactly two additions, the import and the factory block.
   (Two earlier filter-based scripts reported phantom differences by stripping
   `}` and `getSettings` lines from one side only. Reconstruction cannot lie that
   way, and is the better tool for a verbatim move.)

   **The editor has landed, and phase 8 is done.** `series-editor.js`, 734 lines;
   `app.js` 2,179 → 1,524. This is the largest module in the tree and it is one
   cut on purpose: `renderDetails` calls `editing.build(body, top)` and
   `editing.save(context, startEnabled)`, and `ruleForm` writes into the `drafts`
   map that `renderDetails` reads and `forgetDrafts` clears. The pane and the form
   are one closure, and the target tree named exactly one module for them.

   A sub-cut *was* measured before being rejected, and the reason generalises. The
   identity panel (old lines 1390–1598) showed only ten inbound names, which looks
   like a seam — but eight of its locals (`identity`, `enabled`, `sayCounts`,
   `sayNext`, `sayPlan`, `sayState`, `reread`, `readLine`) are consumed 160+ lines
   later, at 1762–1764, 1805, 1847, 1853 and 1988–1989. **Dependency counts alone
   do not find seams inside a closure**: grep the candidate's locals for uses past
   the proposed boundary before believing a low number.

   `seriesFor` was excluded, and it had been wrongly counted in every earlier
   measurement because it sits under the `// -- adding and editing a series`
   header directly above `ruleForm`. Its only caller is `loadLibrary`. Dropping it
   moved the boundary to 1348 and removed `seriesCache` from the surface outright.
   **Section headers in this file are not cluster boundaries.**

   Eighteen callbacks in, six names out (`editing`/`setEditing`, `isOpen`,
   `openEditor`, `renderDetails`, `forgetDrafts`). Four accessors rather than
   objects — `getSettings`, `getSnapshot`, `getMonitoring`, `getLibrary` — all
   four being bindings the entry reassigns wholesale. The one *write* the editor
   did, `settings = matched.settings; snapshot.settings = settings;`, became
   `applySaved(matched.settings)`, which already does both. No `wire()`: all
   thirteen listeners attach to elements the module creates.

   `openLibraryView()` is the navigation broker described in the phase-6 entry.
   The reverse direction was already brokered: `showView` and `cardShell` now call
   `closeEditor()` instead of assigning `editing = null`.

   Verified by reconstruction: 700 of 700 lines identical. One warning from the
   attempt — a substitution of `getLibrary()` without a lookbehind rewrote
   `forgetLibrary()` into `forlibrary` and produced a phantom diff. **Every
   identifier substitution in these moves needs `(?<![.\w$])name(?![\w$:])`**; the
   trailing `:` guard keeps object keys intact.

   Five `test_build.py` tests were updated. They read the whole module graph via
   `interface_js()`, so the move itself was invisible to them — what changed was
   the asserted *text*: `monitoring[rule.id]` → `getMonitoring()[rule.id]`,
   `settings.profiles` → `getSettings().profiles`, `editing = null` →
   `closeEditor()`, and two `split()` anchors that depended on the old
   indentation.
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
- `seriesAlertCard` declares a local `const options`, which shadows the one imported
  from `dom.js`. That scope never calls `options(...)`, so it is inert — but by luck.
  Rename when its surrounding code moves. `renderAlertSettings` had the same problem
  and was fixed on the move into `settings.js`, where it stopped being inert.
- Documentation and static HTML contain outdated plugin/Test Mode wording. Backend
  actions and current tests must be traced before assuming every documented
  invariant is enforced by every manual action.
- Do not fix unrelated defects, redesign scheduling/automation, or perform the
  planned backend staged-run work as part of module extraction.

## Refactor complete

Every numbered phase is landed — S, 2, 3, 4, 5, 6, 7 and 8 — together with the
four final cuts: `storage.js`, `presets.js`, `connections.js` and `library.js`.
There is no remaining extraction planned.

Theme was measured separately before the library cut. It has no call edge to the
catalogue, cards or layout, and its control lives in the top bar, so its 23 lines
moved into `topbar.js`; exact reconstruction was 23/23. Initialization and the
listener moved into `wire()` to retain import purity.

The remaining series block reconstructed exactly, 549/549, into one
`createLibrary` factory. The catalogue and series caches, lazy loading, filters,
bands, cards, layout, scale and their listeners are one closure. Splitting cards
or display preferences would have passed redraws, editor actions, alert actions
and layout state across sibling boundaries without creating an independent
responsibility.

Two cross-feature edges remain explicit and brokered by the entry:
`topbar.js` reads `getLibrary` and `ruleFor` for sidebar counts, while
`library.js` reads `syncedAgo` for a clear card's tooltip. `connections.js` calls
the library's `forgetSeriesCache` intent after an instance save. None is a sibling
import or service locator, and every callback is deferred until after composition.

The final extraction was reviewed for initialization order, stale state, cache
invalidation, listener timing, render ordering and hidden dependency cycles.
Local source-reading and runtime suites passed, followed by the authoritative
host gate: 482 Python tests, worker imports, nineteen ES modules and 13 frontend
runtime tests.
