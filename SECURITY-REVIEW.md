# Security review notes

Updated September 28, 2026. This is not a security certification or release approval.
[PLAN.md](docs/PLAN.md) owns release readiness; [VALIDATION.md](docs/VALIDATION.md)
records evidence.

## Current risks

- The app holds Sonarr credentials and deletion authority. The documented target has no
  Sonarr recycle bin, so deletions are permanent.
- Browser write workflows and large-library performance still need practical evidence.
- Transport/cookie policy, Unicode credentials, request validation and admission limits
  have not had dedicated behavioral testing.
- The target volume reports JSON file modes wider than the application requests; the cause
  is unknown. See [VALIDATION.md](docs/VALIDATION.md).

## Deployment boundaries

Keep the service on a trusted network or behind a controlled HTTPS reverse proxy; do not
expose it publicly by default. `TVR_AUTH=none` disables login and is appropriate only on
an independently protected network. Forwarding headers do not establish authentication or
TLS. Settings and backup archives contain sensitive credentials; host and volume
administrators are trusted.

Passing tests and source inspection do not prove safe destructive operation. Keep Test
Mode on and schedules off until the plan's acceptance work is complete.
