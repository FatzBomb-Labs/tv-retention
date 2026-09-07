# TV Delete

An Unraid plugin that keeps TV libraries at a chosen size. For each show you set how much
to keep — a number of **days**, **episodes**, **seasons**, or any combination — and the
plugin removes the rest **through Sonarr**, so Sonarr's database stays correct and the
removed episodes are unmonitored instead of being re-downloaded.

It replaces the hand-maintained shell script this project grew out of: the folder list,
the retention periods, and the schedule all live in the WebGUI, and every deletion is
justified by real episode metadata rather than a file timestamp.

---

## Contents

- [How it works](#how-it-works)
- [Installing](#installing)
- [First run](#first-run)
- [Rules](#rules)
- [Retention presets](#retention-presets)
- [Monitoring status](#monitoring-status)
- [The health check and caching](#the-health-check-and-caching)
- [Re-monitoring when a rule is widened](#re-monitoring-when-a-rule-is-widened)
- [Sonarr instances and path mapping](#sonarr-instances-and-path-mapping)
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
2. A run asks Sonarr for the series' episodes and files: season, episode number, air
   date, monitored state, and the file path.
3. Each retention condition votes on every episode. The rule's combine mode settles
   disagreements.
4. Selected files are deleted through Sonarr's API. Sidecars are removed alongside,
   empty season folders are cleaned up, and the episodes are unmonitored.
5. The plan and the outcome are written to a run journal and shown in the UI.

Nothing is deleted while **Dry run** is on, and nothing is ever deleted from a folder
that Sonarr does not recognise.

## Installing

Unraid → **Plugins** → **Install Plugin**, and give it the path to
[`install/tv-delete.plg`](install/tv-delete.plg). The manifest carries the package
inside it, so that one file is the whole installer — no repository or internet access is
needed.

To install from a local copy, put the `.plg` somewhere on the server (for example
`/boot/config/plugins/`) and pass that path instead.

The plugin appears at **Tools → TV Delete**. Removing it leaves your settings, run
journals, and history in place, so reinstalling picks up where you left off.

Requirements: Unraid 7.0 or newer, and at least one reachable Sonarr v3/v4 instance.

## First run

1. **Sonarr instances** → add each Sonarr, with its URL, API key, and path mapping.
   Press **Test connection**: it reports the Sonarr version, how many series folders it
   found on this server, and whether Sonarr has its own recycle bin.
2. **Shows & rules** → **Add show**. Either pick the series from Sonarr, or pick a
   folder and let the plugin match it.
3. Press **Preview** on the rule. Read what it would delete and why.
4. When the previews look right, turn **Dry run** off in *Schedule & safety*, and set a
   schedule.

## Rules

A rule holds any combination of three conditions:

| Condition | Meaning |
|---|---|
| Keep days | Keep episodes that aired within this many days |
| Keep episodes | Keep this many newest episodes |
| Keep seasons | Keep this many newest seasons |

Each rule also carries its own **Season 0 / specials** choice: inherit the global setting,
include, or exclude. One show’s specials are worth keeping and another’s are not, so the
global setting is only a default.

Each condition votes to keep or delete each episode. The **combine mode** decides:

| Mode | An episode is deleted when | Effect |
|---|---|---|
| **Earliest** | every condition says delete | Keeps the most. The safe default. |
| **Latest** | every condition says delete, and none was undecidable | Keeps the least, without ever acting on an unknown. |
| **Any** | any condition says delete | Most aggressive. Ignores conditions it cannot judge. |

*Earliest* and *latest* differ only when a condition cannot be evaluated — an episode
with no air date, when the modification-time fallback is switched off. *Latest* keeps
such an episode; *any* deletes it on the strength of the other conditions.

Example: `keep_days = 180`, `keep_episodes = 20`, combine `earliest` keeps everything
from the last 180 days **and** the 20 newest episodes, whichever is more generous.

### Which series can be given a rule

A rule exists to delete files from a folder, so the picker only allows series that have
one on this server. The rest are listed with the reason, greyed out:

| Reason | Meaning |
|---|---|
| *no episodes imported yet, so Sonarr has not created its folder* | Normal. Sonarr creates a series folder on first import. |
| *Sonarr reports N file(s) but the folder is not on this server* | A path-mapping fault. Fix the mapping first. |
| *already used by another rule* | One rule per folder. |
| *no folder configured in Sonarr* | Sonarr itself has no path for the series. |

This governs choosing a series only. An existing rule whose folder later disappears is
never blocked: the run reports it and carries on.

## Retention presets

Rather than typing the same numbers onto every show, create a named preset — *Keep 30
days*, *Keep 2 seasons* — on the **Retention presets** tab. A show's retention is then a
dropdown: pick a preset, or pick **Custom** and set values for that show alone.

A preset is the single source of truth for every show pointing at it. Raise *Keep 30
days* to 90 and all of them widen at once, with no rule-by-rule editing. The preset
editor lists the shows that will change before you save, and a preset still in use cannot
be deleted.

## Monitoring status

Each matched show carries a pill beside its title showing how Sonarr’s monitored flags
compare with that show’s keep frame. It is read from Sonarr on demand — per show, or with
**Check all monitoring** — rather than on page load, so opening the tab stays instant on a
large library. Because it reads live, anything you monitor or unmonitor by hand in Sonarr
shows up straight away.

The pill is a menu. Clicking it opens the options that apply to that particular state, so
an aligned show is never offered a correction that would write nothing to Sonarr.

| Pill | Meaning |
|---|---|
| **In frame monitored, outside unmonitored** | The tidy state. Nothing to do. |
| **All episodes monitored** | Nothing has been unmonitored yet — the usual state before this plugin has run. |
| **Episodes outside the keep frame are still monitored** | Sonarr may re-fetch what the next run deletes. |
| **Episodes inside the keep frame are unmonitored** | Gaps inside the window will not fill. |
| **Monitoring does not match the keep frame** | Both of the above. |

Two corrections may appear in that menu, and neither is ever run automatically:

- **Monitor all within keep frame** — only when episodes inside it are unmonitored
- **Unmonitor all outside keep frame** — only when episodes outside it are monitored

**Show the episodes…** lists the exact episodes behind the pill, including ones Sonarr has no file
for — an episode’s monitored flag matters whether or not it is on disk. Episodes that
have not aired yet always count as inside the frame, so a corrective action never strips
monitoring from the next episode.

Setting this is optional. The plugin unmonitors what it deletes regardless; the pill
exists so the state a library is already in is visible and fixable in one click.

## The health check and caching

Everything the pills show is cached on disk, so opening the tab costs one small read
rather than a conversation with Sonarr. On this server a page load carries about 3 KB and
returns in 50 ms, against roughly 6 seconds to gather the same information live.

A scheduled **health check** keeps that cache honest. It is read-only — it never deletes
and never changes a monitored flag — and for every enabled rule it verifies:

1. the Sonarr instance answers and accepts the API key
2. the rule still matches exactly one series
3. the folder is present, or legitimately not created yet
4. monitoring, recomputed against the current keep frame

Problems raise **one** Unraid notification summarising them, never one per show, and each
affected show is flagged on its own card with a banner at the top of the page. A clean
result is silent unless you ask for it.

What is cached, and for how long:

| Cache | Default lifetime | Notes |
|---|---|---|
| Monitoring per show | 24 hours | Refreshed quietly in the background when the page finds it stale |
| Sonarr series list | 60 minutes | The most expensive call Sonarr offers — 12 MB and about two seconds here |

A cached reading always shows its age (*read 3 h ago*), because there is no cheap way to
learn that someone changed a monitored flag in Sonarr — nothing short of fetching the
episodes reveals it. So a cached number is a snapshot with a timestamp, never dressed up
as live. Anything that moves a keep frame — a rule edited, a preset widened, the specials
or mtime settings changed — invalidates the affected entries at once, without waiting for
the next check. A run that deletes also re-reads the shows it touched, since deleting
unmonitors.

Episode-level detail is deliberately not carried on page load. It is fetched for one show
when you open **Show the episodes…**.

## Re-monitoring when a rule is widened

When the plugin deletes an episode it unmonitors it in Sonarr, and records that in a
ledger. If the rule later widens — usually because a shared preset was raised — those
episodes may belong in the library again. With **Re-monitor episodes again when a rule is
widened** switched on, the next run puts them back on Sonarr's wanted list.

The decision replays the current rule over the episodes still on disk *plus* the ledger
entries, so "keep the newest 20 episodes" counts the missing ones in their proper place
rather than pretending they never existed.

Two limits are deliberate:

- Only episodes **this plugin** unmonitored are ever considered. Anything you unmonitored
  by hand was never written to the ledger, and is never touched.
- The option is **off by default**, because re-monitoring invites Sonarr to download the
  episodes again. A dry run reports exactly what would be re-monitored and changes
  nothing, not even the ledger.

## Sonarr instances and path mapping

Sonarr in a container reports the paths it sees inside that container, and they will not
match Unraid's. If Sonarr's `/tv` is Unraid's `/mnt/user/media/TV`, enter that pair as a
mapping; the plugin translates every path Sonarr reports before touching the filesystem,
and translates back when talking to Sonarr. Add a row per root — a Sonarr with `/tv` and
`/anime` gets two. Leave the mapping empty only if Sonarr runs with Unraid's own paths.

**Detect roots** fills this in for you: it asks Sonarr for its root folders, inspects the
running container that publishes the port in the instance URL, and pairs the two. On this
server that turns Sonarr's `/tv/Series`, `/tv/Kids`, `/tv/News & Talk` and `/tv/Reality`
into the single mapping `/tv` → `/mnt/user/media/TV`, and confirms the folder exists.

Getting this wrong is the most common cause of trouble, so **Test connection** checks the
series folders against the mapping. It only counts a folder as missing when Sonarr says it
holds files: Sonarr does not create a series folder until it first imports something, so a
series with no episodes legitimately has no folder and is reported separately rather than
as a mapping error. A run treats the same case as nothing to do, not as a failure.

Multiple instances are supported — one for series, one for anime, and so on. Each rule
belongs to one instance.

## Air dates and TMDB

Air dates come from Sonarr first. If Sonarr has no air date for an episode, and a TMDB
API key is configured, TMDB is asked (results are cached for 30 days). Failing both, the
file's modification time is used — and that fallback can be switched off, in which case
an episode with no known air date is never deleted.

Every preview shows the source of each date, so it is obvious when a decision rests on a
timestamp rather than a real air date.

## Safety

- **Dry run** — on by default. Runs report what they would delete and change nothing.
- **Mandatory Sonarr match** — an unmatched rule is skipped, with the reason shown.
- **Run limit** — a run is abandoned entirely if the plan exceeds *N* files. This is what
  catches a broken path mapping or a mistyped rule before it does damage.
- **Per-rule limit** — a rule is skipped if it would delete more than *X%* of its
  episodes.
- **Minimum file age** — files modified recently are never touched, protecting imports.
- **Specials** — season 0 is excluded unless you opt in.
- **Unknown files** — media Sonarr does not know about is reported, never deleted.
- **Recycle** — delete through Sonarr (honouring Sonarr's own recycle bin), or move
  files to a plugin-managed recycle folder that is emptied after a set number of days.
- **Journal** — every run appends a full record to `journal.jsonl`, including the reason
  for each file.
- **Two-pass execution** — the plan is re-derived immediately before deleting, so a file
  that changed in between is judged on its current state.

## Scheduling

*Schedule & safety* offers hourly, daily, weekly, monthly, or a custom five-field cron
expression. Enabling it writes `/boot/config/plugins/tv-delete/schedule.cron` and asks
Unraid to rebuild the crontab. The entry is republished when the array starts, and is
removed when the plugin is uninstalled.

Only one run happens at a time: a scheduled run that collides with a manual one exits
rather than queueing.

## Where things are stored

| Path | Contents |
|---|---|
| `/boot/config/plugins/tv-delete/settings.json` | All settings, including API keys. Survives reboots and reinstalls. |
| `/boot/config/plugins/tv-delete/schedule.cron` | The generated cron entry, present only while a schedule is enabled. |
| `<state folder>/state.json` | Run history and the last run report. |
| `<state folder>/journal.jsonl` | Append-only audit trail. |
| `<state folder>/tmdb-cache.json` | Cached TMDB air dates. |
| `<state folder>/unmonitored.json` | The ledger of episodes this plugin unmonitored, used for re-monitoring. |

The **app storage folder** is set in *Schedule & safety*, with a folder picker. On a fresh
install it defaults to a `tv-delete` folder inside the appdata share this server has
configured for Docker (`DOCKER_APP_CONFIG_PATH` in `docker.cfg`, `/mnt/user/appdata` here),
deliberately off the flash device. If the array is down it falls back to the flash config
folder.

API keys are stored in `settings.json` and are never sent to the browser; the UI shows a
mask, and echoing the mask back means "keep the stored key".

## Architecture

```
src/tv-delete/
  TVDelete.page          Unraid Tools page; loads the interface and the assets
  include/api.php        Authenticated bridge: CSRF check, then one JSON call to the worker
  include/interface.html Markup for the five tabs
  assets/app.js          UI logic; holds no authority, re-validates nothing itself
  assets/app.css         Styling, scoped to #tv-delete
  worker/core.py         Settings validation, retention evaluation, filesystem helpers
  worker/sonarr.py       Sonarr v3 client and the rule/series matcher
  worker/tmdb.py         Optional air-date lookup with an on-disk cache
  worker/main.py         RPC dispatch, the run executor, scheduling, notifications
  event/*                Array start/stop hooks
```

The browser never talks to Sonarr and never sees an API key. `api.php` accepts a POST
with a valid Unraid CSRF token, passes the JSON payload to `worker/main.py rpc` in a
fixed environment, and returns the reply. The worker re-validates every field regardless
of what the page sent.

`core.py` performs no network access and deletes nothing, so the retention logic is
tested directly against fixtures. Execution lives in `main.py`.

## Development

```bash
python3 -m unittest discover -s tests -v   # 140 tests
python3 tools/build.py                     # writes dist/ and install/tv-delete.plg
./tools/check-on-host.sh                   # tests, build, PHP and JS lint on FatzServer
```

`tools/check-on-host.sh` stages the source under `/tmp` on the Unraid host and runs the
suite there. It does not install the plugin, touch `/boot`, read the media library, or
contact Sonarr.

See [docs/VALIDATION.md](docs/VALIDATION.md) for what has been verified, and
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the checks to run on the target system
before trusting a live deletion.
