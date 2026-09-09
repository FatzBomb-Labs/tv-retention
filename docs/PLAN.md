# Planned changes

What is agreed and **not yet built**. Delete from this file as things land, so it always
describes what is left rather than what was ever discussed.

The interface rebuild is done: the sidebar shell, the top bar with its scheduled-change
menu, one library with All / Connected / Not-connected filters, the richer Sonarr fields
behind it, and the details pane that edits in place. So is the density pass over it. What
follows is what remains.

---

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

**It stays a plugin.** Docker buys one thing the plugin cannot have: a process that stays
alive, and so a live socket to Sonarr. It does **not** remove scheduling — a container
still decides "run once a day", as a sleep loop rather than a cron line — and it costs the
Unraid notification path, the WebGUI's authentication, and about 35 MiB held permanently
against the plugin's zero at rest. Measured here: peak 33.7 MiB for a tenth of a second,
55 ms per request, ~3 minutes of one core per day; the lightest container on the same box
idles at 17.7 MiB.

The port would be small if that ever changes: `core.py` is pure, `store.py` is the only
filesystem coupling, and the Unraid-specific surface is three things — the notify script,
cron, and the `.page`.

**No live connection.** No SignalR, no held sockets, no daemon. Sonarr is read once a day,
on demand from the button, and immediately before a run. Everything the interface shows is
answered from that reading, and its age is stated rather than hidden.

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
