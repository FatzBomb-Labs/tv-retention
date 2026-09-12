# TV Retention

A container that applies per-show retention to a TV library and deletes through Sonarr.
See [README.md](README.md) for architecture and usage.

Much of the prose below still says "the plugin". It means the application: every
constraint held before the container port and holds after it, and rewriting the word
throughout would churn the file that records why they exist.

## Validation

There is no usable Python in the development environment. Run `./tools/check-on-host.sh`,
or `tools\check-on-host.ps1` from Windows, which stages the source under `/tmp` on
FatzServer, runs `python3 -m unittest discover -s tests`, imports every worker module, and
syntax-checks every shipped module. The suite is 507 Python tests plus 13 frontend runtime
tests, with no expected failures. Both entry points send the same remote script — the
PowerShell one reads it out of the shell script rather than restating it — and both honour
`TVR_HOST` for the ssh target, defaulting to `FatzServer`.

The Windows box cannot stand in for the host, and the two reasons are worth knowing so
neither gets "fixed" in the source: `worker/main.py` imports `fcntl` at module scope, so
every test reaching `server.py` errors out, and `core.normalise` calls `os.path.normpath`,
which rewrites `/tv/x` to `\tv\x` and fails a mapping assertion that is correct in the
container. Both are artifacts of the platform, not faults. What *does* run locally, and is
worth using for a fast inner loop before the real gate, is the frontend suite
(`node --experimental-vm-modules --test tests/frontend/*.test.js`) and the two pure
source-reading modules, with UTF-8 mode forced so `app.js` decodes:
`$env:PYTHONUTF8=1; python -m unittest discover -s tests -p test_build.py` (142 tests) and
the same for `test_migration.py` (33). Those cover most of what the interface refactor
touches; everything else waits for the host.

To see it actually running, build the image on the host and point it at a *copy* of the
settings with the schedule forced off. Never the original, and never a container that could
act: the read-only discipline for live checks applies to the container too.

## Layout

`worker/core.py` is pure: no network, no writes, no clock beyond what it is handed. It is
why re-deciding a rule costs nothing. `worker/store.py` is the filesystem and nothing else.
`worker/actions.py` is the RPC surface, one function per thing the interface can ask for,
and it imports `main` rather than the other way round. `worker/main.py` is what talks to
Sonarr and decides things, plus the loop and the CLI. `worker/server.py` is the front door
— the page, its assets, the JSON API, the poster proxy and the login — and it is the only
module that knows HTTP exists.

Standard library only, everywhere. A dependency would have to earn its place against the
fact that there is currently nothing to audit but the interpreter.
[docs/VALIDATION.md](docs/VALIDATION.md) records the last validation.

## Project constraints

- A rule that does not resolve to exactly one Sonarr series must never be processed.
- The plugin touches no filesystem at all. Sonarr owns it: sizes, air dates, import dates
  and monitoring arrive with the episodes, and deletion is a Sonarr call. There is no
  path mapping, and no setting that duplicates something Sonarr already does.
- Unmonitoring is protection, monitoring is intent. Unmonitoring only ever stops a
  download, so it happens in both modes — including episodes with no file, which are never
  deleted and so would otherwise never be reached. Monitoring can start hundreds of
  downloads, so it happens only under Full sync, or once when someone asks for it on a rule
  they just widened.
- There are two monitoring modes and there is no third. "Leave it to Sonarr" would let
  Sonarr re-fetch what a run just deleted, which is the fetch-and-delete loop the invariant
  below exists to prevent. A setting whose interface needs a danger label is a missing
  invariant.
- Deleting an episode file always unmonitors it. That is an invariant, not a setting:
  anything else builds a fetch-and-delete loop.
- Nothing destructive a series card offers happens immediately: removals queue, and only a
  run applies them, so undo is always available until then. The two one-time monitoring
  passes are the exception and are applied on save, because they only move Sonarr's
  monitored flags — reversible in a click, and deleting nothing. Queueing the unmonitor
  half in particular would be pointless: a run already unmonitors what is outside the
  window, so the downloads it prevents are the ones that would happen before the run.
- The plugin never deletes a series. It asks Sonarr to, so Sonarr's recycle bin and its
  bookkeeping apply. The plugin removes individual episode files, through Sonarr's API.
- Test Mode means **nothing writes** — scheduled or manual, no exceptions. It governed only
  the scheduler once, so a manual run deleted for real while the page said TEST MODE at the
  top of it, and a paragraph in a confirmation dialog was the only thing reconciling the
  two. To delete something, turn Test Mode off. A run's confirmation still states the
  actual plan rather than describing runs in general, and removing a series is guarded by a
  typed confirmation rather than by a mode.
- The Run button may only be hidden on a complete, current plan. A stale or partial
  reading must never be presented as "nothing to do".
- Media files Sonarr does not know about are reported, never deleted.
- An exclusion outranks every rule, including the series' own. It is decided before
  anything else and it is the one answer never weighed against another, which is what
  makes it worth having: a keep window is a policy, and an exclusion is an exception to
  every policy at once.
- One pass decides exclusions, and both readings come out of it. `excluded_causes` says
  *what* excluded each episode, `excluded_episodes` is that with the detail dropped, and
  `exclusion_summary` is that counted. Answering "is this excluded" and "what excluded it"
  in two places is how a pane names a pattern that caught nothing, or misses one that did.
- A series' exclusion list is stored by season and episode *number*, never by Sonarr's
  episode id: ids do not survive a series being removed and added back, and outliving
  ordinary events is the whole point of the list. An entry with no episode number is the
  whole season, including episodes that have not aired — which is the only thing ticking
  every episode individually cannot say.
- The per-series picker offers the hand-picked half and no more. What Automation excludes
  is global and is not stored per series, so a box that let one series untick a global rule
  would be an override with nowhere to live. Automatic exclusions are shown, explained,
  and disabled.
- `worker/core.py` stays free of network access and deletions so the retention logic can
  be tested against fixtures.
- Test Mode defaults to on, and every new install starts with no retention schedule.
- Specials are decided by `retention.include_specials` and its per-series override, and by
  nothing else. There is deliberately no automatic exclusion for season 0: it would be the
  same question asked worse, and two settings for one decision is how a rule ends up
  meaning different things depending on which page you last visited.
- A cached reading is always shown with its age; nothing cached may be presented as live.
- Settings are validated on load, not only on save: a rule written before a field existed
  must still arrive with it, or the interface has nowhere to put the value.
- Sonarr reads happen in the background, per show. They must never raise the busy overlay,
  and must never hold a show other than the one being read.
- A rule is unique by the series it binds to — `(instance_id, series_id)` — not by its
  folder. Keyed on the folder, a rule written before Sonarr moved the series and one added
  afterwards bound to the same series and were both processed. A rule that has never
  matched has no series id, so for that one the folder is still the only identity it has.
- The editor offers a one-time monitoring pass over whatever the keep window holds, not
  only over what a widening brought in: the window's contents are always worth showing, and
  whether they are worth changing is the operator's business. The window as it was still
  travels with the request, so "newly scoped" stays answerable later — episodes move, and a
  remembered list of ids does not. Monitoring is the only half offered, because it is the
  only half that is a choice: a run unmonitors everything outside the window regardless, so
  saving does it too rather than leaving Sonarr a day to fetch what that run would delete.
- Every action on the RPC surface is one the interface actually asks for, and a test says
  so. An action the page cannot reach is still reachable by anything that can post to the
  bridge, and the one that had gone unreachable read every bound series from Sonarr
  synchronously in a single request.
- Both object caches are keyed by the shape of what Sonarr's mapping produces, not only by
  a schema number. Listing a field in `SERIES_FIELDS` or `EPISODE_FIELDS` *is* the cache
  bump, and a test fails if the mapping produces a key the list does not name.
- Test the Sonarr mapping from a Sonarr-shaped payload, not from the shape a consumer
  wants: a consumer reading a key the mapping never set is invisible to the latter.
- A rule that is switched off raises no alerts. It is not being managed, so nothing about
  it is a problem to report — and switching it on brings every one back, because nothing
  was deleted. `alerts.managed_only` is the one filter, applied both where the interface
  reads alerts and where the health check decides what to notify about; hiding only the
  visible half would leave Unraid notifications firing about a series no run will touch.
- Test against isolated fixtures. Live checks against Sonarr must be read-only, run from
  a `/tmp` staging directory with `TVR_CONFIG` pointed away from `/boot`.
- A problem is notified once, when it first appears, and again only if what it says
  changes. `announce_alerts` decides that by key; the summary it replaced was re-sent by
  every sweep for as long as the problem stayed true, which teaches people to ignore the
  notification that matters.
- Assets ship under a release namespace: `/assets/<digest>/<file>`, the digest computed
  at startup over every allowlisted file's name and bytes, and served only from that
  in-memory snapshot. Only `.js`, `.css` and `.png` are public assets — anything else in
  the directory is neither digested nor served. A static import resolves within the same
  digest at every depth, so a module graph cannot straddle versions; a digest the server
  does not hold is refused non-cacheably rather than served from the current release.
- Every module but the entry is side-effect-free at import: definitions, no DOM, timers,
  storage or network. Imports evaluate before the entry body can guard anything, so
  `app.js` alone may look up `#tv-retention` while importing, and the purity test
  enforces that exception file by file.

## The WebGUI's cascade

Unraid's `default-base.css` styles bare `button`, `select` and `input` through
`:where(:not(.unapi *))` selectors. `:where()` contributes **no specificity**, so those
rules never *conflict* with anything in `app.css` — every property this plugin does not
name simply applies, and nothing about the cascade looks wrong while it happens. What
arrived that way, and is now reset explicitly in `#tv-retention button` and the field rule:
`margin: 10px 12px 10px 0`, `min-width: 86px`, `white-space: nowrap` on every button, and
`width: 100%` on every select — which in a flex row means "all of it". Assume any WebGUI
default not named in this file is in force. Tests guard the resets, because dropping one
produces no error, only air.

Assets are served from a release namespace — `/assets/<digest>/<file>` — one digest
computed at startup over every shipped file's name and bytes, hashed together. The
plugin's first cache key joined two per-file digests and truncated, which takes every
character from the first: the key followed the script and ignored the stylesheet, and
four consecutive CSS-only releases shipped under the key the browser already held. The
namespace also keeps a module graph on one version: the page's script tag carries the
digest, a static import resolves within it at every depth, a digest the server does not
hold is refused rather than served from the current release, and namespaced responses
are immutable.

## Planned work

[docs/PLAN.md](docs/PLAN.md) holds what is agreed and not yet built, and the reasoning
behind the decisions that shaped it. Delete from it as things land, so it always describes
what is left.

The frontend refactor is the exception, and it has a file of its own:
[docs/REFACTOR-HANDOFF.md](docs/REFACTOR-HANDOFF.md) is the plan of record for breaking
up `src/assets/app.js`, phase by phase. Look there, not in PLAN.md, for what the interface
split has landed and what it does next — including the contracts extraction must not
break, which is the part worth reading before touching any of it. It follows the same
delete-as-it-lands rule.

**It is a container.** The plugin is gone: no `.plg`, no `.page`, no PHP, no cron entry, no
Unraid paths. [docs/CONTAINER.md](docs/CONTAINER.md) records what the port was and
[docs/PLAN.md](docs/PLAN.md) records why, including what the survey of the neighbouring
tools found.

Licensed **GPL-3.0**, matching Sonarr.

Nothing host-specific goes back in. The one concession is `PUID`/`PGID`/`UMASK`, which are
container conventions rather than Unraid ones, and the container applies them to its own
volume rather than asking anybody to run `chown`.

## Deployment

`docker compose up -d --build --remove-orphans`, one `/config` volume, no media mounts. Work through
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) on the target system before turning Test Mode off.

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

`install/` and its `.plg` are gone. This section described them until after they were
deleted, three paragraphs below the note saying the plugin was scrapped — which is the
argument for deleting from a document rather than appending to it.

To watch it run, build the image on the host and point it at a **copy** of the settings
with the schedule forced off. Never the original, and never a container that could act.
