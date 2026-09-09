# TV Retention

An Unraid plugin that keeps TV libraries at a chosen size. For each show you set how much
to keep — a number of **days**, **episodes**, **seasons**, or any combination — and the
plugin removes the rest **through Sonarr**, so Sonarr's database stays correct and the
removed episodes are unmonitored instead of being re-downloaded.

It replaces the hand-maintained shell script this project grew out of: the show list, the
retention periods, and the schedule all live in the WebGUI, and every deletion is justified
by real episode metadata rather than a file timestamp.

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
- [Alerts and notifications](#alerts-and-notifications)
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

**The plugin touches no filesystem at all.** Sonarr owns it: sizes, air dates, import dates
and monitoring arrive with the episodes, and deletion is a Sonarr call. There is no path
mapping to configure and no setting that duplicates something Sonarr already does — its
recycle bin, its extra-file handling and its empty-folder cleanup all apply as they are.

## Installing

Unraid → **Plugins** → **Install Plugin**, and give it the path to
[`install/tv-retention.plg`](install/tv-retention.plg). The manifest carries the package inside
it, so that one file is the whole installer — no repository or internet access is needed.

To install from a local copy, put the `.plg` somewhere on the server (for example
`/boot/config/plugins/`) and pass that path instead.

The plugin appears at **Tools → TV Retention**. Removing it leaves your settings, run journals
and history in place, so reinstalling picks up where you left off.

Requirements: Unraid 7.0 or newer, and at least one reachable Sonarr v3/v4 instance.

## Getting around

The page is a sidebar and a content pane. **Series** is the library — one list of
everything Sonarr holds, filtered to **All**, **Connected** (has a retention rule) or
**Not connected**. **Media management** holds connections, the schedule, presets and the
retention defaults. **System** holds storage, job history, logs, notifications, stats and
alert settings. **Help** holds the about page and the help text.

There is no separate "add" screen: a series with a rule and a series without are the same
row in the same list, and clicking either opens the same editor in the pane beside it.

## First run

1. **Media management → Connections** → add each Sonarr with its URL and API key. Press
   **Test & save**: it reports the Sonarr version, how many series it holds, and whether
   Sonarr has a recycle bin configured.
2. **Series → Not connected**, and click the show you want. Set its retention, then
   **Save and enable** — or **Save**, which adds the rule switched off until you say
   otherwise.
3. Open **Show scheduled changes** in the header. Read what the next run would delete, and
   why, before anything is scheduled.
4. When that looks right, set a schedule under **Media management → Schedule**. **Test
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
| Keep days | Keep episodes that aired within this many days |
| Keep episodes | Keep this many newest episodes |
| Keep seasons | Keep this many newest seasons |

Each rule also carries its own **Season 0 / specials** choice: inherit the global setting,
include, or exclude. One show's specials are worth keeping and another's are not, so the
global setting is only a default.

Each condition votes to keep or delete each episode. The **combine mode** decides:

| Mode | An episode is deleted when | Effect |
|---|---|---|
| **Earliest** | every condition says delete | Keeps the most. The safe default. |
| **Latest** | every condition says delete, and none was undecidable | Keeps the least, without ever acting on an unknown. |
| **Any** | any condition says delete | Most aggressive. Ignores conditions it cannot judge. |

*Earliest* and *latest* differ only when a condition cannot be evaluated — an episode with
no air date, when estimated dates are switched off. *Latest* keeps such an episode; *any*
deletes it on the strength of the other conditions.

Example: `keep_days = 180`, `keep_episodes = 20`, combine `earliest` keeps everything from
the last 180 days **and** the 20 newest episodes, whichever is more generous.

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
days*, *Keep 2 seasons* — under **Media management → Presets**. A show's retention is then
a dropdown: **Custom** is first and is what a new series starts on, because sharing values
with other shows is a decision, not a default. A show using a preset shows it as a single
blue pill; only a custom rule spells its numbers out.

A preset is the single source of truth for every show pointing at it. Raise *Keep 30 days*
to 90 and all of them widen at once, with no rule-by-rule editing. A preset still in use
cannot be deleted.

## Monitoring

Sonarr's monitored flags decide what it will fetch, so what this plugin does with them is
one setting with two values. It is global, and any series can override it.

| Mode | What it does |
|---|---|
| **Unmonitor only** (default) | Never monitors anything. Unmonitors what it deletes, and anything that falls outside the keep window. |
| **Full sync** | The keep window is authoritative in both directions: episodes inside it are set to monitored, including ones with no file. |

Two things are true in both modes, and neither is a setting:

- **Deleting a file always unmonitors it.** Anything else builds a fetch-and-delete loop:
  Sonarr re-grabs the episode tonight and the next run deletes it again, for ever.
- **Everything outside the window is unmonitored, including episodes with no file.** Those
  are never deleted, so nothing else would ever reach them, and Sonarr would go on fetching
  what the next run removes. That is the same loop by a side door.

There is deliberately no third mode that leaves Sonarr's flags alone. It would mean exactly
the loop above, and a setting whose interface needs a danger label is a missing invariant.

**Full sync can be expensive.** Monitoring an episode with no file asks Sonarr to download
it. On this library that is 290 episodes against zero, so the safe mode is the default and
an upgrade never moves anyone onto the other one.

### The one-time pass

Widening a rule does not start downloads. When you add a series, or save a rule with a
larger keep window, the editor offers **Monitor the episodes this brings into scope** —
once, for the episodes the widening added, and nothing else. It is not offered under Full
sync, where the same thing happens continuously.

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

When Sonarr reports a series as ended, the card says so, once, and an Unraid notification is
sent the first time. If nothing remains inside its keep window the rule has nothing further
to do, and its card offers to remove it.

## Staying current

Reading a series from Sonarr is the expensive part, so the plugin does it only when Sonarr
is what changed. Everything else is arithmetic over episodes it already holds.

| When | What it costs |
|---|---|
| A rule edited, a preset raised, a day passing | **nothing** — the plan is re-decided from the stored episodes |
| Every minute (the cron tick) | two small queries asking Sonarr what changed; only the series it names are re-read |
| Every 15 s while the page is open | the same question, plus every plan re-decided locally |
| Every six hours | Sonarr's series list, to notice series added |
| Daily | a full read of every series, as the backstop |

The daily sweep is not redundant. Sonarr's history reports imports and deletions, but
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

## Alerts and notifications

Problems with a **series** live in the library, because that is where they are fixed: a
badge on the card, and a roll-up above the list that filters to just those shows. Problems
with the **installation** — Sonarr unreachable, no recycle bin — live under **Media
management → Connections**. Neither counts the other's; the count in the header opens
both. **System → Alert settings** decides how loudly you are told, and nothing more.

**A series that is switched off raises nothing.** It is not being managed, so nothing about
it is a problem to report — no badge, no roll-up line, no notification. Nothing is deleted:
switch it back on and every alert it had returns. That is not the same as muting, which is
a decision about a *kind* of alert across every series and leaves a blocking alert
blocking.

Unraid notifications are for something structurally wrong: a series that cannot be found, a
Sonarr that will not answer, deletions with no recycle bin to catch them, a series Sonarr
has newly taken on, a series that has ended. **Never the retention itself.** Episodes being
scheduled for deletion and monitoring being brought into line are the job, not the news —
being told about them is what this plugin exists to avoid.

Each problem is announced **once**, when it first appears, and again only if what it says
changes. A Sonarr that has been unreachable since Tuesday is not news again on Wednesday,
and a notification that repeats is one people learn to ignore.

## Sonarr instances

Add each Sonarr with its URL and API key. **Test & save** confirms the version, counts the
series, and reports whether Sonarr has a recycle bin. An instance can be disabled without
deleting it, which leaves its rules in place and stops them being processed.

If Sonarr has **no recycle bin**, its deletions — including the ones this plugin asks for —
are permanent. That raises a warning with a one-click fix, because it applies to everything
Sonarr deletes, not only to TV Retention.

## Air dates and TMDB

An episode is dated by the first of these that answers:

1. Sonarr's own air date
2. TMDB, if an API key is configured (cached for 30 days)
3. an estimate interpolated from the episodes either side of it
4. the date Sonarr first acquired the episode, from its history
5. the file's import date

Estimated dates can be switched off, in which case an episode none of the first two can date
is never deleted. Every plan shows the source of each date, so it is obvious when a decision
rests on an estimate rather than a real air date.

## Safety

- **Mandatory Sonarr match** — a rule that does not resolve to exactly one series is
  skipped, with the reason shown.
- **Test Mode** — on for a new install. A scheduled run does everything except write, marks
  its output `[TEST MODE]`, and notifies as a real run would. It governs the scheduler only:
  a manual run is always live, and its confirmation says so.
- **Typed confirmation** — removing a series' files requires typing `DELETE ALL`.
- **Deleting always unmonitors** — not a setting, so no configuration can build a
  fetch-and-delete loop.
- **Specials** — season 0 is excluded unless you opt in.
- **Unknown files** — media Sonarr does not know about is reported, never deleted.
- **Journal** — every run appends a full record to `journal.jsonl`, including the reason for
  each file.
- **Two-pass execution** — the plan is re-derived immediately before deleting, so a file
  that changed in between is judged on its current state.
- **One run at a time** — a scheduled run that collides with a manual one exits rather than
  queueing.

## Scheduling

**Media management → Schedule** offers hourly, daily, weekly, monthly by date, monthly by weekday
(*the first Monday*, *the last Friday*), or a custom five-field cron expression.

The crontab holds one fixed entry that wakes the worker every minute; the worker decides
what is due. A generated crontab cannot express "the first Monday of the month", cannot
notice a run missed while the server was off, and cannot hold a job back until Sonarr
answers — all three of which this does. A run held back because Sonarr was unreachable goes
as soon as it answers, and exactly one is ever queued.

## Where things are stored

| Path | Contents |
|---|---|
| `/boot/config/plugins/tv-retention/settings.json` | All settings, including API keys. Survives reboots and reinstalls. |
| `/boot/config/plugins/tv-retention/schedule.cron` | The generated cron entry. |
| `<state folder>/state.json` | Run history and the last run report. |
| `<state folder>/journal.jsonl` | Append-only audit trail. |
| `<state folder>/health.json` | Cached per-series results, alerts, and the change-feed cursor. |
| `<state folder>/catalogue.json` | Sonarr's series list. |
| `<state folder>/episodes/<rule>.json` | One rule's episodes, as last read. |
| `<state folder>/jobs.json` | When each scheduled job last ran. |
| `<state folder>/tmdb-cache.json` | Cached TMDB air dates. |

The **app storage folder** is set under **System → Storage**. On a fresh install it defaults to a
`tv-retention` folder inside the appdata share this server has configured for Docker
(`DOCKER_APP_CONFIG_PATH` in `docker.cfg`, `/mnt/user/appdata` here), deliberately off the
flash device. If the array is down it falls back to the flash config folder.

API keys are stored in `settings.json` and are never sent to the browser; the UI shows a
mask, and echoing the mask back means "keep the stored key".

## Architecture

```
src/tv-retention/
  TVRetention.page          Unraid Tools page; loads the interface and the assets
  include/api.php        Authenticated bridge: CSRF check, then one JSON call to the worker
  include/interface.html Markup for the sidebar shell and its thirteen views
  assets/app.js          UI logic; holds no authority, re-validates nothing itself
  assets/app.css         Styling, scoped to #tv-retention
  worker/core.py         Settings validation and the retention decision. Pure.
  worker/store.py        The filesystem: settings, caches, the log, the progress marker
  worker/sonarr.py       Sonarr v3 client and the rule/series matcher
  worker/tmdb.py         Optional air-date lookup with an on-disk cache
  worker/alerts.py       What needs attention, and whether it blocks or notifies
  worker/schedules.py    When a job is due, including what cron cannot express
  worker/migrate.py      Settings upgrades, v1 through v7
  worker/main.py         Sonarr orchestration, the run executor, the tick, the CLI
  worker/actions.py      The RPC surface, one function per thing the interface can ask for
  event/*                Array start/stop hooks
```

The browser never talks to Sonarr and never sees an API key. `api.php` accepts a POST with a
valid Unraid CSRF token, passes the JSON payload to `worker/main.py rpc` in a fixed
environment, and returns the reply. The worker re-validates every field regardless of what
the page sent, on load as well as on save — a rule written before a field existed must still
arrive with it.

`core.py` performs no network access and writes nothing, which is why re-deciding what a
rule would do costs nothing at all. `actions.py` imports `main`, never the other way round.

Both object caches are keyed by the *shape* of what Sonarr's mapping produces, not only by a
schema number: listing a field in `SERIES_FIELDS` or `EPISODE_FIELDS` is the cache bump, and
a test fails if the mapping produces a key the list does not name.

`app.css` is scoped to `#tv-retention`, but scoping is not isolation. Unraid's own
stylesheet reaches every `button`, `select` and `input` on the page through
`:where(:not(.unapi *))` selectors, which contribute **no specificity** — so any property
this plugin does not name simply applies. Margins, minimum widths and `width: 100%` on
selects all arrived that way and are reset explicitly, with tests to keep them reset,
because losing one produces no error.

## Development

```bash
python3 -m unittest discover -s tests -v   # 388 tests
python3 tools/build.py                     # writes dist/ and install/tv-retention.plg
./tools/check-on-host.sh                   # tests, build, PHP and JS lint on FatzServer
```

`tools/check-on-host.sh` stages the source under `/tmp` on the Unraid host and runs the
suite there. It does not install the plugin, touch `/boot`, read the media library, or
contact Sonarr.

See [docs/VALIDATION.md](docs/VALIDATION.md) for what has been verified, and
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the checks to run on the target system before
trusting a live deletion.
