# Acceptance checks

**Not ready for unattended destructive use. No live writes are authorized by this
checklist.** Keep retention schedules off and Test Mode on. The
[2026-09-17 production-readiness plan](PLAN.md) is the gate of record; this checklist is
not evidence that its fixes have landed.

The review confirmed executor failure, retention edits losing exclusions, shared files
being both protected and selected for deletion, and AniList overwriting Sonarr dates.
Immediate monitoring/recycle-bin actions bypass Test Mode. Backup/restore, concurrent
persistence, recovery, scheduling and UI-state correctness also have unresolved work.
Do not interpret a successful preview or old validation record as release approval.

## Environment and sequence

**Default: disposable config, fake Sonarr and no production credentials or endpoints.**
All saves, API-key changes, monitoring, queue operations, restore, failure injection and
restart checks below belong there. For actual media behavior, use a dedicated disposable
Sonarr with synthetic expendable files only after PLAN's prerequisite gates. Deletion is
not guaranteed reversible, even with a recycle bin.

Follow PLAN's acceptance ladder in order:

1. **A — Deterministic fixtures:** P0/P1 regressions, including the executor and mutation matrix.
2. **B — Isolated container + fake Sonarr:** full UI/action flow and exact request recording.
3. **C — Disposable real Sonarr/media:** separately authorized destructive checks after A/B.
4. **D — Target read-only smoke:** the restricted section below, after isolated validation.
5. **E — Operator-approved target canary:** deferred to PLAN; not authorized now.

Unchecked items describe required evidence, not promises the current code satisfies. Record
candidate revision, image/base digest, VERSION/BUILD/date, environment, results and failures
in [VALIDATION.md](VALIDATION.md). Do not waive known failures because older suites passed.

## 1. Isolated startup, upgrade and persistence

- [ ] Missing login credentials cause startup refusal with both remedies named. Wrong
      passwords fail; valid credentials open the UI. Explicit `TVR_AUTH=none` works only
      in the protected test network; restore login afterwards.
- [ ] Fresh settings start with Test Mode on and retention schedule off. Verify logs,
      `/config` ownership and writes under PUID/PGID, UMASK and explicit non-root `user:`;
      check configurable port/health behavior rather than assuming compatibility.
- [ ] Restart/recreate only the disposable application. Sessions expire; settings,
      rules, history and journal persist. Removing the disposable container does not
      remove its persistent config. No production Sonarr stop or host reboot is needed.
- [ ] Migrate a copy of supported older settings, including plugin-era fixtures, with
      disposable endpoints and schedules forced off before startup. Verify rules, presets,
      exclusions, connections and credential retention without displaying secrets; legacy
      notifications disappear and migration settles on the current supported schema.
      Preserve an untouched copy; do not replay old intent or assume a fixed schema number.
- [ ] Upgrade without clearing the browser cache: script, stylesheet and nested imports
      use one release digest. A CSS-only change also changes the namespace.

## 2. Build 20 UI and navigation checks

Retain these visual/workflow checks for later candidates; Build 20 is not a safety approval.

- [ ] Icons are visibly rendered: list/grid, refresh, close and scheduled-changes caret.
      **Help → About** shows semantic version, build number and image build date; the
      compact top banner shows semantic version only.
- [ ] **Series → All / Watching / Not watching** counts agree with the fixture catalogue.
      Watching means a retention rule, not watched history. Presets and Exclusion Rules
      remain under Series; Settings has General, Connections, Air dates and Schedule;
      System has Status, Stats, Backup and Logs.
- [ ] List rows show title/state, retention, episodes/storage, next airing/ended state,
      scheduled-change badges and alerts. Poster/list switching retains selection and
      hides the column header only in poster mode.
- [ ] Phone-width rows form readable two-column records without clipped titles/badges.
      Check long titles, zoom, keyboard focus, light/dark themes and visible primary actions.
- [ ] Search/sort work. **Hide ended** hides ended shows without rules, but keeps those
      with rules or alerts. **Alerts only** filters to the affected shows.
- [ ] The editor preserves visible series facts/actions around scrolling settings.
      Draft next-run counts update before save, monitoring colors describe the keep window,
      and counts open the matching episode details.

## 3. Rules, presets, exclusions and plan integrity — isolated only

- [ ] **Custom** is first/default. Two rules sharing a preset show its name; editing it
      lists and changes both, not a custom rule. An in-use preset cannot be removed.
- [ ] **Save** adds a disabled rule; **Save and enable** enables it. The existing-rule
      switch saves immediately. Disabled ordinary retention stays inactive, but saving
      a disabled rule currently can still send the outside-window monitoring pass.
- [ ] Draft values survive browsing another series and clear when leaving the library.
      Verify exclusion -> retention edit -> save -> reload preserves protection, and
      Undo -> edit -> save does not resurrect a queue. These are unresolved regressions.
- [ ] Global Exclusion Rules and per-series overrides/pickers explain their sources.
      Global exclusions cannot be unticked locally. Whole-season selection covers future
      episodes; individually selecting all current episodes remains individual entries.
      Specials inherit the global default unless overridden on the series.
- [ ] Any keeps when any condition says keep; All requires every condition to say keep.
      Unknown dates prevent deletion in both. Unique identity is required; unmatched or
      ambiguous rules must not produce executable work.
- [ ] Check scheduled-change counts, bytes, reasons and date provenance against fixture
      truth, including exclusions, future/unknown episodes and shared multi-episode files.
      No protected file may also be executable. There is **no deletion-percentage guard**.
- [ ] Stale/partial readings cannot hide Run or claim a complete empty plan. Confirmation
      identifies actual scope and uncertainty. Delayed responses and concurrent tabs must
      not overwrite newer edits or clear current drafts; require PLAN's regression evidence.

## 4. Monitoring, removals and execution — unsafe tests, isolated only

- [ ] Record exact Sonarr requests for every entry point with Test Mode on/off. Required
      release behavior is zero external mutations in Test Mode; current scope-pass,
      monitored-picker and recycle-bin actions violate this. Settings/local operational
      writes are a separate policy, not proof that Sonarr was untouched.
- [ ] **Change monitor status for episodes within scope** shows the whole current keep
      window, not only newly scoped episodes. Boxes reflect Sonarr, shaded rows/counts
      agree, and only changed flags are submitted. With writes permitted in isolation,
      these changes and the automatic outside-window pass happen on save, not next run.
- [ ] The exclusion picker's monitoring column submits only changed flags; no-op picker
      saves send none. Do not confuse this with the retention editor's automatic scope pass.
      Verify excluded episodes retain their state through subsequent edits and runs.
- [ ] Queue each removal disposition: leave Sonarr untouched, monitor all, unmonitor all,
      monitor inside the window, delete series keeping files, delete series/files. Queueing
      alone must not apply the disposition; Undo must persist until execution begins.
- [ ] The UI requires exact `DELETE` for series-record removal or `DELETE ALL` for series
      and files; wrong words fail, not a wrong series title. **Set monitoring in Sonarr
      before it goes** is a separate immediate picker; queue Undo does not reverse it.
- [ ] Require executor, unmonitor-failure, partial-failure, interrupted-run and changed-identity
      regressions to pass before disposable real deletion. Ordinary retention never monitors;
      explicit removal monitoring is separately identified. No delete may proceed without
      successful unmonitoring of every affected episode or against any protected shared file.
- [ ] Only at ladder C, with explicit isolated Test Mode-off authorization, verify exact
      file/monitor outcomes, recycle-bin behavior and truthful history/journal/byte totals.
      Restore safe settings afterwards. Never substitute a real user's show for this fixture.

## 5. Connections, providers, alerts and freshness — isolated only

- [ ] Add multiple disposable Sonarr instances under **Settings → Connections**.
      **Test & save** reports version/count/recycle-bin state. Reload masks credentials;
      saving an unchanged mask preserves them. Disabling an instance retains its rules.
- [ ] Test optional connections without exposing secrets. Credential fields are masked;
      URLs are not promised secret masking. Plex/Jellyfin success means connectivity only,
      not usable episode dates. Do not count unimplemented choices as date providers.
- [ ] With fixture responses, verify Sonarr precedence, provider order, numbering and
      unresolved exclude/disable behavior. AniList must not overwrite existing dates;
      the confirmed failure blocks enabling it. Check estimates/history provenance against
      real fixture events rather than trusting a provider's successful connection test.
- [ ] Cold load remains usable as per-show readings arrive. Reading age and per-series
      refresh update facts/counts; a show without a rule refreshes catalogue facts too.
- [ ] Simulate Sonarr unavailability using the fake service, not production outages.
      Connections/Status/series alerts identify the problem; no outbound webhook is sent.
      Under managed-only filtering, disabling a rule hides its alerts; enabling restores
      them. Acknowledged warnings return when changed; errors cannot be acknowledged away.
- [ ] Status includes build/date/uptime, mode, schedule, sync age, pending/current run,
      reachability/recycle bin, storage, API-key state and warnings/errors. Status refresh
      sends no Sonarr mutations. Worker readiness must not be inferred from HTTP alone.

## 6. API key, backup and restore — isolated only

- [ ] Create/copy the TV Retention key; normal snapshots expose metadata, not the full
      secret or hash. Regenerate replaces it and Revoke changes its state. Test preservation
      of the one-time reveal through background rendering; this remains open work.
- [ ] **System → Backup → Back up now** creates a credential-bearing archive of the active
      config data at a separate absolute destination. Verify archive contents, exclusion of
      the destination, persistence after replacement and owned-archive retention.
- [ ] Exercise non-root permissions, concurrent/same-second backups and external-state
      handling. The supplied Compose mount alone does not persist a separate backup path.
      Atomic archive replacement is not evidence of snapshot consistency.
- [ ] Restore rejects missing/wrong `RESTORE` confirmation and malformed/incompatible
      archives. Require PLAN's staged activation, rollback, concurrency and permission gates;
      current restore is not proven safe. Use only disposable copies and endpoints.
- [ ] After restore, no historical intent/queue or schedule may silently re-arm; Test Mode
      must be on and scheduling off until reviewed. Neither restore nor subsequent worker
      activity may mutate Sonarr without new authorization. Verify actual reload and draft
      reset, not just a returned reload flag. These protections are still planned.

## 7. Scheduling and recovery — isolated only

- [ ] The prominent Test Mode card is above schedule controls and saves its toggle
      immediately. Required wording must distinguish local writes from external mutations;
      the current blanket "nothing writes" wording is not an assurance.
- [ ] Using fake time/service fixtures, verify hourly/daily/weekly/monthly/custom schedules,
      timezone/DST, sparse cron, missed-run catch-up and Sonarr-unavailable pending work.
      The worker must run without login and avoid duplicate/replayed writes across restart.
- [ ] A scheduled test pass appears in Logs/Status as such, without a durable run-history
      or journal outcome; verify external requests independently. After the prerequisite
      gates, write-enabled schedule checks belong only to disposable ladder C.
- [ ] Ended-series notices, empty-window disabling and one-time Auto re-enable behave as
      documented, including specials and exclusion-only files. Observe Test Mode behavior
      separately; this is not permission to schedule a live library.

## 8. Target read-only smoke — restricted ladder D

Only after A–C and operator approval. This level proves display/read behavior, not writes.

- [ ] Stage under `/tmp` with a copy of settings and `TVR_CONFIG` pointed away from `/boot`.
      Force schedules off and Test Mode on before startup; exclude executable intent and
      queued work from staging. Never mount the active production configuration for this check.
- [ ] Verify login, version/build/date, asset refresh, library display, cache ages,
      read-only Sonarr refreshes and Status/permissions against the staged copy.
- [ ] Browse only: no rule/picker saves, enable toggles, queueing, Run, recycle-bin fix,
      API-key changes or restore. Test Mode alone does not make these actions read-only.
      Local staging cache/log writes are expected; Sonarr mutations are not permitted.
- [ ] Do not stop production Sonarr, reboot its host or restore production settings for
      convenience. Clean up only the isolated TV Retention staging/preflight artifacts.

## 9. Target canary and release — deferred

No target deletion checklist is authorized while P0/P1 findings remain. PLAN's ladder E
requires A–D evidence, operator approval, one deliberately bounded plan and a matching
rollback image/config archive. General scheduling requires a separate decision after the
canary review. Follow PLAN's release/rollback and scoped-cleanup gates; a documentation
cleanup, healthy page or successful read-only smoke does not meet them.
