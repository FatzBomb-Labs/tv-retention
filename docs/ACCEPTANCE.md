# Acceptance checks on the target system

Work through these on FatzServer before turning **Test Mode** off. Each step is either
read-only or reversible.

Test Mode governs the **scheduler** only: a scheduled run does everything except write. A
manual run is always live, so section 6 is the first thing here that can delete anything.

## 1. Install

- [ ] Plugins → Install Plugin → path to `install/tv-retention.plg`. Installation reports
      the SHA256 check passing.
- [ ] **Tools → TV Retention** loads, shows the version, and shows the **TEST MODE** chip.
- [ ] After an upgrade, the page loads the new script *and the new stylesheet* without a
      manual cache clear. The asset URL carries a hash of both files together — hashing
      them separately and truncating took every character from the first, so CSS-only
      releases shipped under the key the browser already held.
- [ ] The Plugins page row shows the **TV Retention** description and a television icon
      that opens the page when clicked. Verify the icon is actually **visible**, not merely
      present: an icon name with no glyph behind it renders as an empty, zero-sized
      element, so the link is there but there is nothing to click.
- [ ] The Tools tile shows the same icon. Both come from the same name.
- [ ] The Name column reads `tv-retention`, which is Unraid's plugin directory identifier,
      not a label.
- [ ] `ls /boot/config/plugins/tv-retention/` shows the package; there is no
      `schedule.cron` yet.
- [ ] `ls /var/log/plugins/tv-retention.plg` exists. `update_cron` will not honour the
      plugin's cron file without that marker.

## 2. Sonarr instances

- [ ] **Media management → Connections** → add `Sonarr-Series` (`http://<server>:8989`).
      **Test & save** reports the Sonarr version and how many series it holds.
- [ ] Add `Sonarr-Anime` (port 8990) and test it.
- [ ] Note whether either Sonarr has a recycle bin configured — the test reports it. If
      not, everything Sonarr deletes is permanent, which is why the alert offers a
      one-click fix rather than a setting of the plugin's own.
- [ ] Reload the page: the API keys show as masked, and saving again keeps them working.
- [ ] Disable an instance and confirm its rules stay in place and stop being processed.

## 3. The library

- [ ] **Series → All** lists everything both Sonarr instances hold. Confirm the count
      matches Sonarr's own.
- [ ] Switch between **All**, **Connected** and **Not connected** and confirm the counts
      add up.
- [ ] Switch to poster view and back. Confirm the selected series stays selected.
- [ ] **Hide ended** removes finished series with no rule, and keeps a finished series
      that has one. Confirm a finished series with an alert stays visible either way.
- [ ] Search for a series by part of its name; confirm the sort orders behave.

## 4. Presets and rules

- [ ] Create a preset, for example *Keep 180 days*. Point two shows at it, and confirm
      both cards show the preset name.
- [ ] Edit the preset. Confirm the editor lists the shows that will change, and that both
      cards update after saving.
- [ ] Confirm a preset in use cannot be removed.
- [ ] Add one show with **Custom** retention and confirm the preset has no effect on it.
- [ ] Confirm **Custom** is the first option and the one a new series starts on.
- [ ] Open a series with no rule, set a keep window, and press **Save** (not *Save and
      enable*). Confirm the rule is added switched off, and that no run touches it.
- [ ] Switch it on from the editor. Confirm the list behind the pane updates immediately,
      without pressing Update.
- [ ] Type a keep value into one series, click a second series, then click back. Confirm
      what you typed is still there. Leave the library and return: confirm it is not.
- [ ] Confirm a rule that resolves to no Sonarr series saves as **not matched**, in red,
      with the reason shown — and that a preview skips it.

## 5. Preview

- [ ] **Show scheduled changes** in the header. For each rule, confirm: the episode list
      looks right, air dates come from `sonarr` rather than an estimate, and no file
      appears that you want to keep.
- [ ] Deliberately set a rule that would delete most of a show, and confirm the per-rule
      guard blocks it with an explanation.
- [ ] Change a keep window in the editor and confirm the next-run lines above the settings
      move with it, before saving.

## 6. First live deletion

- [ ] Pick one show with a small, obviously-correct plan. Switch every other rule off.
- [ ] Press **Run now** and confirm the confirmation names the actual plan — the count and
      the size — rather than describing runs in general.
- [ ] Verify in Sonarr that exactly the listed episodes now show no file, and that they are
      unmonitored. Deleting always unmonitors; that is an invariant, not a setting.
- [ ] Verify on disk that Sonarr's recycle bin caught the files, if one is configured.
- [ ] Check the run appears in **System → Job history**, and that `journal.jsonl` in the
      state folder has a matching record.
- [ ] Switch the other rules back on.

## 7. Monitoring

- [ ] With a rule under **Unmonitor only**, confirm a run unmonitors what falls outside the
      keep window and asks Sonarr to fetch nothing.
- [ ] Switch one rule to **Full sync** and confirm the plan gains a monitor count.
- [ ] Widen a rule and save. Confirm the one-time pass is offered for the episodes the
      widening brought into scope, that the tree shows the window's own episodes checked as
      Sonarr has them, and that only the difference is sent.
- [ ] Confirm the unmonitor half happened on save without being offered — a run does it
      regardless, so waiting only gives Sonarr a day to fetch what that run would delete.
- [ ] Confirm an episode you unmonitored by hand in Sonarr is not re-monitored under
      Unmonitor only.

## 8. Alerts and freshness

- [ ] Open the page cold and confirm it is usable immediately, with each series' reading
      filling in on its own and no busy overlay.
- [ ] Confirm the editor names the age of the reading behind it, and that the refresh
      beside it updates that age, the counts and the plan.
- [ ] On a series with **no rule**, press refresh and confirm the facts actually change —
      it re-reads the catalogue entry, since there is no rule to check.
- [ ] Stop Sonarr briefly and run `main.py check`. Confirm one summary notification, the
      banner at the top of the page, and the affected shows flagged individually.
- [ ] Switch off a series that has an alert. Confirm its badge, its line in the roll-up and
      its notifications all stop — and that switching it back on brings them back.
- [ ] Acknowledge a warning and confirm it hides; change what it says and confirm it
      returns. Confirm an error cannot be acknowledged.

## 9. Schedule

- [ ] Enable a daily schedule. Confirm `/boot/config/plugins/tv-retention/schedule.cron`
      exists and `crontab -l` contains the entry.
- [ ] Wait for one scheduled run (or temporarily set it a few minutes ahead). Confirm it
      appears in history marked *schedule*, that it reports what it would have done, and
      that it changed nothing while Test Mode is on.
- [ ] Turn Test Mode off and watch one scheduled run go through for real.
- [ ] Disable the schedule and confirm the cron file is removed.

## 10. Restart

- [ ] Reboot, or stop and start the array. Confirm settings and rules survive, and that
      the cron entry is republished.

## 11. Removing a series

- [ ] Queue a removal and confirm nothing happens until a run applies it, and that undo is
      available until then.
- [ ] Confirm the removal action offered — leave it alone, monitor, unmonitor, or ask
      Sonarr to delete it — is what actually happens.
- [ ] Confirm a wrong title typed into the confirmation is refused.
- [ ] Confirm the plugin asks *Sonarr* to delete the series rather than deleting anything
      itself, so Sonarr's recycle bin and bookkeeping apply.

## 12. Uninstall

- [ ] Remove the plugin. Confirm the cron entry is gone, and that `settings.json` and the
      journal remain.
- [ ] Reinstall and confirm the rules come back exactly as they were.
