# Frontend refactor completion record

The decomposition of `src/assets/app.js` is complete. This file records the final
architecture and the contracts that future changes must preserve; it is no longer a
phase plan.

Read it with `AGENTS.md`, `README.md`, and `docs/VALIDATION.md`. Implementation and tests
outrank this summary.

## Final state

The entry began at 3,496 lines and is now a composition root of roughly 350 lines. It
constructs feature factories, owns the few documents replaced wholesale at runtime, and
brokers explicit callbacks between features. It contains no feature-sized UI body.

```text
src/assets/
  app.js            composition root and full-render orchestration
  dom.js            DOM construction and controls
  format.js         bytes, dates, plurals and ranges
  storage.js        guarded per-browser preferences
  transport.js      RPC, timeouts and busy overlay
  feedback.js       notices, guarded actions and dialogs
  changes.js        plan descriptions and result presentation
  episode-trees.js  monitor and exclusion tree controls
  activity.js       stats, history, run reports and live logs
  settings.js       global settings panels and the read-only About projection
  checks.js         background check queue, sweep polling and heartbeat
  series-removal.js queued removal, undo and typed confirmation
  series-editor.js  details pane, drafts and rule-save orchestration
  alerts.js         alert cards, acknowledgement and quick actions
  navigation.js     sections, views and library filters
  topbar.js         counts, theme, run/sync actions and alert overview
  presets.js        preset CRUD and reusable retention-condition fields
  connections.js    Sonarr instance CRUD and connection tests
  library.js        catalogue caches, filters, bands, cards, layout and scale
```

No further frontend extraction is planned. In particular:

- `library.js` remains one module because catalogue state, band selection, cards and
  display preferences are one rendering closure. Splitting it would require a broad
  context object or sibling callback cycle.
- `series-editor.js` remains one module because the pane, draft map and form lifecycle are
  one closure.
- The small About renderer remains in `settings.js`: it is a read-only projection of the
  settings and snapshot accessors that module already owns, with no state or listeners of
  its own.
- `app.js` remains the explicit composition root. There is no event bus, mutable callback
  registry, shared state module or service locator.

## Dependency and state rules

- Modules that need replaceable documents receive accessors such as `getSettings`,
  `getSnapshot`, and `getMonitoring`; capturing those objects would make a module stale
  after the next save or refresh.
- A module that needs another feature asks the entry for a narrow capability named for
  intent. Sibling feature modules do not import each other.
- Writes to entry-owned state use named setters or invalidation intents. For example,
  instance saves call `forgetSeriesCache`; they do not receive the cache binding.
- Every module except `app.js` is side-effect-free at import. DOM listeners, storage
  reads, timers and network work begin only from a factory method such as `wire()`.
- Topbar/library cross-feature reads remain explicit and deferred: topbar receives
  `getLibrary` and `ruleFor` for sidebar counts; library receives `syncedAgo` for card
  status text.
- `renderLibrary` is the one library redraw capability. The historical `renderRules`
  compatibility alias has been removed.

## Behavior contracts preserved by the split

- Test Mode means no writes, scheduled or manual.
- A rule that does not resolve to exactly one Sonarr series is never processed.
- Deleting an episode file always unmonitors it.
- Destructive series removal remains queued and undoable until a run.
- Cached readings always show their age.
- Per-series Sonarr reads remain background work and never raise the global busy overlay.
- The Run button is disabled only by a complete, trustworthy plan with nothing actionable.
- Exclusions are decided once and outrank every retention rule.
- Library views share one catalogue and differ only by filter.
- Leaving the library closes the editor and clears drafts.
- Static listeners are attached exactly once from `wire()`; dynamic listeners are
  attached when their controls are constructed.
- Settings saves still post and replace the whole validated document.
- Asset imports stay within one release digest and every non-entry module remains safe to
  import without a page.

## Extraction verification

Each move was reconstructed against its parent commit after undoing only mechanical
accessor substitutions and listener lifting. The final large cuts matched exactly:

- presets: 97/97 lines
- connections: 122/122 lines
- theme: 23/23 lines
- library: 549/549 lines

The completed graph has also been reviewed for initialization order, stale state,
cache invalidation, listener duplication, render ordering and dependency cycles.

The authoritative gate is `./tools/check-on-host.sh`, or
`tools\check-on-host.ps1` from Windows. The current release passes 483 Python tests,
worker imports, syntax checks for all nineteen ES modules, and 13 frontend runtime tests.

## Release identity

`VERSION` is the semantic version, `BUILD` is the monotonically increasing shipped-build
number, and the image creates `BUILD_DATE` when it is built. Help -> About shows all
three; the top banner shows only the semantic version.

Increment `BUILD` for every deployed code change. Change `VERSION` when the release
meaning changes.

## Work that remains elsewhere

Product work is tracked only in `docs/PLAN.md`. The series-details regression reported
after the extraction is a behavior review, not unfinished module work, and should be
investigated against the live pane and pre-refactor history before changing it.
