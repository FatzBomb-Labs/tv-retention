# TV Retention plan of record

Updated September 28, 2026. This file tracks current release readiness; completed work is
recorded in git history. [VALIDATION.md](VALIDATION.md) holds test and target evidence.

## Current state

Implemented: Sonarr-based retention, Test Mode guards, exclusions and shared-file
protection, undoable queued removals, durable run recovery, backup/restore, provider-date
safety and civil-time scheduling. UI state protections and container behavior have been
verified. The target's empty Sonarr recycle bin is accepted; deletions there are permanent.

**Not approved for unattended destructive use. Keep Test Mode on and schedules off.**
Permanent deletion is an accepted target condition, not a recovery guarantee or approval
to run.

## Safety requirements

- Process a rule only when it resolves to exactly one current Sonarr series.
- Exclusions protect monitored state and every file containing excluded episodes.
- Delete files only after all affected episodes are confirmed unmonitored.
- Ordinary retention never monitors episodes; explicit removal dispositions are separate.
- Test Mode blocks every external Sonarr mutation, manual or scheduled.
- Queued removals remain undoable until execution; stale or incomplete readings never
  authorize deletion.

## Remaining acceptance work

1. Run focused fixtures and the Linux host gate; record its current result in
   [VALIDATION.md](VALIDATION.md).
2. Against isolated config and fake Sonarr, verify settings, queue, backup/restore,
   Test Mode and restart behavior; record external requests for writes.
3. Obtain practical evidence for browser write paths, partial-failure behavior, monitoring
   changes, run history and byte totals. Use a throwaway series, rollback image and matching
   config archive. Never use a production library.
4. Measure large-library performance. Existing target reading: 3,019 series, with 385
   displayed; interaction timing has not been measured.
5. Keep schedules off and Test Mode on except during an explicitly bounded, approved
   acceptance run; restore both afterward. No production canary is authorized here.

The target's recycle-bin setting is empty. Sonarr deletes are permanent, and Unraid's SMB
recycle bin does not cover container-level deletes. The Status alert remains correct.

## Out of scope

Radarr, watched-state retention, new providers, outbound notifications, direct media
access, media mounts, filesystem auditing, frontend rewrites and AniList retention dates.
