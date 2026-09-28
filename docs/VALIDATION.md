# Validation record

Current evidence only. Per-change and deployment history belongs in git. Re-run the Linux
gate before quoting test counts or making a release decision.

## Automated gate

Last recorded Linux host gate: **September 21, 2026** on `fatzserver-host`.

- 656 Python tests passed; 1 skipped because the host lacks `America/Detroit` zone data.
- 36 frontend tests passed.
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

Still lacking practical evidence: browser write workflows, monitoring-write correctness
beyond the bounded live run, real-provider behavior with live keys, API-key lifecycle,
backup/restore against a real configuration, and large-library interaction timing. The
bounded September 20 live run completed two deletions (2705 MiB) with zero errors; this is
not broad write-path acceptance.
