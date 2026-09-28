# Validation record

Current evidence only. Per-change and deployment history belongs in git. Re-run the Linux
gate before quoting test counts or making a release decision.

## Automated gate

Last recorded Linux host gate: **September 28, 2026** on `fatzserver-host`, rerun after
the watched-only Calendar, conditional all-rule forecasts and selected-day agenda changes.

- 671 Python tests ran; 1 skipped because the host lacks two reference time zones.
- 40 frontend tests passed.
- Worker imports and shipped-module syntax checks passed; gate exited 0.

These counts are historical until the gate is run again. From Windows, use
`tools/check-on-host.ps1`; see [AGENTS.md](../AGENTS.md) for local checks and limitations.

## Target smoke

Read-only smoke on **September 22, 2026**, demo build 31, with Test Mode on and schedules
off. Login, version/build display, digest-scoped assets, browser refresh, Sonarr reading
age and config/state access were checked. It used the running instance's config rather
than the planned `/tmp` copy, so it was not an isolation test. No writes were exercised.

The target's Sonarr `recycleBin` is empty. Deletions are permanent; Unraid's SMB recycle
bin does not protect Sonarr's container-level deletes. This condition is accepted, not a
recovery guarantee. The Status warning remains correct.

The target's JSON files were observed with mode `0666`, despite the application requesting
`0644` under its default mask. The target uses Unraid FUSE/ACLs; the cause is unknown and
no source defect has been established. A legitimate run-lock collision was also observed
to count as one worker tick failure; it cleared without the worker becoming stuck.

## Additional container evidence

Real-container checks covered non-root startup, `UMASK`, unwritable backup destinations,
credential requirements, health/readiness, graceful shutdown and restart recovery after an
interrupted delete. The interrupted-run test used a synthetic Sonarr. See git history for
individual procedures and measurements.

## Coverage and remaining evidence

Automated tests cover retention decisions, exclusions, shared-file protection, Test Mode,
durable execution/recovery, backup/restore contracts, scheduling, settings revisions and
frontend module behavior. The Linux gate also imports worker modules and syntax-checks
shipped modules.

The Linux gate's fake-Sonarr fixtures cover Test Mode write boundaries, run execution,
partial failure, process interruption/retry, recovery errors and backup/restore staging.
The gate passed; it contacts no live Sonarr and writes only within disposable `/tmp`
staging. Partial Sonarr reads remain due for retry. Build 32 was built on the Linux host and its
shipped worker and build marker checked in a network-isolated container. On September 28,
the demo was replaced with build 32 after a preflight against a config copy (32 rules,
integrity clean, Test Mode on and schedule off in the copy). The replacement is healthy,
the worker reports running, and the live settings were preserved as requested: Test Mode
off and schedule on. One running container remains; rollback is build 31 plus a matching
config archive in a root-only directory. The next scheduled run completed at 05:22:29 UTC
on build 32 with no pending run. Its post-run Sonarr sync finished at 05:22:36 UTC with
reason `after the run` and no errors. The already-open browser reflected the refreshed
reading, and a real-series acceptance test completed successfully; both confirmed by the
operator.

No bugs are currently known. Real-provider behavior with live credentials, API-key
lifecycle and backup/restore against a real configuration have not had separate practical
acceptance runs; these are unverified scenarios, not known defects. Report any bug found
through the repository for reproduction and tracking. No large-library performance issue
has been reported. The bounded September 20 live run completed two deletions (2705 MiB)
with zero errors.
