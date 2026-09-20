# Code and security review notes

Updated September 20, 2026. **Review notes, not a security certification or release
approval.** [docs/PLAN.md](docs/PLAN.md) owns the safety list, remaining work and release
gates; [docs/VALIDATION.md](docs/VALIDATION.md) owns recorded evidence.

## Scope

Read-only inspection of `src/`, `tools/`, `tests/`, `Dockerfile` and Compose, plus the
isolated Linux gate. No deployment or live-service checks have been performed. Passing
tests and source patterns are not behavioral assurance for anything in the open list.

The four failures the September 17 review recorded — executor dispatch, exclusions lost on
save, shared-file protection per-episode rather than per-file, and AniList overwriting
Sonarr dates — have since been fixed or, for AniList, removed as a date source. Their
disposition is in git history, not here.

## Open risks

- Cross-tab and late-response save conflicts are not yet rejected. A stale submission can
  overwrite newer intent.
- Container-level behavior is unverified: privilege dropping, `/config` and backup
  permissions, resource bounds, graceful shutdown, worker readiness and base-image
  renewal all need evidence from a running image.
- Transport and cookie policy, Unicode credential handling, request validation and
  admission limits are inspected but not exercised.
- Asynchronous UI state: `series-editor.js` debounces dispatch but applies returned counts
  without a request-generation guard.

## Threat and deployment boundaries

- The application holds Sonarr credentials and deletion authority. Having no media mounts
  removes one path of damage; incorrect API calls can still destroy library content.
  Sonarr's recycle bin is not a substitute for correct permission checks.
- Configuration, state and backup archives can contain credentials and executable intent.
  Host and volume administrators are trusted.
- Use a restricted trusted network or a controlled HTTPS reverse proxy, not default public
  exposure. Login uses `TVR_USERNAME`/`TVR_PASSWORD`, in-memory sessions and per-session
  CSRF tokens. `TVR_AUTH=none` explicitly removes authentication; forwarding headers are
  not an authentication boundary.
- Standard-library-only and one process reduce dependencies, not review obligations.

The old categorical assurances about XSS/CSRF, leaks, exception containment and "clean"
modules stay withdrawn. Follow the plan's acceptance ladder before enabling destructive
operation.
