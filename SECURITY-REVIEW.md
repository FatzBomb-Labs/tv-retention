# Code and security review notes

Status consolidated 2026-09-17. **Review notes, not a security certification or release
approval.** The [production-readiness plan](docs/PLAN.md) owns implementation, priorities
and release gates; [VALIDATION.md](docs/VALIDATION.md) owns recorded validation evidence.

## Scope and evidence

The earlier review covered `src/`, `tools/`, `tests/`, `Dockerfile` and Compose. This
cleanup used targeted read-only source/test inspection to retire obsolete findings, then
reran the isolated Linux gate: 591 tests, one known executor failure. The gate stopped
before separate import, module-syntax and frontend checks; see VALIDATION for details.
Frontend tests cited below indicate coverage, not fresh execution. No deployment or
live-service checks were performed. Historical totals are not current assurance.

## Current risks

The September 17 review recorded four confirmed failures, retained in the plan:

- The operation executor does not dispatch its Sonarr operations (Phase 1.1;
  `tests/test_run_executor_review.py`). Fixing dispatch alone must not enable an unsafe
  write path: protection, checkpoint and recovery gates must ship with it.
- Retention editor saves lose exclusions (Phase 1.2).
- A shared episode file can be selected for deletion despite an excluded member
  (Phase 1.3).
- AniList overwrites authoritative Sonarr dates (Phase 4.1; required before enabling its
  retention contribution in a release).

Immediate monitoring and recycle-bin writes bypass the run's Test Mode policy; Phase 1.4
requires a common external-mutation boundary. Settings remain editable; the precise
allowed local operational writes must be documented rather than promising “nothing writes.”
Source-inspected concerns about concurrent state, restore, HTTP handling, date identity
and estimates, civil-time scheduling, and asynchronous UI state remain work in Phases
2–6. Their timing, permissions and failure consequences are not all behaviorally verified.
Phase 7 requires isolated acceptance and recorded counter-evidence or fixes, not silent
closure of suspected findings.

## Threat and deployment boundaries

- The application has Sonarr credentials and deletion authority. No media mounts or direct
  media filesystem access limits one path of damage; incorrect API calls can still destroy
  library content. Sonarr's recycle bin is not a substitute for correct permission checks.
- The application writes configuration/state; these and backup archives can contain
  credentials and executable intent. Host/volume administrators are trusted. Restore,
  concurrent saves and interrupted work need the plan's durability and re-arming controls.
- Use a restricted trusted network or controlled HTTPS reverse proxy, not default public
  exposure. Current login uses `TVR_USERNAME`/`TVR_PASSWORD`, in-memory sessions and CSRF
  tokens. `TVR_AUTH=none` explicitly removes authentication; forwarding headers are not an
  authentication boundary. Transport/cookie policy, Unicode credentials, request validation
  and admission limits remain Phase 6 work.
- Standard-library-only and one process reduce dependencies, not review obligations.
  Privilege dropping, base-image renewal, resource bounds, shutdown and worker readiness
  need container evidence. See [CONTAINER.md](docs/CONTAINER.md).

## Disposition of the earlier 15 findings

Numbers identify the old report, not a new severity ranking. “Resolved” below is limited
to the named old mechanism; it does not certify the surrounding workflow.

| Old finding | Current evidence and disposition |
|---|---|
| **1 — Unreachable TMDB** | Resolved old gate: `main.py` constructs TMDB from the key and its provider chain calls `fill_air_dates`, without requiring the deleted `tmdb.enabled` field. Provider correctness remains Phase 4.1. |
| **2 — Season checkbox** | Resolved toggle mechanism: `episode-trees.js` restores `entry.picked` when whole-season selection is cleared; `tests/frontend/episode-trees.test.js` covers toggling and locked automatic exclusions. This does **not** resolve editor-save exclusion loss (Phase 1.2). |
| **3 — Silent library failure** | Resolved old loading loop: `library.js` retains `libraryError` and re-renders on failure; `tests/frontend/library.test.js` covers visible failure and explicit retry. |
| **4 — Dead recycle/rescan branch** | Removed: targeted `src/` searches find no `rescan` definition/call or old plugin recycle branch in `main.py`. |
| **5 — Duplicate episode read** | Resolved specific pair: `process_rule` passes `preloaded=episodes` through `monitoring_for` to `episodes_for`, which stores that reading when persistence is requested. This is not a whole-run performance or executor guarantee. |
| **6 — Missing rule after await** | Both removal and Undo lookups in `series-removal.js` now report a missing target; `tests/frontend/series-removal.test.js` covers disappearance during an in-flight write. Broader stale queue/save races remain Phases 1.2, 2 and 5. |
| **7 — Vestigial tuple** | Removed: `collect_episodes` returns the episode list and current callers consume it directly. Filesystem auditing remains explicitly out of scope in the plan. |
| **8 — Log byte-offset drift** | Resolved byte accounting: `store.read_log` reads binary data and advances by `len(raw)`; `tests/test_store.py` covers split UTF-8 and malformed bytes. Frontend poll ordering remains Phase 5. |
| **9 — Probe on every state lookup** | Reduced to a timed writability cache in `store.state_dir`; `tests/test_store.py` covers reuse and expiry. This does not establish concurrent-storage safety (Phase 2). |
| **10 — Dead CSS selectors** | Named obsolete selectors (`tvr-rollup`, `tvr-folder`, `tvr-queued-mark`) are absent from `src/`. **Follow-up:** visually verify intended queued-marker treatment under Phase 5; removal alone is not visual evidence. |
| **11 — Recycle dialog dead local** | The unused instance lookup is gone from `alerts.js`. **Follow-up:** the dialog still does not name the affected instance; retain this UI clarity issue for Phase 5. |
| **12 — Private helpers/guessed config ID** | Callers use `Sonarr.media_management`, `set_media_management` and `core.validate_text`; the setter rejects a missing ID rather than defaulting to 1. External-write policy remains Phase 1.4. |
| **13 — Login counter/startup assets** | `server.py` locks counter updates and reports unreadable/empty asset directories via `startup_error`. The snapshot is still built at import; per-file read failures are not covered by that directory guard. **Follow-up:** broader startup/error handling and login admission remain Phase 6, not a blanket fix. |
| **14 — Documentation drift** | The cleanup corrects local-subset versus Linux-gate guidance in `AGENTS.md`, removes fixed test-count promises and distinguishes historical results from current failures. Implementation-dependent documentation and release evidence still require Phase 7 reconciliation. |
| **15 — Stale scope counts** | Still open under Phase 5: `series-editor.js` debounces dispatch but applies returned counts without a request-generation guard. Browser timing consequences need targeted validation. |

The old categorical assurances about XSS/CSRF, leaks, Test Mode, exception containment,
retention correctness and “clean” modules are withdrawn. Source patterns and passing
historical tests cannot establish those guarantees. Follow the plan's fixture, browser,
container and release gates before enabling destructive operation.
