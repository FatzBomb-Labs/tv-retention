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
