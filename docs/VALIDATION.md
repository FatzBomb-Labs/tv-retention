# Validation record

Last run: 2026-09-11, from Windows via `tools\check-on-host.ps1` against FatzServer
(Unraid 7.3.2, Python 3.11.15, Node 22.18.0).

## Automated

`./tools/check-on-host.sh`, or `tools\check-on-host.ps1` — source staged under `/tmp` on
the host, removed afterwards.
It installs nothing, touches no `/boot` path, reads no media, and contacts no Sonarr.

| Check | Result |
|---|---|
| `python3 -m unittest discover -s tests` | 482 tests, all pass |
| Worker imports | every module loads, server.py included |
| `node --input-type=module --check` over every `src/assets/*.js` | no syntax errors, checked as ES modules — ten files now: the entry plus `format`, `dom`, `episode-trees`, `changes`, `transport`, `feedback`, `activity`, `settings`, `checks` |
| `node --test tests/frontend/*.test.js` | 13 tests, all pass — 8 runtime flows, 4 settings-contract tests, plus module-import purity |
| `docker build` | 129 MB image — last built 2026-09-11 from the phase-4 tree; the module split changes no build step, only which files `COPY src/assets/` picks up |
| Container, end to end | refuses to start unconfigured; 303 to /login without a session; 401 on a bad password; 403 on a good session with a wrong CSRF token; `snapshot` answers with settings migrated v7 to v8; a percent-encoded traversal 404s; /config written as the requested uid with no chown asked |

### Coverage by area

| File | Tests | What it holds |
|---|---|---|
| `test_build.py` | 104 | The interface, checked statically |
| `test_monitoring.py` | 33 | The two modes, the keep frame, and what each one asks Sonarr to do |
| `test_freshness.py` | 31 | Reading ages, staleness, what may be shown as current |
| `test_migration.py` | 30 | Settings v1 → v7, each step and the whole chain |
| `test_schedules.py` | 26 | When a job is due, including what cron cannot express |
| `test_retention.py` | 23 | Every condition, every combine mode, air-date precedence, the guards |
| `test_mapping.py` | 19 | The Sonarr payload as it actually arrives, through the real client |
| `test_cache.py` | 16 | Cache keys derived from the mapping's shape |
| `test_queue.py` | 15 | Queued removals and the check queue |
| `test_settings.py` | 19 | Validation, redaction, injection and traversal rejection, one rule per series |
| `test_names.py` | 16 | Names each module can reach, names nothing uses, alert display rules |
| `test_presets.py` | 10 | Shared values, and what a preset may not do |
| `test_progress.py` | 12 | The progress marker, the banner over it, and what the header totals |
| `test_sonarr.py` | 9 | Rule-to-series matching, and ambiguity refused rather than guessed |
| `test_unaired.py` | 9 | Unaired seasons, and the next episode due |
| `test_server.py` | 26 | What the front door refuses, guards and lets through — and the release namespace on the wire |

`test_build.py` is the largest because the interface is checked statically: it is the file
with no runtime under test, so the guards that would otherwise be a browser sit here.
Among them — every element the script hides exists in the markup, every id is unique,
braces and parentheses balance, and every icon the interface names is one we ship.

`test_server.py` covers what used to be somebody else's problem. The plugin was handed
authentication and a CSRF token by emhttp and never had to be right about either. One of
its tests found that `TVR_PORT=` — set but empty, a realistic way to write a compose file —
would have taken the container down at startup, because `os.environ.get`'s default applies
to a variable that is absent rather than one set to nothing.

## Live, read-only, against 3022 series and 36 rules

Measured 2026-09-07. Staged under `/tmp` with `TVR_CONFIG` pointed away from `/boot`, and
every Sonarr call asserted to be a GET.

| Measurement | Result |
|---|---|
| Full read of the 36 bound series | 72 calls, 1.3s, 14.8 MiB |
| Re-check with nothing changed | 0 calls, 0.03s |
| Re-check after editing every rule's keep window | 0 calls, 0.03s |
| One tick's change feed (`history/since`, 90s window) | 2 calls, 0 KiB, ~1s |
| Heartbeat recomputing all 36 plans | 0 calls, 0.07s |
| New-series check (catalogue, six-hourly) | 1 call, 3.6s, 11.5 MiB |
| Settings migrated v4 → v5 | `monitor_missing` gone from all 36 rules, mode `unmonitor-only` |
| Plan under Unmonitor only | 186 deletions, 0 monitoring changes |
| Plan under Full sync | 186 deletions, 290 episodes monitored |

The three zero-call rows are the point of the design: re-deciding a rule is arithmetic over
episodes already held, so editing a keep window, raising a preset or a day passing costs
nothing at all.

## Earlier live checks

Against the running `Sonarr-Series` container, from a `/tmp` staging directory with
`TVR_CONFIG` pointed at `/tmp`. GET requests only.

| Check | Result |
|---|---|
| `test-instance` | Sonarr answered, and reported its recycle-bin setting |
| `match` | The folder `News & Talk/Daily Show, The (1996) {tvdb-71256}` matched "The Daily Show" |
| `preview`, preset-driven | A rule pointing at a "Keep 180 days" preset resolved correctly: 66 episodes considered, 1 selected with a real Sonarr air date (2026-03-06), 65 kept |

## The module graph in a browser

Exercised 2026-09-11, against `tv-retention:phase4` built on the host from the phase-4
tree and run on port 18788 against a *copy* of the demo settings with `schedule.enabled`
forced false and Test Mode on. The original `/tmp/tvr-demo` config was not opened for
writing, and the container was removed afterwards.

This is the first browser session the split module graph has had. Until it, the release
namespace and the seven-file import graph were validated only statically and over curl.

The counts below are that session's and are left as recorded. The tree has since grown
`activity.js`, `settings.js` and `checks.js`, so a repeat run should see ten files rather
than seven; what the session established — that a digest holds a whole graph and that a
stale one is refused — does not change with the count.

| Check | Result |
|---|---|
| Page loads, all seven modules fetched under one digest | `/assets/ad57b2472405/…`, every file 200, `immutable` |
| Every static import specifier in the entry resolves within the digest | 6 of 6 — `dom`, `format`, `changes`, `episode-trees`, `transport`, `feedback` |
| A digest the server does not hold | 404 with `Cache-Control: no-store`, not served from the current release |
| Non-allowlisted paths under the digest | `interface.html`, `../worker/core.py`, `settings.json` all 404 |
| Console and page errors across the whole session | none |
| All five sections render | Series, Media management, Settings, System, Help |
| Series list | 181 cards, no placeholder left behind |
| Opening a series card | renders; the busy overlay is **never** raised, which is the constraint on per-show reads |
| Run confirmation | states the actual plan — 36 series, 191 deletions, 211 GiB — and names Test Mode; cancelling sent nothing |
| Server log | no error, traceback or 500; healthy throughout |

The two layers extracted in phase 4 are both covered here: `transport.js` by every RPC the
page makes and by the overlay staying down, `feedback.js` by the Run confirmation.

## Not yet exercised

- **A live deletion.** Test Mode has never been turned off on this server, and no version of this — plugin or container — has ever removed a file. See
  [ACCEPTANCE.md](ACCEPTANCE.md).
- **A live monitoring write.** The selection has been exercised; Sonarr's `PUT` has not.
- **TMDB air-date filling** — no API key configured.
- **Unraid notifications** — no notification has been observed arriving. `announce_alerts`
  is now wired into the sweep, so the first appearance of a problem is what sends one; that
  path has unit coverage but has never been watched end to end on this server.
