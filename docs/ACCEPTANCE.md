# Acceptance checks on the target system

Work through these on FatzServer before turning **Dry run** off. Each step is either
read-only or reversible.

## 1. Install

- [ ] Plugins → Install Plugin → path to `install/tv-delete.plg`. Installation reports
      the SHA256 check passing.
- [ ] **Tools → TV Delete** loads, shows the version, and reports *Dry run is on*.
- [ ] The Plugins page row shows the **TV Delete** description and a bin icon that opens
      the page when clicked. An `icon-` class Unraid does not ship renders as an empty
      element, so the icon silently disappears — verify it is actually visible.
- [ ] The Tools tile shows the same icon. Both come from the same name.
- [ ] The Name column reads `tv-delete`, which is Unraid's plugin directory identifier,
      not a label.
- [ ] `ls /boot/config/plugins/tv-delete/` shows the package; there is no
      `schedule.cron` yet.

## 2. Sonarr instances

- [ ] Add `Sonarr-Series` (`http://<server>:8989`). Press **Detect roots** and confirm it
      fills in `/tv` → `/mnt/user/media/TV`. **Test connection** then reports the Sonarr
      version and finds every series folder that holds files. Series with no episodes yet
      are listed separately as "not created", which is not an error.
- [ ] Add `Sonarr-Anime` (port 8990) with the same mapping, and test it.
- [ ] Note whether either Sonarr has a recycle bin configured — the test reports it. If
      not, deletions through Sonarr are permanent, so consider the plugin's own recycle
      folder.
- [ ] Reload the page: the API keys show as masked, and saving again keeps them working.

## 3. Presets and rules

- [ ] Create a preset, for example *Keep 180 days*. Point two shows at it, and confirm
      both cards show the preset name and its values.
- [ ] Edit the preset. Confirm the editor lists the shows that will change, and that both
      cards update after saving.
- [ ] Confirm a preset in use cannot be removed.
- [ ] Add one show with **Custom** retention and confirm the preset has no effect on it.

- [ ] Add one show by picking it from the Sonarr list. It saves as **matched**.
- [ ] Add one show by browsing to its folder. It also saves as **matched**.
- [ ] Point a rule at a folder Sonarr does not manage. It saves as **not matched**, in
      red, with the reason shown — and a preview skips it.
- [ ] Open the series picker and confirm series with no folder on this server are greyed
      out with the reason, and that a show already covered by a rule cannot be picked again.
- [ ] Recreate the rules the old shell script had, with the same day counts.

## 4. Library scan

- [ ] Configure **every** Sonarr instance first — a show owned by another Sonarr looks
      like an orphan to an instance that does not own it.
- [ ] Run the scan over `/mnt/user/media/TV` and confirm the findings are plausible.
- [ ] Confirm a *Moved or renamed* row really does exist in Sonarr at the stated path.

## 5. Preview

- [ ] **Preview all**. For each rule, confirm: the episode list looks right, air dates
      come from `sonarr` rather than `mtime`, and no file appears that you want to keep.
- [ ] Confirm the *unknown to Sonarr* list is empty, or that everything in it is
      genuinely something Sonarr does not manage.
- [ ] Deliberately set a rule that would delete most of a show, and confirm the per-rule
      guard blocks it with an explanation.

## 6. First live deletion

- [ ] Pick one show with a small, obviously-correct plan. Disable every other rule.
- [ ] Turn **Dry run** off. Press **Run now** and confirm the warning names deletion.
- [ ] Verify on disk that exactly the listed files are gone, together with their
      sidecars, and that nothing else was touched.
- [ ] Verify in Sonarr that the episodes now show no file and are unmonitored.
- [ ] Check the run appears in *Runs & history*, and that `journal.jsonl` in the state
      folder has a matching record.
- [ ] Re-enable the other rules.

## 7. Re-monitoring a widened rule

- [ ] With re-monitoring off, let one live run delete and unmonitor at least one episode.
      Confirm `unmonitored.json` in the state folder lists it.
- [ ] Turn re-monitoring on, widen the preset that rule uses, and **Preview**. Confirm the
      episode is reported as one that would be re-monitored, and that the ledger is
      unchanged by the preview.
- [ ] Run for real. Confirm Sonarr now shows the episode as monitored, and that it has
      left the ledger.
- [ ] Confirm an episode you unmonitored by hand in Sonarr is not affected.

## 8. Health check and caching

- [ ] Open the tab and confirm every pill is already populated, with no manual check.
- [ ] With a cold cache, confirm the page is usable immediately and shows fill in one by
      one, each showing *Reading Sonarr…* with only its own buttons held.
- [ ] Start `main.py check --scheduled` from a shell with the page open. Confirm the
      banner names the phase and counts shows, and that cards update as it goes.
- [ ] Confirm each pill's menu shows when it was read.
- [ ] Widen a preset and confirm the affected pills refresh rather than showing stale numbers.
- [ ] Stop Sonarr briefly and run `main.py check`. Confirm one summary notification, the
      banner at the top of the page, and the affected shows flagged individually.
- [ ] Confirm `schedule.cron` holds both entries when the retention run and the health
      check are enabled, and only the health entry when the run is off.

## 9. Schedule

- [ ] Enable a daily schedule. Confirm `/boot/config/plugins/tv-delete/schedule.cron`
      exists and `crontab -l` contains the entry.
- [ ] Wait for one scheduled run (or temporarily set it a few minutes ahead). Confirm it
      appears in history marked *schedule*, and that an Unraid notification arrived.
- [ ] Disable the schedule and confirm the cron file is removed.

## 10. Restart

- [ ] Reboot, or stop and start the array. Confirm settings and rules survive, and that
      the cron entry is republished.

## 11. Deleting a whole show

- [ ] With dry run ON, confirm the action is refused and says so.
- [ ] With the setting off, confirm the action is not offered at all.
- [ ] Enable it, turn dry run off, and confirm a wrong title is refused.
- [ ] On a show you genuinely want gone, confirm it is removed from Sonarr and disk, the
      rule disappears, and the journal and notification record it.

## 12. Uninstall

- [ ] Remove the plugin. Confirm the cron entry is gone, and that `settings.json` and
      the journal remain.
