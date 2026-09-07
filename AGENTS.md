# TV Delete

An Unraid Tools plugin that applies per-show retention to a TV library and deletes
through Sonarr. See [README.md](README.md) for architecture and usage.

## Validation

There is no Python or PHP in the Webtop development container. Run
`./tools/check-on-host.sh`, which stages the source under `/tmp` on FatzServer and runs
`python3 -m unittest discover -s tests`, `python3 tools/build.py`, and the PHP/JS lints
there. The suite is 196 tests with no expected failures.
[docs/VALIDATION.md](docs/VALIDATION.md) records the last validation.

## Project constraints

- A rule that does not resolve to exactly one Sonarr series must never be processed.
- Dry run means no file is ever removed, including by the whole-show deletion action.
- Media files Sonarr does not know about are reported, never deleted.
- Deletion goes through the Sonarr API so its database and monitoring stay correct;
  the filesystem is touched directly only for sidecars, empty folders, and the
  plugin-managed recycle folder.
- `worker/core.py` stays free of network access and deletions so the retention logic can
  be tested against fixtures.
- Dry run defaults to on, and every new install starts with no retention schedule. The
  health check is read-only and defaults to on.
- A cached reading is always shown with its age; nothing cached may be presented as live.
- Both caches store mapped objects. Changing what the Sonarr mapping produces means
  bumping CACHE_SCHEMA, or the new field reads as absent until the cache expires.
- Sonarr reads happen in the background, per show. They must never raise the busy overlay,
  and must never hold a show other than the one being read.
- Re-monitoring on a widened rule only ever touches episodes recorded in the plugin's own
  unmonitored ledger, never an episode the operator unmonitored by hand. The monitoring
  pills are a separate, live read of Sonarr and change nothing until asked.
- Test the Sonarr mapping from a Sonarr-shaped payload, not from the shape a consumer
  wants: a consumer reading a key the mapping never set is invisible to the latter.
- Test against isolated fixtures. Live checks against Sonarr must be read-only, run from
  a `/tmp` staging directory with `TVD_CONFIG` pointed away from `/boot`.

## Deployment

This is an uninstalled development release. `install/tv-delete.plg` is the self-contained
installer. Work through [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) on the target system
before turning dry run off.
