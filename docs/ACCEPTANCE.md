# Acceptance checks on the target system

Work through these on FatzServer before turning **Dry run** off. Each step is either
read-only or reversible.

## 1. Install

- [ ] Plugins → Install Plugin → path to `install/tv-delete.plg`. Installation reports
      the SHA256 check passing.
- [ ] **Tools → TV Delete** loads, shows the version, and reports *Dry run is on*.
- [ ] `ls /boot/config/plugins/tv-delete/` shows the package; there is no
      `schedule.cron` yet.

## 2. Sonarr instances

- [ ] Add `Sonarr-Series` (`http://<server>:8989`) with mapping `/tv` →
      `/mnt/user/media/TV`. **Test connection** reports the Sonarr version and finds
      every series folder.
- [ ] Add `Sonarr-Anime` (port 8990) with the same mapping, and test it.
- [ ] Note whether either Sonarr has a recycle bin configured — the test reports it. If
      not, deletions through Sonarr are permanent, so consider the plugin's own recycle
      folder.
- [ ] Reload the page: the API keys show as masked, and saving again keeps them working.

## 3. Rules

- [ ] Add one show by picking it from the Sonarr list. It saves as **matched**.
- [ ] Add one show by browsing to its folder. It also saves as **matched**.
- [ ] Point a rule at a folder Sonarr does not manage. It saves as **not matched**, in
      red, with the reason shown — and a preview skips it.
- [ ] Recreate the rules the old shell script had, with the same day counts.

## 4. Preview

- [ ] **Preview all**. For each rule, confirm: the episode list looks right, air dates
      come from `sonarr` rather than `mtime`, and no file appears that you want to keep.
- [ ] Confirm the *unknown to Sonarr* list is empty, or that everything in it is
      genuinely something Sonarr does not manage.
- [ ] Deliberately set a rule that would delete most of a show, and confirm the per-rule
      guard blocks it with an explanation.

## 5. First live deletion

- [ ] Pick one show with a small, obviously-correct plan. Disable every other rule.
- [ ] Turn **Dry run** off. Press **Run now** and confirm the warning names deletion.
- [ ] Verify on disk that exactly the listed files are gone, together with their
      sidecars, and that nothing else was touched.
- [ ] Verify in Sonarr that the episodes now show no file and are unmonitored.
- [ ] Check the run appears in *Runs & history*, and that `journal.jsonl` in the state
      folder has a matching record.
- [ ] Re-enable the other rules.

## 6. Schedule

- [ ] Enable a daily schedule. Confirm `/boot/config/plugins/tv-delete/schedule.cron`
      exists and `crontab -l` contains the entry.
- [ ] Wait for one scheduled run (or temporarily set it a few minutes ahead). Confirm it
      appears in history marked *schedule*, and that an Unraid notification arrived.
- [ ] Disable the schedule and confirm the cron file is removed.

## 7. Restart

- [ ] Reboot, or stop and start the array. Confirm settings and rules survive, and that
      the cron entry is republished.

## 8. Uninstall

- [ ] Remove the plugin. Confirm the cron entry is gone, and that `settings.json` and
      the journal remain.
