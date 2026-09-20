# TV Retention

**Per-series retention for a TV library, using Sonarr rather than watch state.**

Choose how many **days**, **episodes** or **seasons** of each show to keep. Sonarr supplies
metadata and handles media operations through its API; no media server, media mount or
path mapping is required. TV Retention does write its own settings, state and caches.

A shared multi-episode file expires with its latest episode under the selected retention
conditions. An exclusion on any member protects the whole file. It is deleted and counted
once, only after its monitored episodes have been successfully unmonitored.

## Status and safety

**Not yet approved for unattended destructive use. Keep retention schedules off and Test
Mode on.** Test Mode blocks external Sonarr mutations on every path, manual or scheduled,
including the immediate ones: the monitoring pickers, the scope pass on save and the
recycle-bin fix. Settings stay editable, and local cache/log writes still occur.

What is still open is tracked in the [plan](docs/PLAN.md): cross-tab and late-response
save conflicts, and container-level acceptance (non-root startup, permissions, graceful
shutdown, readiness). Until those are done, treat this as a supervised tool.

Do not treat a preview, a green Status page, a typed confirmation or a recycle bin as
proof of safe execution. There is no deletion-percentage guard. Deletions are not
generally reversible; Sonarr's recycle-bin configuration and actual behavior determine
recovery. Start with a disposable configuration and Sonarr instance.

## Contents

- [Installing](#installing)
- [Getting around](#getting-around)
- [Rules and exclusions](#rules)
- [Retention presets](#retention-presets)
- [Monitoring](#monitoring)
- [Queued changes](#queued-changes)
- [Finished shows](#finished-shows)
- [Freshness and alerts](#staying-current)
- [Sonarr instances](#sonarr-instances)
- [Air dates and optional providers](#air-dates-and-optional-providers)
- [API key lifecycle](#api-key-lifecycle)
- [Backups and Status](#backups-and-status)
- [Scheduling](#scheduling)
- [Storage](#where-things-are-stored)
- [Architecture and development](#architecture)

## Installing

Use [docker-compose.yml](docker-compose.yml) for the published-image configuration, or
[Dockerfile](Dockerfile) for a local image build. Image publication and release acceptance
still need evidence; the example's `latest` tag is not a readiness guarantee. Start with a
disposable configuration and Sonarr instance, not a production library.

The Compose example exposes `http://<host>:8787` and mounts `./config` at `/config`, the
only required persistent volume. Replace the deliberately invalid `EDIT-ME` password
before starting. Keep the service on a trusted network; use HTTPS through a suitable
reverse proxy when needed, and do not expose it publicly by default.

Backups are optional and should normally use a separate persistent directory. Uncomment
`./backups:/backups` in the Compose file, make that host directory writable by the
container's `PUID:PGID` (commonly `99:100` on Unraid), and set **System -> Backup** to
`/backups`. The container does not recursively change ownership of this separate mount;
with an explicit Compose `user:`, use matching host ownership yourself. `/config` remains
the only required volume.

Requirements: a container runtime and a reachable Sonarr v3 or v4 instance. No media mounts.

| Variable | Default | Purpose |
|---|---|---|
| `TVR_USERNAME`, `TVR_PASSWORD` | — | Login credentials; password must be at least 8 characters. |
| `TVR_AUTH` | — | Explicit `none` disables login; only for a separately protected, trusted network. |
| `TVR_PORT` | `8787` | HTTP listen port; port mapping and health-check compatibility must be checked if changed. |
| `PUID`, `PGID` | `1000` | Runtime identity and `/config` ownership on root-start; commonly `99`/`100` on Unraid. Explicit `user:` skips ownership adjustment and privilege drop. |
| `UMASK` | `022` | File-creation mask; non-root startup/permission validation remains in PLAN. |
| `TVR_TZ` | `Etc/UTC` | Compose variable forwarded as container `TZ`, e.g. `America/New_York`. |

Startup refuses missing credentials unless `TVR_AUTH=none` is explicit. Login controls
access to the interface, not the worker: schedules do not require an open browser or session.
New settings default to Test Mode on and no retention schedule; verify copied settings too.

## Getting around

- **Series:** All, **Watching** (has a retention rule), **Not watching**, Presets and
  Exclusion Rules. Watching is not media-server watch history or Sonarr's monitored flag.
- **Settings:** General, Connections, Air dates and Schedule.
- **System:** Status, Stats, Backup and Logs. Storage health is part of Status, not a
  separate Storage navigation item.
- **Help:** About and usage guidance. About shows semantic version, `BUILD` and image
  `BUILD_DATE`; the top banner shows the semantic version only.

In a disposable setup, add Sonarr under **Settings → Connections** with its URL and API
key. **Test & save** reports version, series count and recycle-bin configuration. Select a
show under **Not watching**, choose **Add to Retention**, then set retention. **Save** adds
a disabled rule; **Save and enable** adds an enabled one. Neither avoids save-driven Sonarr
monitoring mutations. The header's scheduled-changes menu exposes the proposed work;
reading it is not permission to enable scheduling.

## Rules

The library offers list/poster layouts, search, sorting, **Hide ended** and **Alerts only**.
Hide ended removes ended shows without a rule, but retains shows with alerts. List rows
show title/state, retention, episodes/storage, next airing, scheduled changes and alerts;
the editor keeps series facts and actions around a scrolling settings pane.

Rules bind to a Sonarr instance and series ID. A unique match is required for retention;
unmatched rules show a reason and are skipped. A configured series awaiting its first
import can be selected; a series with no Sonarr path cannot.

| Condition | Meaning |
|---|---|
| Age | Keep episodes within this age; bare numbers are days, with `d`, `w`, `m`, `y` suffixes accepted. |
| Episodes | Keep this many newest episodes. |
| Seasons | Keep this many newest seasons. |

| Keep mode | Decision when all conditions are known |
|---|---|
| **Any** (default) | Keep if any condition says keep; delete only if all say delete. |
| **All** | Keep only if all conditions say keep; any delete vote selects deletion. |

An unknown condition prevents deletion in either mode. For example, Age `180`, Episodes
`20`, Keep **Any** keeps both the last 180 days and the 20 newest episodes.

The editor's next-run counts use draft values. Monitoring colors describe the keep window
(green all monitored, orange some, red none), with a hover breakdown and episode details.
An existing rule's enable switch saves immediately, separately from **Update**. Retention
drafts are retained while browsing other shows and cleared when leaving the library.

### Exclusions and specials

**Series → Exclusion Rules** supplies global season, folder and episode-pattern exclusions.
The per-series picker adds manual exclusions by season/episode number, not Sonarr episode
ID. A season's own checkbox includes future episodes; individually ticking its current
episodes does not. Global exclusions are explained but cannot be unticked per series.
**Season 0 / specials** can inherit the global default (excluded), or be included/excluded
per series.

Exclusions outrank retention and leave excluded episodes' monitored flags alone. An
exclusion on any episode sharing a physical file protects that whole file. The picker also
exposes explicit monitoring edits and shades episodes in the keep window.

## Retention presets

Create named presets under **Series → Presets**. A new rule starts on **Custom**; selecting
a preset shares its retention values with every rule using it. Editing that preset changes
all those rules, and the editor lists affected shows. Presets in use cannot be deleted.
Cards show a preset name or custom values.

## Monitoring

Ordinary retention only unmonitors: episodes outside the keep window, including fileless
episodes, are unmonitored, and file deletion follows successful unmonitoring so Sonarr
cannot re-fetch what is about to be removed. Explicit removal dispositions that monitor
are separate operator intent.

The editor's **Change monitor status for episodes within scope** opens the current keep
window's episodes, checked according to Sonarr. It is not restricted to newly added scope.
Selected differences are sent **immediately on save**, not queued. Saving also requests an
outside-window unmonitor pass without an opt-in. The exclusion picker's changed monitored
flags are likewise applied on save. Monitoring can lead to downloads.

Those immediate paths respect Test Mode, which refuses the write before sending it. A
disabled rule or an unchecked inside-window toggle does not prevent the outside-window
save pass, so avoid such saves when browsing a real library read-only.

## Queued changes

Removal from TV Retention is queued for a run, with **Undo** available beforehand. It is
not an immediate deletion. The queued Sonarr disposition can be:

- Leave the series untouched.
- Monitor the entire series, unmonitor it, or monitor episodes inside the keep window.
- Delete the series record while keeping files — the UI requires `DELETE`.
- Delete the series and files — the UI requires `DELETE ALL`.

The confirmation uses those exact words, not the series title. The removal dialog also
has **Set monitoring in Sonarr before it goes**: those picker changes are immediate and
separate from the queued disposition, though still subject to Test Mode. Undoing a queue
does not undo those writes or a completed deletion.

## Finished shows

Ended shows are labeled and recorded in notices. The workflow can disable a rule once its
keep window is empty, including when only excluded files remain; disabling is not series
removal. A Test Mode run reports rather than applies that change. **Auto re-enable** on an
ended, disabled rule can re-arm it once when a sync finds the series continuing or a newer
episode; specials count only when included. Keep scheduling off during evaluation.

## Staying current

The open page has a cache-only heartbeat every 15 seconds; draft/policy changes can be
recomputed over stored episodes. Page-open/return refreshes and periodic catalogue/managed
series reads fetch Sonarr data in the background. Monitoring changes made directly in
Sonarr require an episode read; history alone does not reveal them.

The editor displays reading age and a per-series refresh. A show without a rule refreshes
its catalogue facts. Cached counts depend on that reading, and the Run button is only
hidden on a complete, current plan. Do not assume every write re-reads Sonarr: the
immediate scope pass uses cached episodes.

## Alerts

Series badges and a library roll-up identify show-level problems. **Settings → Connections**
holds installation problems such as unreachable Sonarr and missing recycle bins.
**System → Status** also exposes recurring warnings suppressed for individual instances.
Disabled-rule alerts are filtered by the managed-only setting. Acknowledged warnings return
if their fingerprint changes; blocking errors cannot be acknowledged away.

Outbound notifications/webhooks are not part of the container. Legacy notification settings
are discarded during migration; Status and Logs provide the operational view.

## Sonarr instances

Multiple instances can be configured under **Settings → Connections**. Disabling one keeps
its rules but stops ordinary processing. Stored Sonarr/provider credentials are masked in
settings responses; saving the unchanged mask retains the secret. They remain sensitive
plaintext in local settings and backups, unlike TV Retention's own hashed API credential.

Connections reports recycle-bin configuration and offers a fix that writes to Sonarr
immediately rather than queueing; Test Mode refuses it. Without a recycle bin, deletion is
permanent; even with one, verify recovery in a disposable environment.

## Air dates and optional providers

**Settings → Air dates** controls provider order and unresolved-date handling; optional
connection credentials/tests live under **Settings → Connections**. Sonarr remains
authoritative. TMDB (with a key) and TVMaze can supply dates. **AniList supplies none**:
its lookup cannot prove series, season and episode identity without matching on title
alone, so it is out of scope as a date source. **Plex/Jellyfin checks are
connectivity-only**; other listed choices are not working providers.

Date enrichment can use neighboring-episode estimates and, for a wholly undated series,
Sonarr-history fallback. Episode details expose provenance such as `sonarr`, provider,
`estimated` or `acquired`. Unresolved files can be excluded or cause the rule to be
disabled with a blocking alert. Provider connection success does not validate dates.

## API key lifecycle

**Settings → Connections** can create a key reserved for future API operations. Its full
value is returned once for display/copy; settings store a hash, prefix and lifecycle
metadata. **Regenerate** replaces the active key; **Revoke** disables it. This is not a
working public automation API. The one-time display stays until you dismiss it with
**Done** — a background refresh will not take it away before you have copied it.

## Backups and Status

**System → Backup** offers timestamped ZIPs of the active config directory, normally
`/config`, including settings, state, journal and caches. It requires a separate absolute
writable destination and has a retained-archive count. Use the optional `/backups` mount
above when archives must survive container replacement; an unmounted destination may be
ephemeral. The backup operation refuses configurations whose authoritative `state_dir` is
outside `/config`, rather than silently omitting application state.

Archives contain credentials. Creation is transaction-coordinated, uses atomic publication,
per-file hashes, owned-archive retention and bounded restore extraction. Stage a restore with
`RESTORE`, review quarantined queues/intents, then activate with `ACTIVATE`; activation holds
the run lock, forces Test Mode on and schedules off, and keeps a rollback journal through
installation. The legacy direct restore operation remains unavailable. Test backup/restore
only in isolation, not on a live configuration, until the acceptance checklist is complete.

**System → Status** reports build/date/uptime, mode and schedule state, sync age,
pending/current runs, Sonarr reachability/recycle-bin state, storage health, API-key state
and warnings/errors. Refresh is read-only with respect to Sonarr. HTTP liveness or a healthy
Status display does not establish worker readiness or safe execution.

## Scheduling

**Settings → Schedule** has a prominent Test Mode card and supports hourly, daily, weekly,
monthly by date or weekday, and custom five-field cron schedules. The worker runs without
an open page and implements missed-run catch-up and pending work while Sonarr is unavailable.
Scheduling uses validated IANA civil time, and the image installs `tzdata` so DST behaves.
Restart behavior still needs container acceptance. **Keep retention schedules off** until
the plan's gates are done, including in copied or restored settings. The mode toggle saves
immediately.

## Where things are stored

Default persistent paths:

| Path | Contents |
|---|---|
| `/config/settings.json` | Settings and provider credentials; hashed TV Retention API-key metadata. |
| `/config/state/state.json`, `journal.jsonl` | Run history/report and audit journal. |
| `/config/state/run-intent.json` | Execution/recovery intent; do not replay copied intent against live services. |
| `/config/state/health.json`, `catalogue.json` | Cached plans/alerts and Sonarr catalogue. |
| `/config/state/episodes/`, `posters/` | Episode readings and artwork. |
| `/config/state/jobs.json`, `tv-retention.log` | Scheduler bookkeeping and operational log. |
| `/config/state/tmdb-cache.json`, `tvmaze-air-date-cache.json`, `anilist-air-date-cache.json` | Provider caches. |

Runtime markers/locks use `/tmp/tv-retention` by default. Advanced overrides include
`TVR_CONFIG_DIR`, `TVR_CONFIG`, `TVR_RUNTIME` and saved `state_dir`; state outside the active
config directory is not covered by the current archive implementation. Keep the default
layout while storage/restore guarantees are being established.

For migration, preserve an untouched settings copy and validate the upgrade in an isolated
configuration with schedules disabled. The loader migrates supported settings and removes
retired notification fields; verify rules, presets, exclusions, connections and credentials
rather than relying on a fixed schema number. Do not copy old runtime intent into live use.

## Architecture

The runtime uses Python's standard library and browser JavaScript modules, without a
framework. That reduces third-party packages, **not** the need to audit application code,
the interpreter, base image, HTTP boundary or dependencies of the operating system.

| Area | Responsibility |
|---|---|
| `src/include/interface.html`, `src/assets/` | Sidebar shell and modular UI; `app.js` is the entry point. |
| `src/worker/server.py` | HTTP, login/session/CSRF handling, assets, poster proxy and JSON API. |
| `src/worker/actions.py`, `main.py` | RPC actions, Sonarr orchestration, executor, worker loop and CLI. |
| `src/worker/core.py` | Settings validation and fixture-testable retention decisions. |
| `src/worker/store.py`, `backup.py` | Config/state/cache persistence and archive operations. |
| `src/worker/sonarr.py`, `tmdb.py`, `tvmaze.py`, `anilist.py` | Sonarr mapping and optional date clients. |
| `src/worker/alerts.py`, `schedules.py`, `migrate.py` | Alerts, due-job calculation and settings upgrades. |

One image, one process: `server.py` runs a standard-library `ThreadingHTTPServer` over
`actions.dispatch`, with the worker and scheduler in a background thread, so there is no
framework, supervisor or second service. The worker runs independently of browser login.
Browsers use the application API, not Sonarr directly. Assets ship under a shared release
digest so static imports stay within one release, and mapping field shapes participate in
cache invalidation.

The frontend is nineteen modules with `app.js` as the composition root: `dom`, `format`,
`storage`, `transport`, `feedback`, `changes`, `episode-trees`, `activity`, `settings`,
`checks`, `series-removal`, `series-editor`, `alerts`, `navigation`, `topbar`, `presets`,
`connections` and `library`. Sibling modules do not import each other; the entry supplies
narrow, intent-named capabilities and reads replaceable documents through accessors such
as `getSettings` and `getSnapshot` rather than captured references. Every module but the
entry is side-effect-free at import.

Sessions are in memory, with `HttpOnly`/`SameSite=Strict` cookies and per-session CSRF
tokens. There is no proxy-header authentication mode, and forwarding headers are not
treated as authentication or TLS proof. Restrict reachability to a trusted network or a
controlled HTTPS reverse proxy. Settings are validated on load and on save; cross-tab
conflict rejection is still open work.

## Development

See [AGENTS.md](AGENTS.md) for conventions, constraints and validation.
`tools/check-on-host.sh` and its Windows counterpart `tools/check-on-host.ps1` stage source
under `/tmp` for Linux validation; the scripts do not build an image or contact Sonarr.
Windows cannot substitute for Linux-specific worker behavior. Frontend tests live under
`tests/frontend/`; backend fixtures and regressions live under `tests/`.

[docs/PLAN.md](docs/PLAN.md) owns the safety list, the remaining work and the acceptance
ladder. [docs/VALIDATION.md](docs/VALIDATION.md) records the current gate result.

Licensed GPL-3.0; see [LICENSE](LICENSE).
