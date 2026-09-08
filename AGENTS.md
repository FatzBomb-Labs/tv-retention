# TV Retention

An Unraid Tools plugin that applies per-show retention to a TV library and deletes
through Sonarr. See [README.md](README.md) for architecture and usage.

## Validation

There is no Python or PHP in the Webtop development container. Run
`./tools/check-on-host.sh`, which stages the source under `/tmp` on FatzServer and runs
`python3 -m unittest discover -s tests`, `python3 tools/build.py`, and the PHP/JS lints
there. The suite is 318 tests with no expected failures.

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
- Nothing destructive a series card offers happens immediately: removals queue, and only a
  run applies them, so undo is always available until then. The two one-time monitoring
  passes are the exception and are applied on save, because they only move Sonarr's
  monitored flags — reversible in a click, and deleting nothing. Queueing the unmonitor
  half in particular would be pointless: a run already unmonitors what is outside the
  window, so the downloads it prevents are the ones that would happen before the run.
- The plugin never deletes a series. It asks Sonarr to, so Sonarr's recycle bin and its
  bookkeeping apply. The plugin removes individual episode files, through Sonarr's API.
- Test Mode governs the scheduler only: a scheduled run does everything except write. A
  manual run is always live, so its confirmation must state the actual plan, and removing
  a series is guarded by a typed confirmation rather than by a mode.
- The Run button may only be hidden on a complete, current plan. A stale or partial
  reading must never be presented as "nothing to do".
- Media files Sonarr does not know about are reported, never deleted.
- `worker/core.py` stays free of network access and deletions so the retention logic can
  be tested against fixtures.
- Test Mode defaults to on, and every new install starts with no retention schedule.
- A cached reading is always shown with its age; nothing cached may be presented as live.
- Settings are validated on load, not only on save: a rule written before a field existed
  must still arrive with it, or the interface has nowhere to put the value.
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
  a `/tmp` staging directory with `TVR_CONFIG` pointed away from `/boot`.

## Planned work

[docs/PLAN.md](docs/PLAN.md) holds what is agreed and not yet built, and the reasoning
behind the decisions that shaped it. Delete from it as things land, so it always describes
what is left.

## Deployment

`install/tv-retention.plg` is the self-contained installer. Work through
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) on the target system before turning Test Mode off.

It is installed on FatzServer, so a rename is not free: the slug appears in
`/boot/config/plugins/<slug>/`, the state folder, `/usr/local/emhttp/plugins/<slug>/`, the
package name, and the marker in `/var/log/plugins/` that `update_cron` requires before it
will honour a plugin's cron file at all.

Development changes are deployed by copying `src/tv-retention/` over
`/usr/local/emhttp/plugins/tv-retention/`. That is enough to test, but only reinstalling
the `.plg` makes it survive — the package is what an Unraid upgrade restores from.
