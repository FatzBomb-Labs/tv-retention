# Code and security review

Audit of `src/`, `tools/`, `tests/`, `Dockerfile` and `docker-compose.yml`, requested as an
examination for code fixes, optimizations, memory leaks, edge cases, error trapping,
redundancy, dead code and general code health.

**Nothing was changed.** This document is the entire change: no source file was edited, and
every finding below is reported for someone to act on deliberately.

## Method

Every Python module was read directly. The frontend was read by a second, independent
reader, and each of its claims was then re-verified against source — line numbers, the
mechanism of the failure, and a grep for the symbol — before being listed here. Findings
already guarded by the existing tests were discarded rather than reported.

## Baseline

Measured before and after the audit, using the local inner loop described in
[AGENTS.md](AGENTS.md):

| Suite | Result |
|---|---|
| `test_build.py` | 156 passed |
| `test_migration.py` | 45 passed |
| `tests/frontend/*.test.js` | 13 passed |
| full suite (local) | 480 collected — 1 failure, 59 errors, all platform artifacts |

The full-suite failures are the two documented Windows artifacts and nothing else:
`worker/main.py` imports `fcntl` at module scope, which takes down every test that reaches
`server.py`, and `core.normalise` calls `os.path.normpath`, which rewrites `/tv/x` to
`\tv\x` and fails a mapping assertion that is correct in the Linux container. Neither is a
fault in the source. The authoritative gate remains `tools/check-on-host.sh`.

## Summary

| # | Severity | File | Lines | Issue | Confidence |
|---|----------|------|-------|-------|------------|
| 1 | HIGH | `src/worker/main.py` | 523-524 | TMDB air-date provider is permanently unreachable | 9/10 |
| 2 | MEDIUM | `src/assets/episode-trees.js` | 109-114 | Season checkbox cannot be un-ticked; stored whole-season exclusions are silently downgraded | 9/10 |
| 3 | MEDIUM | `src/assets/library.js` | 112, 306 | Library read failures are swallowed; the pane shows "Reading…" forever | 9/10 |
| 4 | MEDIUM | `src/worker/main.py` | 496-499 | Unsatisfiable `recycle.mode == 'plugin'` branch; leaves `Sonarr.rescan` dead | 10/10 |
| 5 | MEDIUM | `src/worker/main.py` | 446, 456 | Every rule is read from Sonarr twice per run | 8/10 |
| 6 | MEDIUM | `src/assets/series-removal.js` | 140-142, 62-63 | Unguarded `.find()` used after an `await`; a concurrent refresh loses the change | 7/10 |
| 7 | LOW | `src/worker/main.py` | 185-197, 446 | Vestigial 3-tuple return; `missing`/`unknown` are never read | 9/10 |
| 8 | LOW | `src/worker/store.py` | 287-305 | `read_log` byte-offset drift on non-ASCII or malformed content | 7/10 |
| 9 | LOW | `src/worker/store.py` | 87-100 | `state_dir()` writes and unlinks a probe file on every call | 8/10 |
| 10 | LOW | `src/assets/app.css` | 118, 359, 400, 408 | Four dead selectors, one from an incomplete rename | 9/10 |
| 11 | LOW | `src/assets/alerts.js` | 148 | Dead local; the dialog cannot name the instance it is about | 10/10 |
| 12 | LOW | `src/worker/actions.py` | 678, 706 | Cross-module use of privates; hardcoded `media.get("id", 1)` fallback | 7/10 |
| 13 | LOW | `src/worker/server.py` | 107-115, 159 | `_failures` mutated without its lock; `build_release()` walks the assets directory at import | 7/10 |
| 14 | LOW | `AGENTS.md` | — | Stale test counts and an inaccurate claim about the development environment | 10/10 |
| 15 | LOW | `src/assets/series-editor.js` | 465-468 | Debounced `scope-counts` responses have no sequence guard | 5/10 |

No CRITICAL findings.

## Findings

### 1. The TMDB provider never runs — HIGH

[src/worker/main.py:523](src/worker/main.py#L523)

```python
tmdb_cfg = settings.get('tmdb') or {}
if tmdb_cfg.get('enabled') and tmdb_cfg.get('api_key'):
    tmdb = TMDB(tmdb_cfg['api_key'], cache_path=state_dir(settings) / 'tmdb-cache.json')
```

`settings['tmdb']` never contains an `enabled` key. `DEFAULTS['tmdb']` is
`{'api_key': ''}` (`core.py:56`), `validate_settings` rebuilds the section as
`{'api_key': tmdb_key}` and drops unknown keys, and the migration deliberately deletes the
old flag — `tests/test_migration.py:98` asserts exactly that
(`self.new['tmdb'] == {'api_key': 'b' * 32}`).

So `tmdb_cfg.get('enabled')` is always `None`, `tmdb` is always `None`, and
`collect_episodes` never calls `fill_air_dates`. `fill_air_dates` has one call site in the
codebase; this is it.

Meanwhile the feature advertises itself as working: `AIR_DATE_PROVIDERS['tmdb']` says
`'needs': 'an API key, under Connections', 'built': True`, `DEFAULTS` says *"An API key is
the switch: nobody enters one they do not want used"*, and `air_dates.providers` defaults
to `['tmdb', ...]`. "Test TMDB" in the interface reports success, because
`action_test_tmdb` constructs `TMDB(key)` directly and never consults `run()`.

The user-visible effect: a configured API key is accepted, validated and reported healthy,
while every run silently evaluates air dates without it.

**Fix:** gate on the key alone.

```python
if tmdb_cfg.get('api_key'):
```

This is untested locally because `main.py`'s `fcntl` import blocks the tests that reach
`run()` on Windows — the host suite is the only place it is exercised.

### 2. The season checkbox cannot be un-ticked — MEDIUM

[src/assets/episode-trees.js:109](src/assets/episode-trees.js#L109)

```js
box.addEventListener('change', () => {
  wholeSeason = box.checked;
  ticks.forEach((entry) => { entry.tick.disabled = wholeSeason;
                             if (wholeSeason) entry.tick.checked = true; });
  refresh();
});
```

There is no else-branch. Ticking the season box ticks every episode and disables them;
un-ticking it re-enables them but leaves them all ticked, and `refresh()` then computes
`on === row.total` and sets `row.box.checked = true` again, so the box springs straight
back on. The only recovery is un-ticking every episode by hand.

The data consequence is worse than the display one. `picked()` (143-155) stops emitting the
`{season, episode: null}` entry once the box is off, so a stored whole-season exclusion —
which is documented as covering episodes that have not aired — is silently rewritten as an
enumerated list of episodes that currently exist.

Note the comment immediately above the handler: an override with nowhere to live is
*"more use than a box that springs back"*. The handler then builds a box that springs back.
The parallel tree handles this correctly at 244-245
(`episodeBoxes.get(id).checked = box.checked`), which is why this reads as an omitted
else-branch rather than a design choice.

**Fix:** record each tick's pre-season state at build time and restore it.

```js
entry.tick.checked = wholeSeason ? true : entry.picked;
```

### 3. Library read failures are swallowed — MEDIUM

[src/assets/library.js:112](src/assets/library.js#L112) and
[src/assets/library.js:306](src/assets/library.js#L306)

```js
if (isLibraryView()) loadLibrary().catch(() => { libraryLoading = false; });
```

`loadLibrary` already clears `libraryLoading` in its own `finally`, so the catch does
nothing except silence the rejection. Because the throw skips `renderLibrary()`,
`library` stays `null`, and `renderLibrary()` re-invokes `loadLibrary()` whenever
`library === null`, the pane sits on "Reading the stored library…" indefinitely and
re-fires a failing request on every render, reporting nothing.

This is the exception rather than the house style — every other fallible action routes
through `guarded`, which surfaces the message.

**Fix:** report it, or render an explicit error state instead of the loading paragraph.

### 4. A branch that cannot be taken — MEDIUM

[src/worker/main.py:496](src/worker/main.py#L496)

```python
if (settings.get('recycle') or {}).get('mode') == 'plugin':
    # Moving files out of the library is invisible to Sonarr until it rescans.
    with contextlib.suppress(SonarrError):
        client.rescan(rule['series_id'])
```

`recycle` is not in `DEFAULTS`, `migrate.py` explicitly deletes it
(`for gone in ('guards', 'sidecars', 'delete_empty_dirs', 'recycle')`), and
`validate_settings` would strip it regardless. The condition can never be true.

The consequence beyond the dead lines: `Sonarr.rescan` has exactly one call site, this one,
so it is dead API — the same class of problem the project's own "every action on the RPC
surface is one the interface actually asks for" constraint exists to catch.

**Fix:** delete the branch and `Sonarr.rescan`.

### 5. Every rule is read from Sonarr twice per run — MEDIUM

[src/worker/main.py:446](src/worker/main.py#L446) and
[src/worker/main.py:456](src/worker/main.py#L456)

```python
episodes, missing, unknown = collect_episodes(settings, rule, client, tmdb)   # client.episodes()
reconciled = reconcile_monitoring(settings, rule, monitoring_for(settings, rule, force=True), dry_run)
```

`collect_episodes` calls `client.episodes(rule['series_id'])`, then
`monitoring_for(force=True)` fetches and caches the same series' episodes a second time.
Sonarr's `/api/v3/episode?seriesId=` payload is large — the client's own note at
`sonarr.py:231` calls it "several megabytes for a long-running series" — so this doubles the
per-run read cost for every rule.

**Fix:** pass the episodes already in hand into the monitoring pass. Care is needed:
`monitoring_for(force=True)` also refreshes the episode cache, which is a side effect worth
preserving deliberately or replacing explicitly.

### 6. Unguarded lookup after an await — MEDIUM

[src/assets/series-removal.js:140](src/assets/series-removal.js#L140) and
[src/assets/series-removal.js:62](src/assets/series-removal.js#L62)

```js
await api('set-monitored', …, 'Setting monitoring in Sonarr…');   // up to 90 s
const target = (getSettings().rules || []).find((other) => other.id === rule.id);
target.queue = Object.assign({}, target.queue, { removal: { … } });
```

A background `refresh()` in that window replaces `settings` wholesale, so if the rule has
gone the next line throws `Cannot set properties of undefined` inside `guarded`, surfacing a
cryptic message and discarding a change the operator just confirmed. The `Undo` handler at
62-63 is the same shape with a much smaller window. `series-editor.js` guards the identical
lookup at 176 and 448 with `if (!target) return;`; `connections.js:42` is the only other
unguarded instance.

**Fix:** `if (!target) return;` after each `find`.

### 7. Vestigial 3-tuple return — LOW

[src/worker/main.py:185](src/worker/main.py#L185) and
[src/worker/main.py:446](src/worker/main.py#L446)

`collect_episodes` ends `return episodes, [], []`, and its only caller unpacks
`episodes, missing, unknown` and never reads the last two. The constraint "media files
Sonarr does not know about are reported, never deleted" describes a feature this shape was
built for and no longer delivers. `docs/PLAN.md` makes no mention of it, so it is dead
rather than reserved.

**Fix:** return the list alone and update the call site.

### 8. `read_log` byte-offset drift — LOW

[src/worker/store.py:287](src/worker/store.py#L287)

```python
handle.seek(offset)
text = handle.read(limit)
return {'offset': offset + len(text.encode('utf-8')), …}
```

The offset is a byte offset, and normally re-encoding the decoded text reproduces the byte
count exactly. It stops doing so when a byte sequence does not decode cleanly: under
`errors='replace'` a malformed byte becomes U+FFFD, which re-encodes to three bytes while
consuming one, so the returned offset drifts and the viewer can skip or re-read text. The
realistic trigger is non-ASCII log content — episode titles are logged, and accented
characters, dashes and CJK are all common — combined with the initial
`offset = max(0, size - limit)` landing mid-character.

The rotation handling above it (`if offset > size: offset = 0`) is correct.

**Fix:** open in binary mode and decode after reading, so the byte count is the one the
file gave you.

### 9. `state_dir()` probes writability on every call — LOW

[src/worker/store.py:87](src/worker/store.py#L87)

```python
directory.mkdir(parents=True, exist_ok=True)
probe = directory / '.writable'
probe.write_text('')
probe.unlink()
```

`state_dir` is called by essentially every cache read, cache write, log write and journal
append, so this creates, writes and unlinks a file constantly — churn worth avoiding on
flash-backed storage, which is what Unraid arrays are.

**Fix:** probe once and remember the result, or probe only when the directory is first
created.

### 10. Dead CSS selectors — LOW

[src/assets/app.css:118](src/assets/app.css#L118), [359](src/assets/app.css#L359),
[400](src/assets/app.css#L400), [408](src/assets/app.css#L408)

`button.tvr-rollup` (the group selector at 118 and its own rule at 400) and
`button.tvr-folder` (359) match nothing in any module or in `interface.html`.
`.tvr-queued-mark` (408) is an orphan of a rename: the live marker is `.tvr-queued-x`,
styled at 225 and emitted by `library.js:190`, so the intended treatment for a queued
series silently stopped applying.

This is exactly the failure mode the cascade section of [AGENTS.md](AGENTS.md) warns about:
dropping a rule produces no error, only air.

**Fix:** delete 359 and 408, and `button.tvr-rollup, ` from the group selector at 118 plus
the rule at 400. Keep `button.tvr-side-item` at 118 — it is live.

### 11. Dead local in the recycle-bin dialog — LOW

[src/assets/alerts.js:148](src/assets/alerts.js#L148)

```js
const instance = (getSettings().instances || []).find((i) => i.id === alert.instance_id);
```

`instance` is never referenced again — the body uses `alert.instance_id` directly. As a
result the dialog cannot name the instance it is about, and `getSettings()` is walked on
every invocation for nothing.

**Fix:** delete the line, or use it and guard with `if (!instance) return;`.

### 12. Cross-module privates and a hardcoded fallback — LOW

[src/worker/actions.py:678](src/worker/actions.py#L678) and
[src/worker/actions.py:706](src/worker/actions.py#L706)

`action_enable_recycle_bin` and `check_recycle_bin` (`main.py:1417`) both reach into
`Sonarr._request(...)`, and `action_test_tmdb` does `from core import _text`, importing a
private helper across a module boundary. `action_enable_recycle_bin` also PUTs
`config/mediamanagement/{media.get("id", 1)}` with a hardcoded id of 1 as the fallback.

**Fix:** promote the two helpers to public methods on the client.

### 13. Startup and locking in the front door — LOW

[src/worker/server.py:107](src/worker/server.py#L107) and
[src/worker/server.py:159](src/worker/server.py#L159)

`_failures` is a bare module global mutated without the `_lock` that guards `_sessions`,
which is a timing-only concern here. Separately, `build_release()` calls
`directory.iterdir()` at import time, so a missing assets directory crashes the process at
import rather than reporting a startup error the way a missing credential does.

**Fix:** take the lock for `_failures`; move the directory walk into a guarded startup step.

### 14. Documentation drift — LOW

[AGENTS.md](AGENTS.md)

The document states the local inner loop covers `test_build.py` at 142 tests and
`test_migration.py` at 33, and describes the suite as "482 Python tests plus 9 frontend
runtime tests". The measured figures are **156**, **45**, **480 collected** and **13 frontend
runtime tests**. It also asserts "There is no usable Python in the development environment",
which is inaccurate — a working `.venv` (3.10.11) runs the source-reading and frontend
suites, and only the two documented platform artifacts hold.

### 15. No sequence guard on debounced counts — LOW

[src/assets/series-editor.js:465](src/assets/series-editor.js#L465)

Two `scope-counts` requests dispatched at least 250 ms apart by typing can land out of
order, and the later-landing stale response writes the older counts. Narrow and
self-correcting on the next keystroke.

**Fix:** capture a token per dispatch and ignore a response whose token is not current.

## Checked and found clean

- **No XSS.** Grepping all of `src/` for `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `createContextualFragment`, `document.write`, `eval(` and `new Function` returns zero
  matches. Every dynamic node is built through `dom.js`'s `el()`/`text()`, so no escaping
  helper is needed and none is missing.
- **No CSRF, XSS or path traversal in `server.py`.** `data-csrf` and the login page
  interpolate only `secrets.token_urlsafe(32)` and constants; `send_json` keeps
  `ensure_ascii=True`; `serve_asset` guards traversal by dict membership in the frozen
  release snapshot with an exact-equality digest compare; `public_assets` is a suffix
  allowlist; `read_form` runs unconditionally first in `do_POST`, so an oversize body cannot
  poison the next keep-alive request; sessions are in-memory with `HttpOnly` and
  `SameSite=Strict`; `handle_login` uses `hmac.compare_digest` for **both** username and
  password with a flat capped delay rather than a lockout; `startup_error` refuses to start
  without credentials unless `TVR_AUTH=none`.
- **No memory leaks.** The only page-lifetime timers are the intended 15 s `checks.js`
  watchdog and the log poll, and both clear before re-arming. No listener, observer or
  unbounded cache retains anything it should not.
- **Test Mode holds on every path.** `dry_run = preview or test_mode` at `main.py:515`
  governs manual runs as well as scheduled ones.
- **No unhandled exception escapes the RPC surface.** `dispatch` (`actions.py:762`) catches
  broadly and returns `{'ok': False, 'error': …}`.
- **The retention verdict logic is correct.** `evaluate`'s `any`/`all` combination matches
  the documented "an unknown protects the file" rule, `_order_key` falls back to
  `dt.date.min`, and `air_watermark`'s string-max over ISO dates is sound.
- **`tmdb.py`, `schedules.py` and `alerts.py`** are clean: error classification
  distinguishes 401 from other HTTP and from URL/timeout/OSError/JSON failures; cron
  expansion handles steps, ranges, wraps and day-vs-weekday semantics with a bounded
  `last_occurrence`; alert merge, resolve, fingerprint and `managed_only` filtering are
  consistent with the documented invariants.
- **`migrate.py`** applies its cascading `if version < N` steps correctly, including the
  `% 7` weekday mapping and `_guard` shape conversion.

## Code health

The layering described in [AGENTS.md](AGENTS.md) is real rather than aspirational.
`core.py` imports no network and no clock; `store.py` is filesystem-only; `actions.py`
imports `main` and never the reverse. Standard-library-only is confirmed. The code is
disciplined about not re-reading Sonarr except in finding 5, and the frontend's `guarded()`
house style is consistent enough that findings 2, 3 and 6 stand out as departures from it.

The dominant pattern across these findings is **code that outlived the thing it was written
for**. The `recycle` branch, the `missing`/`unknown` tuple, the TMDB `enabled` flag, the
reaches into `_request`/`_text`, and all four dead CSS selectors are leftovers from the
Unraid-plugin era. The migration layer prunes stored settings carefully in one direction and
has no counterpart that prunes the consumer side, which is why a deleted key is still read
and a renamed class is still styled. Finding 10 is the sharpest illustration: a rename left
the old rule behind, producing no error, only air.

Two things worth noting beyond the individual findings. First, `run()` is the
highest-traffic path in the application and has the thinnest local coverage, because the
`fcntl` import blocks every test that reaches it on Windows — and that is precisely where
finding 1 hides. The documented host gate is the right mitigation, but it means the
best-hidden bug class is the one that only surfaces there. Second, finding 1 is the only
item that makes a shipping feature silently not work; everything else is dead weight, a
display defect or a narrow edge case.
