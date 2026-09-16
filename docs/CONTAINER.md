# The container port

**Done.** Landed over commits `15dfca1` (cut Unraid out) and `fbadb3b` (serve it). Kept as
the record of what the port was and why each piece went where it did — the decision behind
it is in [PLAN.md](PLAN.md).

What it cost, against the estimate below: the Unraid surface was exactly as small as it
looked, `core.py` never moved, and the suite went 388 → 372 → 386 as package tests were
replaced by server tests. Two things were not in the plan. Ten icons had to be drawn,
because the interface borrowed the WebGUI's Font Awesome and that is neither ours to ship
nor present anywhere else. And the container had to take ownership of its own volume: a
bind mount arrives owned by root, and telling people to `chown` a directory before the
thing will start is the exact friction that sent this project looking at its neighbours in
the first place.

**The plugin is not kept alongside.** It borrows PHP and Python from the host, so an Unraid
release can break it at any time, and polishing something with that dependency on the way to
a container is work with a shelf life. `api.php`, `TVRetention.page` and `event/` are
deleted rather than maintained. Nothing is lost operationally: the plugin has never taken a
live deletion, so there is no running system to protect.

---

## What carries over

Of 8,594 lines, most of the plugin is not Unraid-specific and does not move.

| File | Lines | Change |
|---|---|---|
| `core.py` | 945 | **none** — pure, and the part that decides deletions |
| `sonarr.py` | 306 | none |
| `migrate.py` | 238 | none |
| `schedules.py` | 209 | none, though most of it stops being reachable (see below) |
| `alerts.py` | 200 | none |
| `tmdb.py` | 112 | none |
| `actions.py` | 727 | `dispatch(request)` stays; it stops arriving over stdin |
| `store.py` | 380 | paths only — five constants and the appdata lookup |
| `main.py` | 1,388 | four functions go, one loop arrives |
| `app.js` / `app.css` / `interface.html` | 3,965 | served as static files |
| `api.php` | 124 | **deleted** |
| `TVRetention.page`, `event/*` | ~30 | **deleted** |

`core.py` not moving is the point. The retention decision is the dangerous part, it is
already pure, and it must not travel in the same change as everything else.

### The Unraid surface, in full

Everything that knows it is on Unraid:

```
store.py    CONFIG  CRON  RUNTIME  UPDATE_CRON  NOTIFY   + state_dir from docker.cfg
main.py     array_ready()  require_ready()  notify()  write_cron()
include/    api.php, TVRetention.page
event/      disks_mounted, stopping_svcs
```

Five constants, four functions, four files. `array_ready` and `require_ready` disappear
outright — a container has no array to wait for, and no `/mnt/user` to check, because it
mounts no media at all.

---

## Shape

One process, one image, no media mounts.

```
tv-retention
  /config          the only volume: settings.json, caches, journal, posters
  :8787            the interface and its API
  SONARR_URL etc.  optional; the interface configures instances as it does now
```

**No media mounts**, and no path mapping — the worker touches no filesystem, so it cannot
damage a library even if it is wrong. This is not a differentiator on its own: an
API-driven container normally has one config volume, and most of the neighbours do. It is
worth stating in the README as a property, not as a headline.

The HTTP server is standard library — `ThreadingHTTPServer` over `actions.dispatch`, which
already takes a decoded request and returns a plain dict. That is roughly eighty lines,
replacing 124 lines of PHP that exist only to bridge into emhttp.

### One thing gets easier

Every `:where()` reset in `app.css` — the margins, the minimum widths, `width: 100%` on
selects — exists because the page is embedded in Unraid's stylesheet. So does the
`#tv-retention` scoping, and the test guarding all of it. In a container the page is ours
and that entire hazard class disappears.

---

## What has to be built

Three things. Everything else is moving code that already works.

### 1. Authentication — the open question

This is the design work, and it is not optional. This software exists to delete media, and
most containers in this space ship with no authentication at all because they assume a
trusted LAN. That assumption is doing more work than it should for a tool with delete
authority. Today the WebGUI supplies both authentication and a CSRF token for free; both
have to be replaced.

| Option | For | Against |
|---|---|---|
| **None, bind to LAN** | what the neighbours do | a tool that deletes media should not be the one that trusts the network |
| **Password + session cookie** | stdlib (`hashlib.scrypt`), no dependency, forced on first run | a password to store and reset; needs CSRF handling of its own |
| **Trust a proxy header** | free SSO for anyone already running Authelia or authentik | catastrophic if the container is reachable without the proxy |
| **API key only** | trivial | no session, so the key ends up in a bookmark |

**Recommendation:** a password with a session cookie as the floor, set on first run and not
disableable, plus proxy-header trust as an explicit opt-in for people who already have SSO
and know what they are turning on. `SameSite=Strict` plus a per-session token for CSRF.

**This is the decision to make before anything is written**, because it shapes the request
path every action goes through.

### 2. Notifications (removed)

The container has no outbound notification or webhook surface. Alerts remain keyed and
are shown in the in-app Alerts, owning Connections/Series views, and the System → Status
overview. Legacy notification settings are discarded by migration so an old webhook URL
cannot remain as an unused credential.

### 3. Resident loop, and later a live connection

`schedules.py` stays but most of it stops being reachable. A container decides "run once a
day" with a sleep loop, and the two things that justified the tick — catching up a run
missed while the server was off, and holding one until Sonarr answers — are now trivially
true rather than carefully arranged. The cron file, `update_cron`, and the `/var/log/plugins`
marker all go.

**v1: poll the change feed every ten seconds.** The mechanism already exists — it is what
the minute tick calls — and running it from a resident process is the whole of what
"live" means to anyone using this. Two small queries, no dependency, no new failure mode.

**v2, only if v1 proves insufficient:** Sonarr's SignalR endpoint. It needs a websocket
client, which would be this project's first dependency outside the standard library. In a
container that is acceptable — the image is ours — but it should be a deliberate second
step with evidence behind it, not part of the port.

---

## Migration

Settings are already at v7 with a migration chain behind them, and the container reads the
same document: point it at the plugin's `settings.json` once, let `migrate` run, and write
the result into `/config`. The state folder is already on appdata. Nothing needs
re-entering, including API keys.

Test Mode changed meaning on the way across, and for the better. It governed the scheduler
only, so a manual run deleted for real while the page said TEST MODE at the top of it. Now
it means nothing writes at all, which is what the Run button had to be able to say without
lying — and one rule with no exceptions is worth more than the flexibility it cost.

---

## What is lost

Said plainly, so it is not discovered later:

- **The WebGUI's authentication**, replaced by something we now own and must get right.
- **Native Unraid notifications**; the container reports alerts in its own interface.
- **Zero memory at rest.** Measured: about 35 MiB held permanently against the plugin's
  nothing. The lightest container on this server idles at 17.7 MiB.
- **The Tools menu entry**, and with it the fact that it is already in front of you.

None of these outweigh running as root on other people's servers with authority to delete
their media. All of them are worth stating before the work starts.

---

## Sequencing

1. **Decide authentication.** Nothing else can be written around an undecided request path.
2. **Cut the Unraid surface out.** Delete `api.php`, the `.page` and `event/`; replace
   `array_ready`/`require_ready` with nothing, remove `notify` and `write_cron` in favour of
   the resident loop; repoint `store.py` at `/config`. The suite must still pass at the end of this, which
   is what makes it safe — `core.py` is untouched throughout.
3. **Build the HTTP server and the image.** `actions.dispatch` already takes a decoded
   request and returns a dict, so this is a thin front end over what exists.
4. **Prove it** on a copy of the real settings, read-only, against the real Sonarr — the
   same discipline `docs/VALIDATION.md` records. Then once, for real, on one series.
5. **Publish.** GPL-3.0, GHCR image, a README written for a stranger rather than for its
   author, and Test Mode on by default with the validation state stated plainly.

Step 2 is a deletion, not a rewrite, and that is the whole reason this is tractable: what is
being removed is five constants, four functions and four files.
