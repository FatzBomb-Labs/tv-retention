# Planned changes

What is agreed but not yet built, in the order agreed: **cleanup → data → shell → the
rest**. The first step is done; everything below it is not.

This file exists so the plan survives a break in the conversation. When something here is
built, delete it from this file rather than ticking it — the file should always describe
what is *left*.

---

## Decided: it stays a plugin

Docker was considered seriously. It buys one thing the plugin cannot have: a process that
stays alive, and so a live socket to Sonarr. It does **not** remove scheduling — a
container still decides "run once a day", as a sleep loop rather than a cron line — and it
costs the Unraid notification path, the WebGUI's authentication, and about 35 MiB held
permanently against the plugin's zero at rest.

Measured on this server: peak 33.7 MiB for a tenth of a second, 55 ms per request, ~3
minutes of one core per day. The lightest container on the same box idles at 17.7 MiB.

The port would be small if that ever changes: `core.py` is pure, `store.py` is the only
filesystem coupling, and the Unraid-specific surface is three things — the notify script,
cron, and the `.page`.

## Decided: less scheduling, not more

Shows air at most once a day. The interface offers hourly, daily, weekly, monthly by date,
monthly by nth weekday, and a custom cron expression — six frequencies for a decision that
is realistically *daily at an hour, or off*.

Reduce the **interface** to: Off / Daily at ‹hour› / Weekly on ‹day› at ‹hour›. Keep
`schedules.py` underneath: it catches up a run missed while the server was off and holds
one until Sonarr answers, and neither of those is something a simpler surface should lose.

## Decided: stop chasing live

No SignalR, no held connections, no daemon. What exists is enough:

- the per-minute tick asks Sonarr what changed and re-reads only that
- a page open re-decides every plan locally every 15 s, at no network cost
- opening the page refreshes what it shows

If a series looks stale, the refresh control reads it again. That is the whole answer.

---

## Next: richer Sonarr data

Sonarr already sends these in the payload the plugin fetches and discards. Pull them into
the mapping — which means adding them to `SERIES_FIELDS`, and the cache key moves on its
own:

- poster and banner URLs
- season count, and episodes per season
- total episodes, and how many are on disk
- next airing and previous airing dates
- network, runtime, certification, overview, genres

Nothing uses them yet. They are what makes the grid view worth building, and gathering them
is independent of the interface work, so it can land first.

`nextAiring` is the one with a use beyond decoration: a series with nothing due for months
is a different proposition from one airing tonight, and the interface could say so.

## Then: the shell

A sidebar, replacing the tab strip, which is already straining at seven tabs with their own
filters inside them.

**Top bar** — one line. Icon, title, version on the left. On the right: **All scheduled
changes** (a dropdown of the individual changes, each opening its own view; no dropdown at
all when the count is zero), a refresh control, and a run button that is disabled when
there is nothing scheduled.

**Sidebar**

```
ALERTS  [count]
  All      [count]     (default when Alerts is opened)
  Errors   [count]
  Warnings [count]
  Notices  [count]

SERIES
  Add series           (Sonarr's series minus the ones already connected, each with Add)
  Connected series     (default when Series is opened)
  Presets

HISTORY
  Job history
  Logs

SETTINGS
  Connections          (Sonarr instances, TMDB key, anything else connection-shaped)
  Storage              (app storage folder)
  Logging              (runs kept, detail level)
  Notifications        (every Unraid notification, on or off)
  About
  Help                 (general usage; most items carry a ? tooltip)
```

## Then: series views

- grid view and list view, switchable
- a details pane that edits in place — no dialog for add or edit
- multiple selection, and mass edit across the selection

## Open questions

**Should every series require a preset?** Argued for: one source of truth, no per-series
values drifting apart. Argued against: it forces a preset into existence for every one-off,
and "Keep 30 days (Firefly only)" is a preset that lies about being shared. Current
proposal — keep Custom, make a preset the default choice, and put **New preset** on the Add
Series view so making one is never a detour. Not settled.

## Wanted, not yet designed

**Folder auditing.** Reporting library folders Sonarr does not know about. Note the
conflict before building it: *the plugin touches no filesystem at all* is a load-bearing
invariant — it is why there is no path mapping, why deletion is one API call, and why a
whole class of bug cannot occur. An audit means the plugin reads directories again. That is
allowed if it stays **read-only and reports**, never deletes, and never feeds a decision
that deletes; but it should be a deliberate exception with its own boundary, not a quiet
softening of the rule.

**Plex and Jellyfin interaction.** Wanted for a specific reason, still to be defined. Worth
knowing before designing: both expose watched state per user, which is the obvious thing a
retention tool would want and cannot get from Sonarr. Do not build until the reason is
written down here.
