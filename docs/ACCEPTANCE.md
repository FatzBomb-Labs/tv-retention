# TV Retention Acceptance

**Not ready for unattended destructive use.** Keep schedules off and Test Mode on unless a
step explicitly authorizes a disposable environment. No production credentials, media or
Sonarr target belong in these checks.

The implementation baseline is documented in [PLAN.md](PLAN.md). Historical test results and
known limits are recorded in [VALIDATION.md](VALIDATION.md).

## Safety rules

- Never test deletion against a production library.
- Use a copied configuration and an isolated Sonarr instance.
- Test Mode must block every external Sonarr mutation through the UI, CLI, scheduler and
  direct action paths.
- Queueing a removal must not mutate Sonarr. Undo must work until execution starts.
- No file deletion is valid unless all affected episodes were successfully unmonitored.
- Restore must leave Test Mode on, schedules off and historical work quarantined until review.
- A read-only smoke test is not permission to run a write-enabled operation.

## Acceptance sequence

### 1. Deterministic checks

Run the focused frontend/backend checks and `tools/check-on-host.ps1`.

Confirm:

- the Linux gate passes;
- worker imports and shipped-module syntax checks pass;
- no unexpected test failures or skipped mandatory checks exist;
- the current result is recorded in [VALIDATION.md](VALIDATION.md).

### 2. Isolated container

Use fake Sonarr and a disposable config. Verify the smallest complete workflow:

- startup refuses missing or invalid credentials;
- login, configurable port and health endpoint work;
- settings, rules, exclusions, queues and migrations survive restart;
- UI saves and background refreshes do not lose newer edits;
- Test Mode sends no Sonarr mutations;
- backup, restore staging and activation preserve the safety defaults;
- malformed HTTP/RPC input is rejected cleanly;
- shutdown and restart recover without replaying completed work.

Record exact external requests for any operation that is allowed to write in this fixture.

### 3. Disposable Sonarr

Use synthetic media and a dedicated Sonarr instance. Test Mode may be disabled only here.
Verify one bounded run for:

- monitoring changes;
- shared multi-episode files;
- recycle-bin behavior;
- partial failures and restart recovery;
- truthful run history, journal and byte totals.

Restore Test Mode on and schedules off when finished.

### 4. Target read-only smoke

Stage under `/tmp` with a copied configuration, schedules off and Test Mode on. Verify:

- login and version/build display;
- asset loading and browser refresh behavior;
- library, cache ages and read-only Sonarr refreshes;
- config/state permissions and status reporting.

Do not save rules, queue removals, run retention, change monitoring, alter the recycle bin,
change API keys or restore during this step.

### 5. Operator canary

This remains deferred. It requires:

- steps 1-4 completed and reviewed;
- one rollback image and matching config archive;
- one deliberately bounded series and plan;
- explicit operator approval;
- a documented return to Test Mode with schedules disabled.

## Open acceptance items

- Docker and WSL are unavailable on the current Windows development machine; container
  checks require the Linux host or another approved environment.
- AniList cannot provide retention dates until it can prove series, season and episode
  identity without title-only matching.
- Browser reload, accessibility, large-library performance, container permissions, HTTP
  hardening, readiness and graceful shutdown still need practical evidence.
- No live deployment, production deletion or general unattended schedule is authorized.
