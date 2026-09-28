# TV Retention plan of record

Updated September 28, 2026. This file tracks current release readiness; completed work is
recorded in git history. [VALIDATION.md](VALIDATION.md) holds test and target evidence.

## Current state

Implemented: Sonarr-based retention, Test Mode guards, exclusions and shared-file
protection, undoable queued removals, durable run recovery, backup/restore, provider-date
safety and civil-time scheduling. UI state protections and container behavior have been
verified. The target's empty Sonarr recycle bin is accepted; deletions there are permanent.

The operator approved the demo's live schedule with Test Mode off. Build 32 has completed
a scheduled run and an error-free post-run sync; do not change those live settings without
operator approval. This is approval for the current deployment, not blanket release
acceptance or a guarantee of recovery from permanent deletion.

## Safety requirements

- Process a rule only when it resolves to exactly one current Sonarr series.
- Exclusions protect monitored state and every file containing excluded episodes.
- Delete files only after all affected episodes are confirmed unmonitored.
- Ordinary retention never monitors episodes; explicit removal dispositions are separate.
- Test Mode blocks every external Sonarr mutation, manual or scheduled.
- Queued removals remain undoable until execution; stale or incomplete readings never
  authorize deletion.

## Remaining acceptance work

1. Re-run focused fixtures and the Linux host gate after code changes; record the current
   result in [VALIDATION.md](VALIDATION.md).
2. Use isolated config and fake Sonarr for destructive failure and recovery cases that a
   successful live run cannot establish: partial writes, Test Mode, restart and restore.
   Record attempted external writes. Do not repeat the live run just to prove it works.
3. A real-series acceptance test completed successfully. Along with the build-32 scheduled
   run, error-free post-run sync and confirmed browser refresh, this establishes the tested
   live workflows. No bugs are currently known. Report any bug found through the repository
   so it can be reproduced and tracked.
4. Real-provider behavior with live credentials, API-key lifecycle and backup/restore
   against a real configuration have not had separate practical acceptance runs. These are
   optional confidence checks, not known defects or blockers to the already tested flows.
   No performance complaint has been reported for the 3,019-series target.
5. Preserve the approved demo schedule and Test Mode setting. Use Test Mode on and schedules
   off for isolated acceptance exercises; restore the live settings only when returning
   to the approved demo deployment. Do not authorize additional live write exercises by
   changing this document.

The target's recycle-bin setting is empty. Sonarr deletes are permanent, and Unraid's SMB
recycle bin does not cover container-level deletes. The Status alert remains correct.

## Out of scope

Radarr, watched-state retention, new providers, outbound notifications, direct media
access, media mounts, filesystem auditing, frontend rewrites and AniList retention dates.
