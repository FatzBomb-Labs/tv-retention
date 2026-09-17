# Frontend architecture after extraction

The decomposition of `src/assets/app.js` is complete; no further extraction is planned.
This records module ownership and requirements for future changes, not proof that every
behavior is correct. [PLAN.md](PLAN.md), revised 2026-09-17, owns the unresolved safety,
persistence and UI work. [VALIDATION.md](VALIDATION.md) distinguishes historical passing
checks from the later failing review. See `AGENTS.md` for repository and release rules.

## Module responsibilities

`app.js` is the composition root: it constructs feature factories, owns replaceable
shared documents, orchestrates full renders and brokers explicit feature callbacks.
The 19-module responsibility map is:

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
  alerts.js         alert cards, acknowledgement, scoped suppression and system actions
  navigation.js     sections, views and library filters
  topbar.js         counts, theme, run/sync actions and alert overview
  presets.js        preset CRUD and reusable retention-condition fields
  connections.js    Sonarr instance CRUD and connection tests
  library.js        catalogue caches, filters, bands, cards, layout and scale
```

`library.js` keeps catalogue state, bands, cards and display preferences in one rendering
closure; `series-editor.js` keeps the pane, drafts and form lifecycle together. About stays
in `settings.js` as a read-only projection with no independent state or listeners.

## Dependency, state and lifecycle rules

- `app.js` remains the explicit composition root: no event bus, mutable callback registry,
  shared state module or service locator. Sibling feature modules do not import each other;
  cross-feature work uses narrow, intent-named capabilities supplied by the entry.
- Replaceable documents are read through accessors such as `getSettings`, `getSnapshot`
  and `getMonitoring`, not captured object references that go stale after save/refresh.
- Entry-owned state changes use named setters or invalidation intents. Instance saves call
  `forgetSeriesCache`, rather than receiving the cache binding. Plan invalidation must
  cover retention, exclusions, queue, instance and mode changes; revision coupling and
  stale-response handling are unresolved work in PLAN, not guarantees of this structure.
- Cross-feature reads stay explicit and deferred: topbar receives `getLibrary` and `ruleFor`
  for sidebar counts; library receives `syncedAgo` for card status. `renderLibrary` is the
  single library redraw capability; the old `renderRules` alias is gone.
- Every module except `app.js` must be side-effect-free at import: no DOM access, storage,
  listeners, timers or network work. Only the entry may look up `#tv-retention` on import;
  feature work begins through explicit factory/method calls.
- Attach static listeners exactly once from `wire()` and dynamic listeners when controls
  are constructed, not on every full render. Asset imports must remain within one release
  digest, and non-entry modules must be importable without a page.

## Behavioral requirements — not current guarantees

The split was intended to preserve these boundaries. The latest review found violations;
mechanical extraction checks do not demonstrate end-to-end safety. PLAN defines the fixes
and required regression evidence.

- Test Mode must prevent external Sonarr mutations on every entry point, scheduled or
  manual. Settings remain editable; permitted local operational writes need an explicit
  policy. Immediate monitoring/recycle-bin paths currently violate the external-write
  requirement, so the old blanket claim that nothing writes is not implementation evidence.
- Process a rule only with exactly one confirmed Sonarr series identity, including queued
  and resumed work. No file deletion may proceed without confirmed unmonitoring of all
  affected episodes. Ordinary retention never monitors; explicit removal dispositions
  remain separate operator intent. The executor regression currently fails.
- Destructive removal stays queued and undoable until execution starts; stale drafts or
  recovered intents must not resurrect canceled work.
- Decide exclusions once, ahead of every retention rule. Protect monitoring state and
  every file containing excluded content. Retention edits must preserve exclusions;
  exclusion loss and shared protected/delete files are confirmed review failures.
- Show cached readings with their age. Per-show Sonarr reads stay background work, with
  no global busy overlay or unrelated-show blocking.
- Suppress Run as having nothing actionable only on a complete, current, trustworthy
  plan. Stale/partial/unknown readings cannot justify an empty-plan claim; queued removals
  must count toward eligibility. Freshness, confirmation and revision behavior need PLAN's
  targeted tests.
- Library views share one catalogue and differ by filter. Leaving the library closes the
  editor and clears drafts; late responses must not repopulate a closed or different pane.

## Save implementation and evidence

Settings saves currently post and replace the whole validated document. **This is an
implementation detail, not an immutable contract.** PLAN calls for backend revisions and
conflict rejection, owned-field/rule-ID merges, serialized frontend saves and request/draft
generation checks so stale submissions or responses cannot erase newer intent.

Extraction was checked against parent commits with mechanical accessor/listener changes
accounted for, and the graph was reviewed for initialization, state access, invalidation,
listeners, render order and cycles. Those historical checks are not proof that the review
findings are fixed or that the current gate passes. Validation commands and release identity
policy belong in `AGENTS.md`; dated results and coverage limits belong in VALIDATION.
The cleanup-time Linux gate failed on the known executor regression before reaching
frontend checks; see VALIDATION. No deployment was performed. Remaining product and
correctness work belongs in PLAN, not an unfinished extraction plan.
