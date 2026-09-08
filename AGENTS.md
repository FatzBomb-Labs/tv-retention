# TV Delete

An Unraid Tools plugin that applies per-show retention to a TV library and deletes
through Sonarr. See [README.md](README.md) for architecture and usage.

## Validation

There is no Python or PHP in the Webtop development container. Run
`./tools/check-on-host.sh`, which stages the source under `/tmp` on FatzServer and runs
`python3 -m unittest discover -s tests`, `python3 tools/build.py`, and the PHP/JS lints
there. The suite is 300 tests with no expected failures.

## Layout

`worker/core.py` is pure: no network, no writes, no clock beyond what it is handed. It is
why re-deciding a rule costs nothing. `worker/store.py` is the filesystem and nothing else.
`worker/actions.py` is the RPC surface, one function per thing the interface can ask for,
and it imports `main` rather than the other way round. `worker/main.py` is what talks to
Sonarr and decides things, plus the tick and the CLI.
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
- Nothing a series card offers happens immediately: removals and monitoring fixes queue,
  and only a run applies them. Undo is therefore always available until then.
- The plugin never deletes a series. It asks Sonarr to, so Sonarr's recycle bin and its
  bookkeeping apply. The plugin removes individual episode files, through Sonarr's API.
- Test Mode governs the scheduler only: a scheduled run does everything except write. A
  manual run is always live, so its confirmation must state the actual plan, and removing
  a series is guarded by a typed confirmation rather than by a mode.
- The Run button may only be hidden on a complete, current plan. A stale or partial
  reading must never be presented as "nothing to do".
- Media files Sonarr does not know about are reported, never deleted.
- Deletion goes through the Sonarr API so its database and monitoring stay correct;
  the filesystem is touched directly only for sidecars, empty folders, and the
  plugin-managed recycle folder.
- `worker/core.py` stays free of network access and deletions so the retention logic can
  be tested against fixtures.
- Dry run defaults to on, and every new install starts with no retention schedule. The
  health check is read-only and defaults to on.
- A cached reading is always shown with its age; nothing cached may be presented as live.
- Settings are validated on load, not only on save: a rule written before a field existed
  must still arrive with it, or the interface has nowhere to put the value.
- Both caches store mapped objects. Changing what the Sonarr mapping produces means
  bumping CACHE_SCHEMA, or the new field reads as absent until the cache expires.
- Sonarr reads happen in the background, per show. They must never raise the busy overlay,
  and must never hold a show other than the one being read.
- A widened rule offers a one-time pass over the episodes the widening brought into scope,
  and offers it only then. The window as it was travels with the request, so "newly scoped"
  stays answerable at run time: episodes move, and a remembered list of ids does not.
- Both object caches are keyed by the shape of what Sonarr's mapping produces, not only by
  a schema number. Listing a field in `SERIES_FIELDS` or `EPISODE_FIELDS` *is* the cache
  bump, and a test fails if the mapping produces a key the list does not name.
- Test the Sonarr mapping from a Sonarr-shaped payload, not from the shape a consumer
  wants: a consumer reading a key the mapping never set is invisible to the latter.
- Test against isolated fixtures. Live checks against Sonarr must be read-only, run from
  a `/tmp` staging directory with `TVD_CONFIG` pointed away from `/boot`.

## Deployment

This is an uninstalled development release. `install/tv-delete.plg` is the self-contained
installer. Work through [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) on the target system
before turning dry run off.
