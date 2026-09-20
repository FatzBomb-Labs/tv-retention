# Container design record

The port is complete: `15dfca1` removed the Unraid integration and `fbadb3b` added the
HTTP front end. This records the design, not production approval. The
[production-readiness plan](PLAN.md) owns remaining work and
release gates; [SECURITY-REVIEW.md](../SECURITY-REVIEW.md) records current review limits.

## Architecture and boundaries

- **One image, one process.** `server.py` uses standard-library `ThreadingHTTPServer`
  over `actions.dispatch`, with the resident worker/scheduler in a background thread.
  Reusing the existing request boundary avoided a framework, supervisor or second service.
  The worker runs independently of browser login. `core.py` keeps retention decisions
  separate from network and filesystem effects; `store.py` owns local persistence.
- **Sonarr owns media.** No media mounts or path mapping: metadata and monitoring come
  from Sonarr, and deletion is requested through its API. Incorrect API calls can still
  damage the library; absence of direct media access is not a safety guarantee.
- **Portable runtime.** The container replaces the host PHP/Python bridge, WebGUI page,
  event hooks and cron integration rather than maintaining a parallel plugin. Standard
  library only keeps dependencies small, but application, interpreter and base-image
  security still require review and renewal. Scheduling uses validated IANA civil time,
  and the image installs Debian `tzdata` so DST behavior is available in production.
  Worker readiness and shutdown remain Phase 6.

## Persistence and process identity

`/config` is the only required volume in the shipped Compose file. Settings and default
state (caches, journal, run intent and posters) belong there; the application does write
to the filesystem. Config/state overrides require care: a backup must not silently omit
state stored elsewhere.

A separate optional persistent `/backups` mount is supported for archive retention. Uncomment
the example mount, set the Backup destination to `/backups`, and make the host directory
writable by the configured `PUID:PGID` before startup. The application does not recursively
chown this operator-supplied mount, so an explicit non-root `user:` must also match its
ownership. `/config` remains the only required volume; no media mount is supported. Backup
creation is transaction-coordinated and refuses a `state_dir` outside `/config` rather than
silently omitting authoritative state.

On root startup, `server.take_the_volume` prepares `/config` ownership and applies `UMASK`,
then drops supplementary groups and switches to `PGID`/`PUID`. This avoids requiring a
manual ownership repair for a fresh bind mount. With an explicit non-root `user:`, that
setup is skipped. Ownership scope, non-root UMASK behavior, backup permissions and supported
hardening settings still require Phase 6 container acceptance.

## HTTP and authentication

The interface and JSON API share port 8787 by default (`TVR_PORT` configures the server).
Login uses `TVR_USERNAME` and `TVR_PASSWORD`; startup refuses missing credentials or a
password shorter than eight characters unless the operator explicitly sets `TVR_AUTH=none`.
The shipped Compose placeholder intentionally fails that check.

Sessions are in memory, with `HttpOnly`/`SameSite=Strict` cookies and per-session CSRF
tokens. There is no proxy-header authentication mode. Restrict reachability to a trusted
network or controlled HTTPS reverse proxy; disabling login requires an independently
restricted boundary. Forwarding headers must not be treated as authentication or TLS
proof. Transport/cookie policy, HTTP limits and malformed-input handling remain Phase 6
work, not assurances supplied by this design record.

## Notifications and settings migration

The container has no outbound notification or webhook surface. Alerts remain keyed and
are shown in their owning Connections/Series views and the System → Status overview.
Legacy notification settings are discarded by migration so an old webhook URL cannot
remain as an unused credential.

`migrate.py` also scrubs notification settings from current-version documents. Its
connection migration carries legacy TMDB settings into `connections.tmdb` when absent,
retains the old key for normalization, and seeds API-key and backup metadata. This records
key preservation, not a promise that arbitrary future settings or restores are compatible;
Phases 3 and 7 own that validation.

## Release ownership

`VERSION` identifies the semantic release, `BUILD` the monotonically increasing shipped
build, and the image stamps `BUILD_DATE`. About shows all three. Increment BUILD for
deployed code changes, not this documentation cleanup. Static assets belong to a frozen
release snapshot under `/assets/<digest>/`, preventing a module graph from mixing releases.

[AGENTS.md](../AGENTS.md) owns deployment and scoped rollback/cleanup conventions;
[README.md](../README.md) describes usage. Follow the plan's acceptance ladder alongside
[ACCEPTANCE.md](ACCEPTANCE.md), recording candidate evidence in
[VALIDATION.md](VALIDATION.md). The remaining container checks and the supported deployment
profile are listed in [PLAN.md](PLAN.md). Test Mode's external-write boundary remains the
required safety rule; keep settings editable and do not repeat the old “nothing writes”
guarantee for local application writes.
