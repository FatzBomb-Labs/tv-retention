# TV Retention

A container that applies per-show retention to a TV library and deletes through Sonarr.
[README.md](README.md) is the usage record; this file is the contributor contract. Older
prose elsewhere says "the plugin" — it means this application.

## Status

The contracts below are requirements, not proof the current code satisfies them. Known
gaps — the run executor losing operations, exclusions dropped on save, shared episode
files protected per-episode instead of per-file, immediate paths that bypass Test Mode —
are recorded with evidence in [docs/PLAN.md](docs/PLAN.md), which holds what is agreed
and not yet built; delete from it as things land. Until they land: no unattended
destructive operation, schedules off, and Test Mode alone is not a safety boundary for
immediate actions.

## Validation

Run `./tools/check-on-host.sh`, or `tools\check-on-host.ps1` from Windows. Both stage the
source under `/tmp` on the host (`TVR_HOST`, default `fatzserver-host`), run the full
unittest suite, import every worker module, and syntax-check every shipped module; the
PowerShell entry reads the remote script out of the shell script rather than restating it.
[docs/VALIDATION.md](docs/VALIDATION.md) records the latest runs — never quote a test
count from memory.

The Windows box cannot stand in for the host, and neither reason is a source fault to
fix: `worker/main.py` imports `fcntl` at module scope, and `core.normalise` relies on
POSIX `os.path.normpath` behaviour that Windows rewrites. What does run locally is a fast
inner loop, not the gate: the frontend suite
(`node --experimental-vm-modules --test tests/frontend/*.test.js`) and, with UTF-8 mode
forced (`$env:PYTHONUTF8=1`), the pure source-reading and filesystem-only modules —
`test_build.py`, `test_migration.py` and `test_store.py`, each via
`python -m unittest discover -s tests -p <module>`.

To see it running, build the image on the host and point it at a *copy* of the settings
with the schedule forced off. Never the original, and never a container that could act.

## Layout

`worker/core.py` holds the retention decisions and settings validation, free of network
access and deletions so they can be tested against fixtures; its `atomic_json` is the one
place it touches local files. `worker/store.py` is local persistence and nothing else.
`worker/actions.py` is the RPC surface, one function per thing the interface can ask for,
and it imports `main` rather than the other way round. `worker/main.py` talks to Sonarr,
decides things, and owns the loop and the CLI. `worker/server.py` is the front door —
page, assets, JSON API, poster proxy, login. The integration clients (`sonarr.py`,
`tmdb.py`, `tvmaze.py`, `anilist.py`) also know HTTP; `server.py` is not the only one.

Standard library only, everywhere. A dependency must earn its place against the fact that
there is currently little to audit but the interpreter — little, not nothing: a short
list does not remove the audit obligation.

## Project constraints

- A rule that does not resolve to exactly one Sonarr series is never processed. A rule is
  unique by the series it binds to — `(instance_id, series_id)` — not by its folder; a
  rule that has never matched has no series id, and for that one the folder is the only
  identity it has.
- Sonarr owns the media filesystem: sizes, air dates, import dates and monitoring arrive
  with the episodes, and deletion is a Sonarr call. No media mounts, no path mapping, and
  no setting that duplicates something Sonarr already does. The application never deletes
  a series — it asks Sonarr to, so Sonarr's recycle bin and bookkeeping apply — and
  removes individual episode files only through Sonarr's API.
- Ordinary retention unmonitors outside-window episodes, including those without files,
  and never monitors. Explicit queued-removal monitoring dispositions are separate operator
  intent. Deleting a file requires confirmed unmonitoring of all affected episodes first,
  preventing a fetch-and-delete loop.
- Destructive card actions queue and remain undoable until execution. One-time monitoring
  passes apply on save: the optional pass covers the whole keep window, not only a widened
  subset; saving also unmonitors outside it. The previous window travels with the request
  so "newly scoped" remains answerable. These external writes require the same mode guard.
- Test Mode must prevent external mutations through every entry point, scheduled or manual.
  Settings remain editable; PLAN owns the precise allowed-local-write policy and the current
  bypass fixes. Default Test Mode on and retention schedules off. Run confirmations state
  the actual plan; series removal requires typed confirmation, not merely a mode change.
- Exclusions outrank every rule and are set aside before computing the keep frame. Protect
  excluded episodes' monitored state from automation and every file containing excluded
  content. The picker offers explicit monitored-flag controls alongside manual exclusions.
- One pass decides exclusions, and both readings come out of it. `excluded_causes` says
  *what* excluded each episode, `excluded_episodes` is that with the detail dropped, and
  `exclusion_summary` is that counted. Answering "is this excluded" and "what excluded
  it" in two places is how a pane names a pattern that caught nothing, or misses one that
  did.
- A series' exclusion list is stored by season and episode *number*, never by Sonarr's
  episode id: ids do not survive a series being removed and added back. An entry with no
  episode number is the whole season, including episodes that have not aired — the only
  thing ticking every episode individually cannot say.
- The per-series picker offers the hand-picked half and no more. What Automation excludes
  is global and is not stored per series, so a box that let one series untick a global
  rule would be an override with nowhere to live. Automatic exclusions are shown,
  explained, and disabled.
- Specials are decided by `retention.include_specials` and its per-series override, and
  by nothing else. There is deliberately no automatic exclusion for season 0: it would be
  the same question asked worse, and two settings for one decision is how a rule ends up
  meaning different things depending on which page you last visited.
- A cached reading is always shown with its age; nothing cached may be presented as live.
  The Run button may only be hidden on a complete, current plan — a stale or partial
  reading must never be presented as "nothing to do".
- Settings are validated on load, not only on save: a rule written before a field existed
  must still arrive with it, or the interface has nowhere to put the value.
- Sonarr reads happen in the background, per show. They must never raise the busy
  overlay, and must never hold a show other than the one being read.
- Every action on the RPC surface is one the interface actually asks for, and a test says
  so. An action the page cannot reach is still reachable by anything that can post to the
  bridge, and the one that had gone unreachable read every bound series from Sonarr
  synchronously in a single request.
- Both object caches are keyed by the shape of what Sonarr's mapping produces, not only
  by a schema number. Listing a field in `SERIES_FIELDS` or `EPISODE_FIELDS` *is* the
  cache bump, and a test fails if the mapping produces a key the list does not name.
- Test the Sonarr mapping from a Sonarr-shaped payload, not from the shape a consumer
  wants: a consumer reading a key the mapping never set is invisible to the latter.
- A rule that is switched off raises no alerts. Apply the single `alerts.managed_only`
  filter consistently to interface and health-check readings. `announce_alerts` announces
  a problem by key when it first appears and again only if its content changes, not on
  every sweep. Alerts stay in the application; no outbound notification/webhook surface.
- Test against isolated fixtures. Live Sonarr checks must be read-only, staged under
  `/tmp` with `TVR_CONFIG` pointed at copied settings away from `/boot`, schedules off and
  Test Mode on. Do not invoke saves, monitoring, recycle-bin changes, restore or Run in
  a read-only smoke; Test Mode alone does not authorize those actions.
- Assets ship under a release namespace: `/assets/<digest>/<file>`, one digest computed
  at startup over every allowlisted file's name and bytes, and served only from that
  in-memory snapshot. Only `.js`, `.css` and `.png` are public assets — anything else in
  the directory is neither digested nor served. A static import resolves within the same
  digest at every depth, so a module graph cannot straddle versions; a digest the server
  does not hold is refused non-cacheably rather than served from the current release.
- Every module but the entry is side-effect-free at import: definitions, no DOM, timers,
  storage or network. Imports evaluate before the entry body can guard anything, so
  `app.js` alone may look up `#tv-retention` while importing, and the purity test
  enforces that exception file by file.

## Frontend and documentation

The standalone container does not load Unraid's WebGUI CSS. Keep the tested button/field
resets in `app.css`: they deliberately prevent inherited margins, minimum widths, nowrap
and full-width selects from breaking compact layouts. The old cascade incident is not a
current host dependency.

[docs/REFACTOR-HANDOFF.md](docs/REFACTOR-HANDOFF.md) records the completed frontend split's
module map and architecture contracts; read it before changing the interface. It is not
an unfinished extraction plan. [docs/CONTAINER.md](docs/CONTAINER.md) records the completed
port. PLAN owns remaining work; retire items when they land with evidence in VALIDATION.

Licensed **GPL-3.0**, matching Sonarr. No host-specific integration returns: no plugin
installer, WebGUI page, PHP bridge or host cron. `PUID`/`PGID`/`UMASK` are portable container
conventions; the container prepares its own volume rather than requiring manual `chown`.

## Deployment

Compose runs the published image and has no build context; the `Dockerfile` builds the
image to publish. One required `/config` volume, no media mounts. Work through
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) and PLAN's prerequisite gates before any live write.

A replacement leaves exactly one running application container and no stopped predecessors.
Use `--rm` for preflight containers and remove them on both success and failure. Keep rollback
as one `tv-retention:rollback` image tag plus one matching config archive, never as a renamed
or stopped container. After the replacement is healthy, remove the predecessor container and
superseded TV Retention image tags and backups. Cleanup must be scoped to TV Retention; never
use a global Docker prune.

`VERSION` is the semantic release version and `BUILD` is the monotonically increasing
shipped-build number. Increment `BUILD` for every deployed code change; change `VERSION`
when the release meaning changes. The image creates `BUILD_DATE` at build time, and About
shows all three while the top banner stays on the semantic version alone.
