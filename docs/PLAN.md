# Planned changes

What is agreed and **not yet built**. Delete from this file as things land, so it always
describes what is left rather than what was ever discussed.

The interface rebuild is done: the sidebar shell, the top bar with its scheduled-change
menu, one library with All / Connected / Not-connected filters, the richer Sonarr fields
behind it, and the details pane that edits in place. So is the density pass over it. What
follows is what remains.

---

## Decided: it is published, as a container, and the plugin is retired

**Published.** Not because the market research came back encouraging — it came back
mixed — but because building for one server and retrofitting later is how projects get
rewritten. If nobody takes it, nothing is lost that was not already spent. If somebody
does, it was worth writing down.

**The plugin is scrapped rather than shipped first.** It borrows PHP and Python from the
host, so every Unraid release is a coin flip on a runtime this project does not control.
Polishing something with that dependency, on the way to a container anyway, is work with a
shelf life.

**GPL-3.0**, matching Sonarr and the neighbourhood. Note the asymmetry before changing your
mind: MIT to GPL is easy later, GPL to MIT needs every contributor's permission.

### The position, in one sentence

*Maintainerr builds Plex collections and acts on them. This sets a retention policy on a
series.*

That is the honest difference and it is fair to both tools. What the survey found:

- **Thirteen years of unmet demand.** [Sonarr #314](https://github.com/Sonarr/Sonarr/issues/314)
  has been open since August 2013 — 102 comments, ~30 distinct requesters, named use cases
  of daily shows, news, sports and kids' TV. A maintainer said in 2014 that they wanted to
  build it. They have not.
- **The naive workaround breaks Sonarr.** The thread is full of `find -mtime +5 -delete`,
  which deletes behind Sonarr's back and lets it re-download. That fetch-and-delete loop is
  what the monitoring invariants here exist to prevent, and it is the reason this project
  started.
- **The category is aimed elsewhere.** Maintainerr, Janitorr, purgeomatic, Reclaimarr,
  Deleterr, EmbyArrSync — almost all of them act on *watch state*, and most require a media
  server or Tautulli. Watch state is the wrong signal for a daily show: nobody watches the
  archive, you just want the last thirty, permanently.
- **Maintainerr can be bent to it, and is not shaped for it.** `seasonFileRank` and
  `episodeFileRank` arrived in v3.17.0 (2026-07-05), so the primitives exist. But a rule is
  scoped to a *Plex library*, "take action after days" is mandatory, half the form is
  collection presentation — Plex home, overlays, posters, sort titles — and at Media type
  "Shows" the Sonarr actions are whole-show: delete the entire show, or unmonitor and delete
  every episode. Preserving the newest two seasons means knowing to switch media type and
  knowing a rank property exists.
- **The gap is architectural, not marketing.** A marketing gap closes with a paragraph. A
  gap between "a collection things fall into" and "a policy attached to a series" does not.

**The risk, recorded honestly:** Maintainerr shipped fifteen releases in two months and is
moving toward these primitives. This is a niche, not a market. Neither is a reason not to
publish; both are reasons not to expect much.

## The port

**Superseding "it stays a plugin".** That decision was argued on memory, the Unraid
notification path and the WebGUI's authentication, and it was correct for a tool used by
one person on one server. Two things changed it.

**It is an application, not an OS or GUI extension.** Unraid's guidance draws that line by
what a thing *extends*, not by whether it has a web page. This one extends nothing: no
driver, no array behaviour, no system config, no WebGUI internals. Its subject is a media
library and another container's HTTP API. Strip the page away and what is left is a
scheduled job that talks to Sonarr over the network. It also runs as root, unsandboxed,
with authority to delete media — which is precisely the case the guidance protects against
once other people are running it.

**Both advantages only exist on Unraid.** If the audience is "people who run Sonarr", most
of them are not on Unraid, and native notifications reach none of them. A generic outbound
notification reaches everyone, Unraid users included. What read as a cost is a reason to
go.

**The live connection is the bonus, not the reason** — and it is available more cheaply
than it looks. What the interface lacks is not SignalR, it is a process that stays
alive: a resident loop polling the change feed every ten seconds is already
transformative next to a daily sync and a refresh button, and costs nothing new. SignalR
is an optimisation on top, and would be this project's first dependency outside the
standard library. Do not reach for it in v1.

**What the port is not.** Not a rewrite, and not a conversion. The boundary is already in
the right place — of 8,594 lines, the Unraid-specific surface is `api.php`, the `.page`,
two event scripts, five constants in `store.py`, and four functions in `main.py`. Build the
container alongside the working plugin; retire the plugin once the container is proven.
There are 3,022 series and real deletions pending on something that works, and that is not
a thing to hold hostage to a migration.

[CONTAINER.md](CONTAINER.md) holds the design, what genuinely has to be built, and the one
question — authentication — that is still open.

## Still to build

### Automation, in the order it is being built

The exclusion primitive is done, the Automation page that carries it is done, and the
series pane now reads out what applies to the series in front of you, with a picker behind
an **Edit** button. What is left:

1. **A staged run.** Read everything, decide everything, then write everything — rather
   than deciding and writing per series as it goes. A run that fails halfway currently
   leaves Sonarr in a state no single decision produced.
2. **The air-date invariant.** A series with unresolved air dates cannot be kept by age:
   refuse `keep_days` on it, checked when the editor opens and again on save, and say so
   rather than silently processing nothing. Then TVMaze and AniList as providers behind
   Sonarr and TMDB, each switchable.

   **Build it synchronously.** Sonarr fills `airDateUtc` from TVDB for anything with a
   broadcast slot, so on nearly every series the check is a scan over episodes already in
   the cache: no network, no wait, and nothing on screen. That removes the reason for a
   background reconcile queue, a progress state, or any asynchronous shape at all. Only a
   series with an actual gap reaches a provider, and only then does anyone see anything.

   The one population that genuinely lacks dates is **season 0** — specials are often
   undated in TVDB — and specials are excluded by default. So the realistic trigger is
   somebody who has deliberately switched them on, which is a good place for the invariant
   to speak up rather than a nuisance.
3. **Persistence, with an intent ledger** — what was decided, what was written, and what
   is still owed, so a run interrupted mid-write can be finished rather than repeated.
   Nothing to do with `automation.persistence` below, which is a settings group being
   deleted; this is durability for a run in flight.

## Series end handling: what is left

The audit, the Full sync removal, settings v12, the auto-disable, the surviving notice, the
re-enable watermark and the exclusion picker's monitoring column have all landed. What
remains of this line of work:

### The series details pane: one Automation box

Today the pane says automation in two places — a read-only Automation box whose single
**Edit** button opens only exclusions, and a separate select for specials below it.

Replace with one box listing every automation as a line, each line clickable to edit, each
line showing whether the value is inherited or set on this series. The data for that
distinction already exists — `action_automation` returns `specials_default` — it is simply
not drawn as a difference.

What is left to list, after the deletions: the one-time monitoring pass, specials,
exclusions, and the ended-series state.

Two constraints:

- **The one-time pass is not the same kind of thing as the rest.** The others are standing
  policy; that one is an action taken on save, shown only when the window has moved. Inside
  a list of settings it reads as a setting that vanishes. Keep it visually distinct.
- **Expand inline rather than opening dialogs.** The pane already expands the monitor tree
  in place. Exclusions stay a dialog, because the tree needs the room.

In the exclusions dialog, specials come first, and the tree shows season 0 only when
specials are not automatically excluded — the picker offers the hand-picked half and no
more.

Ask on close only when something would actually change. "Nothing to monitor" as a dialog
is a dialog that teaches people to dismiss dialogs. Unmonitoring outside the window stays
a *statement* rather than a question, as it is today: a run does it regardless, so offering
the choice only decides whether it happens now or within a day.

### Decisions from that work worth not relitigating

- **Our own writes are never drift.** There is no Sonarr change feed: drift would be found
  by comparing a fresh read to the stored episode cache, and a run re-reads with
  `force=True` after it writes. By the next comparison the cache already holds what the run
  did. Sonarr is never asked who moved a flag, and does not say.
- **Persistence settings were deleted rather than wired.** Sync runs once a day, so
  detecting "somebody monitored this outside the window" only works when the intent happens
  to straddle a sync boundary — monitor it, get the file, unmonitor it is one evening.
  The exclusion guarantee covers the same ground without a race or a setting.
- **There is no per-series override mechanism to build.** It was a prerequisite when eight
  automation settings needed one. After the deletions only `include_specials` remains, and
  it already has one.

## Elsewhere

**A smaller schedule.** Shows air at most once a day, and the interface offers hourly,
daily, weekly, monthly by date, monthly by nth weekday, and a custom cron expression — six
frequencies for a decision that is realistically *daily at an hour, or off*. Reduce the
**interface** to Off / Daily at ‹hour› / Weekly on ‹day› at ‹hour›, and keep `schedules.py`
underneath: it catches up a run missed while the server was off and holds one until Sonarr
answers, and neither of those is something a simpler surface should lose.

**Tooltips.** Most settings should carry an explanation on hover rather than in small type
under the control. The library's row is the pattern and the whole of the technique: *Hide
ended* carries its entire rule in a `title`, and the switch reads as two words. A control
does not need to explain itself to be understood, only to be trusted, and hovering is where
trust is cheap.

The `hint()` helper written for this was deleted rather than kept — it rendered a `?` that
opened a `window.alert`, which is a worse answer than the attribute the browser already
has, and it had sat uncalled since it was written.

## Open questions

**Settled:** every series does *not* require a preset. Custom is the first option and what
a new series starts on. A preset is a decision to share values with other series, and
adding one is usually not that — defaulting to the first preset made that decision quietly,
with the values coming from somewhere the form never mentioned.

Still worth doing: **New preset** from inside the series editor, so promoting one series'
values into a shared preset is not a detour through another section.

## Settled by investigation, not yet built

**Telling Plex and Jellyfin what was deleted: don't.** Investigated at length and the
answer moved twice, so the reasoning matters more than the conclusion.

Sonarr can already notify both, on an `onEpisodeFileDelete` trigger that is off by default
— but its Plex connection triggers a **full library scan**, not a path-scoped one.
[Sonarr #6141](https://github.com/Sonarr/Sonarr/issues/6141) asks for the partial version
and is still open, and that gap is the entire reason Autoscan exists. So "just tick the
box" is advice that breaks a deliberate arrangement for anyone who turned it off on
purpose.

But Plex and Jellyfin now do partial updates from their own file watchers, which makes
Autoscan largely redundant and makes notification unnecessary for most people. The
remaining gap is real but narrow: watchers can be switched off, and — the part nobody
accounts for — **inotify does not cross a network mount**, so on NFS, SMB or rclone the
watcher is enabled, looks correct, and never fires.

What that is worth: a **media-changed webhook**, carrying the series path and the deleted
episode paths, shaped like Sonarr's `EpisodeFileDelete` so Autoscan and Notifiarr take it
unmodified. Eighty lines, no new integration, generically useful. Not a Plex client and not
a Jellyfin client.

**Plex and Jellyfin as air-date sources: later, and for one reason only.** Not speed — a
TMDB lookup is one call keyed on an id Sonarr already gave us, while asking Plex means
finding the library, the series, the season and the episode, then matching on title. Not
independence either: their agents scrape TMDB and TVDB, so they are a cache of the sources
we would otherwise ask directly.

The one case that survives is **sidecar and plugin metadata**. Somebody with curated `.nfo`
files or a metadata plugin may hold a date no online database has. That is a real source,
and it is the only argument for building either client.

**TVDB is excluded outright.** Sonarr's primary metadata source *is* TVDB: if Sonarr has no
air date, TVDB has none, and asking again costs a paid v4 key to learn nothing.

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

---

## Decisions worth keeping

**No live connection — while it is a plugin.** No SignalR, no held sockets, no daemon.
Sonarr is read once a day, on demand from the button, and immediately before a run.
Everything the interface shows is answered from that reading, and its age is stated rather
than hidden. This was never a preference: a plugin has no process that stays alive, and
every mechanism above exists to work around that. See the decision below.

**A series that is switched off raises nothing.** Not a display filter — one filter,
`alerts.managed_only`, applied both where the interface reads alerts and where the health
check decides what to notify about. Hiding only the visible half would leave Unraid
notifications firing about a series no run will touch. Nothing is deleted; the facts stay
in the health cache and come back the moment it is switched on.

**Adding a series and switching it on are two decisions.** So they are two buttons —
*Save and enable* and *Save* — rather than a switch that has to be found first. A rule that
already exists keeps its switch, and that one acts when it is clicked rather than on
Update, because switching a series off is a thing you do and not a change you save.

**The panel is answered against the form, not the last save.** `scope-counts` re-decides
the saved rule with the editor's values in place and returns the plan, from the same stored
episodes it already counts against. Nothing touches Sonarr and nothing is written, so
changing a keep window moves the deletion and monitoring counts while you change it.
