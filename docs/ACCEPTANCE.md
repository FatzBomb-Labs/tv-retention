# Acceptance checks on the target system

Work through these before turning **Test Mode** off. Each step is either read-only or
reversible.

Test Mode means nothing writes — scheduled or manual, no exceptions. Section 6 is where it
gets turned off, and it is the first thing here that can delete anything.

## 1. Start it

- [ ] `docker compose up -d --remove-orphans` with no `TVR_USERNAME`/`TVR_PASSWORD` and no `TVR_AUTH`.
      Confirm it **exits**, and that the message names both ways forward.
- [ ] Add the two variables and start again. `http://<host>:8787` shows a login.
- [ ] A wrong password is refused; the right one lands on the page with the **TEST MODE**
      chip showing.
- [ ] `docker logs` carries no traceback and no permission error. Confirm `/config` was
      taken over without anyone being asked to `chown` anything, and that `PUID=99
      PGID=100` is honoured if you set it.
- [ ] Every icon is **visible**, not merely present — the bars/grid toggle, refresh, close,
      the caret in the scheduled-changes menu. An icon with no glyph behind it renders as
      an empty, zero-sized element, so the button is there and there is nothing to click.
- [ ] Stop the container and start it again. Confirm you are logged out (sessions are in
      memory, deliberately) and that nothing else was lost.
- [ ] After an upgrade, the page loads the new script *and the new stylesheet* without a
      manual cache clear. The asset URL carries a hash of every asset together — hashing
      them separately and truncating took every character from the first, so CSS-only
      releases shipped under the key the browser already held.
- [ ] **Help → About** shows the semantic version, build number and the date this image
      was built. The compact version in the top banner remains the semantic version alone.
- [ ] With `TVR_AUTH=none`, confirm it starts, serves without a login, and says so in the
      log. Then put the password back.

## 1a. Bringing settings over from the plugin

- [ ] `cp /boot/config/plugins/tv-retention/settings.json ./config/settings.json`, start,
      and confirm every rule, preset and instance is present with its API key intact.
- [ ] Confirm the settings file on disk now reads `"settings_version": 13` and that any
      legacy `notifications` block is gone; Sonarr keys, rules and presets remain intact.

## 2. Sonarr instances

- [ ] **Media management → Connections** → add `Sonarr-Series` (`http://<server>:8989`).
      **Test & save** reports the Sonarr version and how many series it holds.
- [ ] Add `Sonarr-Anime` (port 8990) and test it.
- [ ] Note whether either Sonarr has a recycle bin configured — the test reports it. If
      not, everything Sonarr deletes is permanent, which is why the alert offers a
      one-click fix rather than a setting of its own.
- [ ] Reload the page: the API keys show as masked, and saving again keeps them working.
- [ ] Disable an instance and confirm its rules stay in place and stop being processed.

## 3. The library

- [ ] **Series → All** lists everything both Sonarr instances hold. Confirm the count
      matches Sonarr's own.
- [ ] Switch between **All**, **Connected** and **Not connected** and confirm the counts
      add up.
- [ ] Confirm the default list is an operational row with title/state, retention,
      episodes/storage, next airing or ended state, planned changes and alerts.
- [ ] Switch to poster view and back. Confirm the selected series stays selected and the
      column header is hidden only in poster mode.
- [ ] Narrow the browser to a phone-width viewport. Confirm rows collapse into a readable
      two-column record without clipping the title or planned changes.
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

- [ ] Confirm a run unmonitors what falls outside the keep window and asks Sonarr to fetch
      nothing. The plan's monitor count is always zero: no run monitors anything.
- [ ] Widen a rule and save. Confirm the one-time pass is offered for the episodes the
      widening brought into scope, that the tree shows the window's own episodes checked as
      Sonarr has them, and that only the difference is sent.
- [ ] Confirm the unmonitor half happened on save without being offered — a run does it
      regardless, so waiting only gives Sonarr a day to fetch what that run would delete.
- [ ] Confirm an episode you unmonitored by hand in Sonarr is not re-monitored.
- [ ] Exclude an episode that falls outside the keep window, monitor it in Sonarr, and run.
      Confirm it is neither deleted nor unmonitored — that guarantee is the reason there is
      no setting for this.
- [ ] In the exclusion picker, confirm the second column reads as Sonarr has it: unmonitor
      something in Sonarr, reopen, and confirm the box is clear rather than ticked.
- [ ] Open the picker and close it with **Save**, having touched nothing. Confirm no
      monitoring change is sent — only what you move is written.
- [ ] Move one box, save, and confirm the log names that series and counts exactly one.
- [ ] Confirm every episode shows its air date and source, and that one with none says so
      rather than showing a blank.
- [ ] Confirm episodes inside the keep window are shaded, and that the count matches what
      the pane says the next run would keep.
- [ ] Tick every episode of a season one by one. Confirm the season heading fills in, and
      that what is **saved** is still one entry per episode — the heading's own box is the
      only thing that means "including episodes that have not aired".

## 8. Alerts and freshness

- [ ] Open the page cold and confirm it is usable immediately, with each series' reading
      filling in on its own and no busy overlay.
- [ ] Confirm the editor names the age of the reading behind it, and that the refresh
      beside it updates that age, the counts and the plan.
- [ ] On a series with **no rule**, press refresh and confirm the facts actually change —
      it re-reads the catalogue entry, since there is no rule to check.
- [ ] Stop Sonarr briefly and run
      `docker exec tv-retention python3 /app/worker/main.py check`. Confirm the Status and
      Connections badges flag it, the affected shows are flagged individually, and no
      outbound notification or webhook is attempted.
- [ ] Switch off a series that has an alert. Confirm its badge and line in the roll-up stop;
      switching it back on brings them back.
- [ ] Acknowledge a warning and confirm it hides; change what it says and confirm it
      returns. Confirm an error cannot be acknowledged.

## 9. Schedule

- [ ] Enable a daily schedule a few minutes ahead. Confirm the run happens without anyone
      being logged in — close the browser and check the history afterwards. The worker is
      the point; authentication guards the interface, never the work.
- [ ] Confirm it appears in history marked *schedule*, reports what it would have done, and
      changed nothing while Test Mode is on.
- [ ] Turn Test Mode off and watch one scheduled run go through for real.
- [ ] Stop the container across a scheduled time, then start it again. Confirm the missed
      run is caught up rather than skipped.
- [ ] Set `TZ` and confirm "daily at 4am" means 4am where you are.

## 10. Restart

- [ ] `docker compose restart`, and reboot the host. Confirm settings, rules, history and
      the journal all survive, and that the schedule resumes on its own.

## 11. Removing a series

- [ ] Queue a removal and confirm nothing happens until a run applies it, and that undo is
      available until then.
- [ ] Confirm the removal action offered — leave it alone, monitor, unmonitor, or ask
      Sonarr to delete it — is what actually happens.
- [ ] Confirm a wrong title typed into the confirmation is refused.
- [ ] Confirm it asks *Sonarr* to delete the series rather than deleting anything itself,
      so Sonarr's recycle bin and bookkeeping apply.

## 12. Connections, API key, backup and status

- [ ] Under **General → Connections**, configure an optional provider, test it, and confirm
      its URL/credential is masked after saving. Saving an unchanged mask keeps the secret.
- [ ] Create the TV Retention API key, copy it, and confirm the full value is not shown after
      refresh. Regenerate replaces it; Revoke changes the state without exposing a secret.
- [ ] Configure a separate writable backup destination and press **Back up now**. Confirm
      the timestamped ZIP contains settings, state, journal and caches, excludes the backup
      directory itself, and is pruned to the configured count. Treat the archive as a
      credential-bearing file.
- [ ] Restore a point only after typing `RESTORE`; confirm the page asks for a reload and
      that no Sonarr call or media change occurs during backup/restore.
- [ ] Open **System → Status** and confirm build/date/uptime, Test Mode, sync age, pending
      or current run, Sonarr reachability/recycle-bin state, storage health and API-key
      state. Refreshing Status must be read-only.

## 13. Remove it

- [ ] `docker compose down --remove-orphans`. Confirm `./config` still holds the settings
      and the journal.
- [ ] Bring it back up and confirm everything is where it was.
