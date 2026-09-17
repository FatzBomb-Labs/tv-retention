# TV Retention

**Per-series retention for a TV library, driven by Sonarr alone.**

Set how much of each show to keep — a number of **days**, **episodes**, **seasons**, or any
combination — and the rest is removed **through Sonarr**, so its database stays correct and
what goes is unmonitored rather than downloaded again tonight.

For the shows that never end. A daily talk show accumulates for ever, and nobody wants the
2019 episodes; you want the last thirty, permanently. That is a standing policy rather than
a cleanup, which is why this is not driven by watch state and does not need a media server.

```yaml
services:
  tv-retention:
    image: ghcr.io/fatzserver/tv-retention:latest
    ports: ["8787:8787"]
    volumes: ["./config:/config"]
    environment:
      - TVR_USERNAME=admin
      - TVR_PASSWORD=EDIT-ME
```

One volume. **No media mount**, no path mapping, no `PUID` juggling over a library: this
never opens a library file. Sonarr owns the filesystem and deletion is an API call, so a
whole class of mistake cannot happen here.

### How it differs from the neighbours

Most tools in this space delete what **nobody watched** — Maintainerr, Janitorr, purgeomatic
and the rest — and most need Plex, Jellyfin, Emby or Tautulli to answer that question.
Watch state is the wrong signal for a daily show: nobody watches the archive.

Maintainerr in particular builds a Plex collection and acts on what falls into it. This
sets a retention policy on a series. Both are good; they are answering different questions.
If what you want is *"keep the newest two seasons of everything tagged Reality"*, and you
would rather not stand up a media server to say so, this is the one shaped for that.

### Status

Working and in use, but **young**: it has not yet been run in anger against a large library
by anyone but its author. Test Mode is on by default and a scheduled run reports exactly
what it would do while changing nothing. Read [Safety](#safety) before you turn it off.

---

## Contents

- [How it works](#how-it-works)
- [Installing](#installing)
- [Getting around](#getting-around)
- [First run](#first-run)
- [Rules](#rules)
- [Retention presets](#retention-presets)
- [Monitoring](#monitoring)
- [Queued changes](#queued-changes)
- [Finished shows](#finished-shows)
- [Staying current](#staying-current)
- [Alerts](#alerts)
- [Sonarr instances](#sonarr-instances)
- [Air dates and TMDB](#air-dates-and-tmdb)
- [Safety](#safety)
- [Scheduling](#scheduling)
- [Where things are stored](#where-things-are-stored)
- [Architecture](#architecture)
- [Development](#development)

---

## How it works

1. Every rule is bound to exactly one Sonarr series. A rule that does not resolve to a
   series is marked **not matched**, shown in red, and skipped by every run.
2. A run asks Sonarr for the series' episodes and files: season, episode number, air date,
   monitored state, import date and size.
3. Each retention condition votes on every episode. The rule's combine mode settles
   disagreements.
4. Selected files are deleted through Sonarr's API, and the episodes are unmonitored.
5. The plan and the outcome are written to a run journal and shown in the UI.

**It touches no filesystem at all.** Sonarr owns it: sizes, air dates, import dates and
monitoring arrive with the episodes, and deletion is a Sonarr call. There is no path mapping
to configure and no setting that duplicates something Sonarr already does — its recycle bin,
its extra-file handling and its empty-folder cleanup all apply as they are.

## Installing

`docker compose up -d --remove-orphans`, with the file above, then open
`http://<host>:8787`. The orphan cleanup is intentionally scoped to this Compose project;
do not use a global Docker prune.

Requirements: Docker, and at least one reachable Sonarr v3 or v4 instance. Nothing else —
no media server, no Tautulli, no library mount.

### General settings

| Variable | Default | |
|---|---|---|
| `TVR_USERNAME`, `TVR_PASSWORD` | — | Required. The password must be at least 8 characters. |
| `TVR_AUTH` | — | Set to `none` to run with no login at all. Only sane when nothing untrusted can reach the port — behind a VPN or Tailscale. |
| `TVR_PORT` | `8787` | |
| `PUID`, `PGID` | `1000` | Who owns `/config`. On Unraid use `99` and `100`. The container takes ownership on start, so no `chown` is asked of you. |
| `UMASK` | `022` | |
| `TVR_TZ` | `Etc/UTC` | Compose forwards this as the container's `TZ`; it decides when "daily at 4am" is. For example, use `America/New_York`. |

**It will not start without a login.** Set the two variables, or set `TVR_AUTH=none` on
purpose. The alternative — running with the interface locked away — would leave the half
that deletes running unsupervised while the half that would notice is unreachable, and the
realistic way to arrive there is a typo in a compose file six months from now.

Authentication guards the interface, never the work. The schedule runs whether or not
anybody is logged in, the same way Sonarr downloads with nobody watching.

## Getting around

The page is a sidebar and a content pane. **Series** is the library — one list of
everything Sonarr holds, filtered to **All**, **Connected** (has a retention rule) or
**Not connected**. **Series** also holds Presets and Exclusion Rules. **Settings** holds
General, Connections, Air dates and Schedule. **System** holds Status, Stats, Backup and
Logs, while **Help** holds the about page and guidance.

There is no separate "add" screen: a series with a rule and a series without are the same
row in the same list. Clicking either opens its details in the pane beside it; an
unconnected series shows **Add to Retention** before exposing the rule editor.

## First run

1. **Settings → Connections** → add each Sonarr with its URL and API key. Press
   **Test & save**: it reports the Sonarr version, how many series it holds, and whether
   Sonarr has a recycle bin configured.
2. **Series → Not connected**, click the show you want, then choose **Add to Retention**.
   Set its retention, then **Save and enable** — or **Save**, which adds the rule switched
   off until you say otherwise.
3. Open **Show scheduled changes** in the header. Read what the next run would delete, and
   why, before anything is scheduled.
4. When that looks right, set a schedule under **Settings → Schedule**. **Test
   Mode** is on for a new install, so the first scheduled runs report exactly what they
   would do and change nothing. Turn it off once you have watched one go through.

## Rules

The library reads as a list or as a wall of posters. It can be ordered by **needs
attention** (the default — problems first, so two broken shows among thirty healthy ones
are at the top), title either way, recently added to Sonarr, largest on disk, most
episodes, or shortest keep window. The title, the search box, the sort, both filters and
the layout switch are one row: they are all one question, and each of them used to answer
it from a line of its own.

**Hide ended** drops series that have finished *and* have no rule — one that has a rule
stays, and one with an alert always stays. **Alerts only** narrows to what needs
addressing.

In poster view the artwork is the card: alerts stack top-right, what the next run would do
runs down the left as a badge and a count, and the retention appears along the bottom on
hover. A rule that is switched off is a dashed frame and a dimmed poster.

A rule holds any combination of three conditions:

| Condition | Meaning |
|---|---|
| Age | Keep episodes that aired within this age. A bare number means days; `d`, `w`, `m`, and `y` suffixes are accepted. |
| Episodes | Keep this many newest episodes |
| Seasons | Keep this many newest seasons |

Each rule also carries its own **Season 0 / specials** choice: inherit the global setting,
include, or exclude. One show's specials are worth keeping and another's are not, so the
global setting is only a default.

Each condition votes to keep or delete each episode. **Keep** decides how those conditions
combine:

| Mode | An episode is deleted when | Effect |
|---|---|---|
| **Any** | every condition says delete | Keep it when any condition matches. The safe default. |
| **All** | any condition says delete | Keep it only when every condition matches. More aggressive. |

An unknown condition never authorizes deletion in either mode. For example, if age cannot
be determined, another condition cannot use that missing fact as permission to delete.

Example: `Age = 180`, `Episodes = 20`, Keep `Any` keeps everything from
the last 180 days **and** the 20 newest episodes, whichever is more generous.

An ended, disabled series can arm **Auto re-enable**. The next catalogue sync switches it
back on, once, if Sonarr marks the series continuing again, or if an episode newer than
anything it knew about has aired or been scheduled. A special counts only when that
series' effective **Include specials** setting is on.

### Choosing a show

Shows are added by picking a Sonarr series, and only that way. A rule binds to a series id,
and the folder follows from Sonarr — so there is nothing a folder-based rule could express
that this does not.

The library **is** the picker: search it, click the show, and the editor opens in the pane
beside the list. Each row shows either its folder or the reason it cannot be chosen:

| Row shows | Meaning |
|---|---|
| its folder path | Ready to use |
| *awaiting first episode — no folder yet* | Selectable. Sonarr creates the folder on first import and the rule picks it up then. |
| *no folder configured in Sonarr* | Not selectable. Sonarr has no path for the series. |

### The editor

The pane is four bands and only one of them scrolls. Fixed at the top: the pane's own bar,
carrying a refresh and a close; then the series — poster, title, year and network, the
episode counts, when the next episode airs, and what the next run would do to it. The
settings scroll between that and the buttons that commit them, so the plan those settings
move is never scrolled out of view while you move it.

The counts read *62 episodes (12 monitored)*, and the monitored figure is coloured by
whether the episodes **the rule keeps** are monitored — green all, orange some, red none.
Scope rather than the whole series, because a finished show with two seasons kept and six
older ones unmonitored is exactly right. Hovering gives the breakdown.

The next-run lines are answered against the values **in the form**, not the ones last
saved, so changing a keep window visibly moves the deletion and monitoring counts above it.
Each line hovers with its full wording and clicks through to the episodes.

A rule that already exists carries a switch at the top right, and it acts when it is
clicked rather than on **Update** — switching a series off is a thing you do, not a change
you save. A series being added has no switch: **Save and enable** and **Save** are the two
answers, and they are two buttons.

Unsaved edits survive a look at another series. Clicking a second poster to check something
and clicking back is browsing, not abandoning; leaving the library is leaving, and clears
them.

## Retention presets

Rather than typing the same numbers onto every show, create a named preset — *Keep 30
days*, *Keep 2 seasons* — under **Series → Presets**. A show's retention is then
a dropdown: **Custom** is first and is what a new series starts on, because sharing values
with other shows is a decision, not a default. A show using a preset shows it as a single
blue pill; only a custom rule spells its numbers out.

A preset is the single source of truth for every show pointing at it. Raise *Keep 30 days*
to 90 and all of them widen at once, with no rule-by-rule editing. A preset still in use
cannot be deleted.

## Monitoring

Sonarr's monitored flags decide what it will fetch. **A run only ever unmonitors**, and
that is not a setting — it is the whole of the behaviour:

- **Deleting a file always unmonitors it.** Anything else builds a fetch-and-delete loop:
  Sonarr re-grabs the episode tonight and the next run deletes it again, for ever.
- **Everything outside the window is unmonitored, including episodes with no file.** Those
  are never deleted, so nothing else would ever reach them, and Sonarr would go on fetching
  what the next run removes. That is the same loop by a side door.

Unmonitoring is protection: it only ever stops a download. Monitoring is intent, and it
costs downloads — on this library, 290 episodes against zero — so nothing decides it on a
schedule. It happens once, when you ask for it.

### Keeping an episode outside the keep window

**Add it to the exclusion list first.** An excluded episode sits outside everything a run
acts on, so its monitored flag is never touched — whatever you set in Sonarr afterwards
stays set, permanently. Without the exclusion, the next run unmonitors it.

The exclusion picker offers the monitored flag alongside each episode, in a second column,
so the whole decision is made in one place rather than half here and half in Sonarr. The
boxes are pre-filled from what Sonarr reports now — an episode you unmonitored by hand
reads that way — and only the ones you actually move are sent, so opening the picker and
closing it changes nothing.

### The one-time pass

Widening a rule does not start downloads. When you add a series, or save a rule with a
larger keep window, the editor offers **Monitor the episodes this brings into scope** —
once, for the episodes the widening added, and nothing else.

It stores the window *as it was*, not a list of episode ids, so an episode that arrives
between the save and the run is judged by where it actually falls.

## Queued changes

Nothing a series card offers happens immediately. Removals and the one-time monitoring pass
are queued and applied by the next run, so undo costs nothing until then. A run applies them
in order: queued removals, then monitoring, then retention deletion.

**Removing a series from TV Retention** asks what Sonarr should do about it:

| Action | Sonarr |
|---|---|
| Leave the series untouched | nothing |
| Set the entire series to monitored | monitors everything |
| Set the entire series to unmonitored | unmonitors everything |
| Set only episodes inside the keep window to monitored | monitors the window |
| Delete the series, keeping the files | deletes the series record |
| Delete the series and its files | deletes both |

The last two require typing `DELETE` or `DELETE ALL` before the button will arm. The plugin
never deletes a series itself — it asks Sonarr to, so Sonarr's recycle bin and bookkeeping
apply.

## Finished shows

When Sonarr reports a series as ended, the card says so, once, and the app records a notice
the first time. The rule stays on: there is still something inside its keep window, and the
notice says it will be switched off when there is not.

When nothing is left inside the window, **the rule is switched off automatically** and a
notice records it. No further episodes are coming and nothing remains to act on, so the
only thing left for it to do was be evaluated for ever. Nothing is deleted, the rule is
not removed, and switching it back on is one click — the notice offers to remove it if you
are done with the show.

A series whose only remaining files are *excluded* reaches the same point, and needs no
rule of its own to get there: exclusions are set aside before the keep window is worked
out, so a series holding nothing else has an empty window.

None of this happens in Test Mode. A run says what it would have switched off, and changes
nothing.

## Staying current

Reading a series from Sonarr is the expensive part, so the plugin does it only when Sonarr
is what changed. Everything else is arithmetic over episodes it already holds.

| When | What it costs |
|---|---|
| A rule edited, a preset raised, a day passing | **nothing** — the plan is re-decided from the stored episodes |
| Every 15 s while the page is open | one cache-only heartbeat, plus every plan re-decided locally |
| On page open, or return after five idle minutes | a quiet background refresh when the stored reading is old enough, plus fresh connection health |
| Hourly | Sonarr's series list and every managed series, coalesced with manual and page refreshes |

The managed-series refresh is not redundant. Sonarr's history reports imports and deletions, but
**monitoring toggled by hand in Sonarr is not a history event** and no cheap endpoint
reveals it, so a full read is the only thing that catches it.

Nothing cached is ever presented as live: the editor names the age of the reading behind it
(*Last refreshed: 40 min ago*), and everything above that line is current arithmetic over
that reading. While a sweep runs, each card's plan is replaced by *Reading from Sonarr…*
rather than left standing, because a stale plan shown as current is worse than no plan.

The refresh beside that line re-reads **this** series. On one with a rule that is a full
check; on one without, there is no rule to check, so it re-reads the series' own catalogue
entry — title, seasons, next airing, artwork — and writes it back into the stored list, so
the page is never left fresher than the cache behind it.

A run always reads Sonarr for itself. A stored reading never stands behind a write.

## Alerts

Problems with a **series** live in the library, because that is where they are fixed: a
badge on the card, and a roll-up above the list that filters to just those shows. Problems
with the **installation** — Sonarr unreachable, no recycle bin — live under **Settings →
Connections**. Neither counts the other's; contextual badges identify the area that needs
attention. **System → Status** also lists recurring warnings hidden for one specific Sonarr
instance, so they can be restored without changing any other connection.

**A series that is switched off raises nothing.** It is not being managed, so nothing about
it is a problem to report — no badge or roll-up line. Nothing is deleted:
switch it back on and every alert it had returns. That is not the same as muting, which is
a decision about a *kind* of alert across every series and leaves a blocking alert
blocking.

Issues are shown in the app and can be acknowledged where appropriate. Outbound
notifications and webhooks are not part of the container; legacy notification settings are
discarded during migration. Use **System → Status** for the operational overview.

Each problem is shown **once**, when it first appears, and again only if what it says
changes. A Sonarr that has been unreachable since Tuesday is not news again on Wednesday.

## Sonarr instances

Add each Sonarr under **Settings → Connections** with its URL and API key. **Test & save** confirms the version, counts the
series, and reports whether Sonarr has a recycle bin. An instance can be disabled without
deleting it, which leaves its rules in place and stops them being processed.

If Sonarr has **no recycle bin**, its deletions — including the ones this plugin asks for —
are permanent. That raises a warning with a one-click fix, because it applies to everything
Sonarr deletes, not only to TV Retention.

## Air dates and optional providers

Sonarr remains the primary source. When it leaves a date blank, the enabled providers under
**Settings → Air date resolution** are asked in the order shown: TMDB (when its key is set),
TVMaze, AniList, and a configured Plex or Jellyfin endpoint. Results are cached with the
episode reading. Missing dates can then be estimated from neighbouring episodes; when an
entire series has no dated episode, Sonarr history is the last estimate available.

Every plan and episode picker shows the date source (`sonarr`, a provider, `estimated`, or
`acquired`). If a keep-by-age rule still has undated files, the configured safety answer is
applied: exclude those files automatically, or disable the rule and raise a blocking alert.
No unresolved file is silently judged by its import date.

## API key lifecycle

**Settings → Connections** can create a key reserved for future API operations. The full
value is shown once and can be copied; the settings file stores only a hash, prefix and
creation/revocation metadata. Regenerate replaces the active key, and Revoke disables it.

## Backups and Status

**System → Backup** writes timestamped ZIP archives of TV Retention's `/config` data —
settings, state, journal and caches — to a separate absolute destination. The destination
is never included in itself, archives are atomic, and old archives are pruned to the
configured count. Backups contain credentials. Restore requires typing `RESTORE` and then
reloading the page; no Sonarr or media operation is performed.

**System → Status** reports build and uptime, Test Mode and schedule state, sync age,
pending/current runs, Sonarr reachability and recycle-bin state, storage health, API-key
state, and current warnings/errors. Its refresh is read-only.

## Safety

- **Mandatory Sonarr match** — a rule that does not resolve to exactly one series is
  skipped, with the reason shown.
- **Test Mode** — on for a new install, and it means **nothing writes**. Scheduled or
   manual, no exceptions: a run does everything except write and marks its output
   `[TEST MODE]`. To delete something, turn it off. The Run button says which of the two it
   is about to do.
- **Typed confirmation** — removing a series' files requires typing `DELETE ALL`.
- **Deleting always unmonitors** — not a setting, so no configuration can build a
  fetch-and-delete loop.
- **Specials** — season 0 is excluded unless you opt in.
- **Journal** — every run appends a full record to `journal.jsonl`, including the reason for
  each file.
- **Two-pass execution** — the plan is re-derived immediately before deleting, so a file
  that changed in between is judged on its current state.
- **One run at a time** — a scheduled run that collides with a manual one exits rather than
  queueing.

## Scheduling

**Settings → Schedule** offers hourly, daily, weekly, monthly by date, monthly by
weekday (*the first Monday*, *the last Friday*), or a custom five-field cron expression.
The resident worker decides what is due, catches up a missed run, and holds one pending run
until Sonarr answers. A page does not need to be open.

## Where things are stored

Everything is under `/config`, so a backup is a directory and a move is a copy.

### Coming from the Unraid plugin

This was an Unraid plugin until version 2026.09.09. Settings carry over exactly, API keys
included — copy the file in and the container upgrades it on first read:

```bash
mkdir -p ./config
cp /boot/config/plugins/tv-retention/settings.json ./config/settings.json
```

Then remove the plugin. The caches are rebuilt on the first sync and are not worth moving.

| Path | Contents |
|---|---|
| `/config/settings.json` | All settings, including API keys. |
| `/config/state/state.json` | Run history and the last run report. |
| `/config/state/journal.jsonl` | Append-only audit trail. |
| `/config/state/health.json` | Cached per-series results, alerts, and the change-feed cursor. |
| `/config/state/catalogue.json` | Sonarr's series list. |
| `/config/state/episodes/<rule>.json` | One rule's episodes, as last read. |
| `/config/state/jobs.json` | When each scheduled job last ran. |
| `/config/state/posters/` | Artwork borrowed from Sonarr, keyed so a changed poster is a new file. |
| `/config/state/tmdb-cache.json` | Cached TMDB air dates. |
| `/config/state/tvmaze-air-date-cache.json`, `/config/state/anilist-air-date-cache.json` | Cached optional-provider dates. |

Backups are written to the separate destination configured under **System → Backup** and
contain the settings, state, journal and these caches, including credentials.

The state folder can be moved under **System → Storage** if you would rather it sat
elsewhere; there is rarely a reason.

API keys are stored in `settings.json` and are never sent to the browser; the UI shows a
mask, and echoing the mask back means "keep the stored key".

## Architecture

```
src/
  include/interface.html Markup for the sidebar shell and its operational views
  assets/app.js          UI logic; holds no authority, re-validates nothing itself
  assets/app.css         Styling
  assets/icons.css       Ten icons, drawn here rather than borrowed
  worker/server.py       The page, its assets, the JSON API, the poster proxy, the login
  worker/core.py         Settings validation and the retention decision. Pure.
  worker/store.py        The filesystem: settings, caches, the log, the progress marker
  worker/sonarr.py       Sonarr v3 client and the rule/series matcher
  worker/tmdb.py         Optional TMDB air-date lookup with an on-disk cache
  worker/tvmaze.py      Credential-free TVMaze air-date lookup
  worker/anilist.py     Credential-free AniList air-date lookup
  worker/backup.py      Atomic config-volume backup and restore
  worker/alerts.py      What needs attention, and whether it blocks
  worker/schedules.py    When a job is due
  worker/migrate.py      Settings upgrades, v1 through v13
  worker/main.py         Sonarr orchestration, the run executor, the loop, the CLI
  worker/actions.py      The RPC surface, one function per thing the interface can ask for
```

**Standard library only.** No framework, no dependencies, nothing to audit beyond the
interpreter — which is also why the image is small and why `core.py` can be tested against
fixtures without any of the rest.

One process: the worker loop runs in a thread behind the HTTP server, so the schedule keeps
its own time whether or not the page is ever opened.

The browser never talks to Sonarr and never sees an API key. `server.py` checks the session
cookie and a per-session CSRF token, then hands the JSON payload to `actions.dispatch`. The
worker re-validates every field regardless of what the page sent, on load as well as on
save — a rule written before a field existed must still arrive with it.

`core.py` performs no network access and writes nothing, which is why re-deciding what a
rule would do costs nothing at all. `actions.py` imports `main`, never the other way round.

Both object caches are keyed by the *shape* of what Sonarr's mapping produces, not only by a
schema number: listing a field in `SERIES_FIELDS` or `EPISODE_FIELDS` is the cache bump, and
a test fails if the mapping produces a key the list does not name.

`app.css` carries explicit resets on `button`, `select` and `input` that look redundant now
and are kept deliberately. They exist because the interface used to be embedded in Unraid's
WebGUI, whose stylesheet reached every control through `:where()` selectors that contribute
**no specificity** — so any property this file did not name simply applied, silently. Four
rounds of spacing work went into finding that. The page is its own now, but a reset that
states what it wants is worth more than one that inherits and hopes.

## Development

```bash
python3 -m unittest discover -s tests -v   # 388 tests
docker build -t tv-retention .             # the image
./tools/check-on-host.sh                   # tests, build, PHP and JS lint on fatzserver-host
```

`tools/check-on-host.sh` stages the source under `/tmp` on a host with Python and runs the
suite there — a convenience for developing inside a container that has none. It builds no
image, reads no media, and contacts no Sonarr.

See [docs/VALIDATION.md](docs/VALIDATION.md) for what has been verified, and
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the checks to run on the target system before
trusting a live deletion.
