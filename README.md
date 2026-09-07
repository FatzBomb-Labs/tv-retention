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

## Sonarr instances and path mapping

Sonarr in a container reports its own paths. If Sonarr's `/tv` is Unraid's
`/mnt/user/media/TV`, enter that pair as the mapping; the plugin translates every path
Sonarr reports before touching the filesystem, and translates back when talking to
Sonarr. Leave the mapping empty only if Sonarr runs with Unraid's own paths.

Getting this wrong is the most common cause of trouble, so **Test connection** counts how
many of Sonarr's series folders actually exist on this server and lists the ones that do
not.

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

The state folder defaults to `/mnt/user/appdata/tv-delete`, deliberately off the flash
device. If the array is down it falls back to the flash config folder.

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
python3 -m unittest discover -s tests -v   # 67 tests
python3 tools/build.py                     # writes dist/ and install/tv-delete.plg
./tools/check-on-host.sh                   # tests, build, PHP and JS lint on FatzServer
```

`tools/check-on-host.sh` stages the source under `/tmp` on the Unraid host and runs the
suite there. It does not install the plugin, touch `/boot`, read the media library, or
contact Sonarr.

See [docs/VALIDATION.md](docs/VALIDATION.md) for what has been verified, and
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the checks to run on the target system
before trusting a live deletion.
