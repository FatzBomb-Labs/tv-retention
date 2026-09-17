# Release plan

This is the plan of record for the next product pass. It describes agreed work that is
not yet built. Remove an item when it lands; do not turn this into a history of discarded
ideas.

The target is the first release-quality version of the container: a focused retention
tool with a quiet, operational interface, useful status reporting, and no leftover
plugin-era surfaces.

## Current product shape

The sidebar is organised as follows:

- **Series** — All, Watching, Not watching, Presets, and Exclusion Rules.
- **Settings** — General, Connections, Air dates, and Schedule.
- **System** — Status, Stats, Backup, and Logs.
- **Help** — About and the existing guidance pages.

Notifications and Radarr Specials are removed. Settings owns optional connections, the
API-key lifecycle, air-date configuration, logging, and Schedule; System owns runtime
Status, Stats, Backup, and Logs. Alert reporting and safety enforcement remain available
in context, but neither has a standalone navigation page.

## Phase 1 — remaining workflow improvements

These items predate the current interface review and remain part of the agreed work.

### Series editor and schedule

- [ ] Consolidate the series editor's automation information into one box showing
      inheritance and per-series values clearly.
- [ ] Keep the one-time monitoring pass visually separate from standing automation policy.
- [ ] Keep exclusions expandable in their existing dialog/tree workflow.
- [ ] Simplify the schedule interface to Off, Daily, or Weekly while retaining the
      scheduler's catch-up and Sonarr-unreachable behavior.
- [ ] Add a New preset action from the series editor.
- [ ] Prefer concise tooltips for controls whose explanation does not need permanent page
      space.

### Optional auditing

- [ ] Before building folder auditing, define it as a read-only reporting boundary that
      never feeds a deletion decision. It must not weaken the no-media-filesystem rule.

## Phase 2 — release hardening

- [ ] Bump the semantic version from `0.3.0` to `1.0.0` when this plan is complete.
- [ ] Complete the live acceptance pass in `docs/ACCEPTANCE.md` before turning Test Mode
      off in a deployment.

## Explicitly out of scope

- Radarr or “TV Specials” movie management.
- Deleting or auditing media through the filesystem.
- Watched-state retention from Plex or Jellyfin unless a separate product decision gives
  it a concrete purpose.
- External notifications and webhooks.
- API operations beyond persistent key lifecycle until their contracts are designed.

## Delivery order

1. Remaining workflow improvements (staged runs, editor/schedule cleanup, optional auditing).
2. Documentation, migrations, validation, and the `1.0.0` release.
