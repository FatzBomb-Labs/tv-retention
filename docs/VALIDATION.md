# Validation record

Last run: 2026-09-09, from the Webtop development container against FatzServer
(Unraid 7.3.2, Python 3.11.15, PHP 8).

## Automated

`./tools/check-on-host.sh` — source staged under `/tmp` on the host, removed afterwards.
It installs nothing, touches no `/boot` path, reads no media, and contacts no Sonarr.

| Check | Result |
|---|---|
| `python3 -m unittest discover -s tests` | 388 tests, all pass |
| `python3 tools/build.py` | package and manifest built, rebuild is byte-identical |
| `php -l src/tv-retention/include/api.php` | no syntax errors |
| `php -l` on the PHP section of `TVRetention.page` | no syntax errors |
| `node --check src/tv-retention/assets/app.js` | no syntax errors |
| Icon check | `fa-television` resolves to a real glyph |

### Coverage by area

| File | Tests | What it holds |
|---|---|---|
| `test_build.py` | 120 | The package, and the interface as a build artefact |
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

`test_build.py` is the largest because the interface is checked statically: it is the file
with no runtime under test, so the guards that would otherwise be a browser sit here.
Among them — every element the script hides exists in the markup, every id is unique,
braces and parentheses balance, and every WebGUI default this page has to undo is undone.

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

## Not yet exercised

- **A live deletion.** Test Mode has never been turned off on this server. See
  [ACCEPTANCE.md](ACCEPTANCE.md).
- **A live monitoring write.** The selection has been exercised; Sonarr's `PUT` has not.
- **The plugin installed through the WebGUI**, and the generated cron entry firing.
- **TMDB air-date filling** — no API key configured.
- **Unraid notifications** — no notification has been observed arriving. `announce_alerts`
  is now wired into the sweep, so the first appearance of a problem is what sends one; that
  path has unit coverage but has never been watched end to end on this server.
