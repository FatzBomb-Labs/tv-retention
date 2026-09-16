#!/usr/bin/env python3
"""The HTTP front end: the page, its assets, the JSON API, and the poster proxy.

This replaces `api.php` and the Unraid `.page` together. The plugin got authentication and
a CSRF token free from emhttp, and reached the worker by spawning it per request; a
container owns both, and the worker is already in this process.

The wire format is unchanged from the plugin's — a form POST carrying `csrf_token` and a
JSON `payload` — because `actions.dispatch` already takes a decoded request and returns a
plain dict. The interface did not have to change to be served from somewhere else.

Standard library only.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import http.cookies
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import main
from actions import dispatch
from store import load_settings, state_dir

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / 'assets'
MARKUP = HERE.parent / 'include' / 'interface.html'

def env(name: str, fallback: str = '') -> str:
    """An environment variable, where empty means absent.

    `TVR_PORT=` in a compose file is a realistic way to write it, and os.environ.get's
    default does not apply to a variable that is set to nothing — it applies to one that is
    not set at all. int('') then takes the container down at startup.
    """
    return (os.environ.get(name) or '').strip() or fallback


# Every environment variable env_int() could not parse, by name, with the raw value it
# was given. Checked by startup_error() rather than left to crash the process: a typo in
# PUID used to raise ValueError at import time, before the same clear "will not start"
# message a short password gets ever had a chance to run.
_bad_env: dict[str, str] = {}


def env_int(name: str, fallback: int, base: int = 10) -> int:
    """An integer environment variable, recorded and replaced rather than crashing.

    The fallback keeps the module importable — PORT and the rest still have to be usable
    numbers for everything below this to work — but the bad value is not silently
    forgotten either: startup_error() reports exactly what was wrong, the same way it
    already does for a missing password.
    """
    raw = env(name)
    if not raw:
        return fallback
    try:
        return int(raw, base)
    except ValueError:
        _bad_env[name] = raw
        return fallback


PORT = env_int('TVR_PORT', 8787)
USERNAME = env('TVR_USERNAME')
PASSWORD = env('TVR_PASSWORD')
# The only way to run without a login, and it is a value nobody can forge. Sonarr kept a
# "disabled for local addresses" mode and it became CVE-2026-30975: a caller spoofed
# X-Forwarded-For to look local and skipped authentication entirely. A header cannot be
# trusted to say where a request came from; an environment variable can be trusted to say
# what the operator chose.
OPEN = env('TVR_AUTH').lower() == 'none'
SESSION_HOURS = env_int('TVR_SESSION_HOURS', 720)
# Parsed here rather than where they are used, in take_the_volume(): every malformed
# environment variable is reported by startup_error() the same way, before anything
# tries to act on it — a container is not meant to learn from a traceback that PUID was
# misspelled in the compose file.
PUID = env_int('PUID', 1000)
PGID = env_int('PGID', 1000)
UMASK = env_int('UMASK', 0o22, base=8)

MAX_BODY = 1024 * 1024
COOKIE = 'tvr_session'


def startup_error() -> str:
    """What is wrong with the configuration, or an empty string.

    Refusing to start is deliberate. The alternative — run, but nobody can log in — leaves
    the half that deletes running unsupervised while the half that would notice is locked
    away, and the realistic way to reach it is a typo in a compose file six months from now.
    """
    if _asset_error:
        return f'TV Retention will not start: {_asset_error}.'
    if _bad_env:
        broken = ', '.join(f'{name}={value!r}' for name, value in sorted(_bad_env.items()))
        return f'TV Retention will not start: not a whole number — {broken}.'
    if OPEN:
        return ''
    if not USERNAME or not PASSWORD:
        return ('TV Retention will not start without a login.\n\n'
                '  Set TVR_USERNAME and TVR_PASSWORD, or set TVR_AUTH=none to run with no\n'
                '  authentication at all — appropriate only if nothing untrusted can reach\n'
                '  the port, for example behind Tailscale or a VPN.\n\n'
                'This software deletes media. It will not quietly run open.')
    if len(PASSWORD) < 8:
        return 'TVR_PASSWORD must be at least 8 characters.'
    return ''


# -- sessions ---------------------------------------------------------------
# In memory, so a restart logs everyone out. That is the right trade for a tool with one
# operator: nothing to persist, nothing to leak, and no session store to invalidate.
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
# Enough to make guessing over a LAN pointless without maintaining per-address state.
_failures = 0


def open_session() -> dict:
    token = secrets.token_urlsafe(32)
    entry = {'csrf': secrets.token_urlsafe(32), 'expires': time.time() + SESSION_HOURS * 3600}
    with _lock:
        for stale in [key for key, value in _sessions.items() if value['expires'] < time.time()]:
            _sessions.pop(stale, None)
        _sessions[token] = entry
    return {'token': token, **entry}


def session_for(header: str | None) -> dict | None:
    if OPEN:
        return {'csrf': '', 'expires': 0}
    if not header:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(COOKIE)
    if not morsel:
        return None
    with _lock:
        entry = _sessions.get(morsel.value)
    if not entry or entry['expires'] < time.time():
        return None
    return entry


def credentials_match(username: str, password: str) -> bool:
    # Both compared, both in constant time: comparing the username with == leaks whether it
    # was right through timing, which is half the secret.
    return (hmac.compare_digest(username, USERNAME)
            & hmac.compare_digest(password, PASSWORD))


# -- the page ---------------------------------------------------------------

# What ships: the interface's own scripts, styles and icons, and nothing else. A file of
# any other type in this directory is not part of the release — it neither moves the
# digest nor gets served — so a stray backup or notes file cannot become fetchable just
# by sitting next to the real assets. The MIME types are pinned rather than guessed from
# the system's tables, because a module script is only executed for a JavaScript MIME
# type and `nosniff` never comes off.
PUBLIC_ASSETS = {
    '.js': 'text/javascript',
    '.css': 'text/css',
    '.png': 'image/png',
}


def build_release(directory: Path = ASSETS) -> tuple[str, dict]:
    """The one release every page and asset request agrees on, fixed at startup.

    A single digest over the whole shipped set, each name hashed with its bytes, fed in
    sorted order so the digest is a fact about the directory rather than about the order
    the OS listed it in. It replaces a key that joined two per-file digests and truncated,
    which takes every character from the first — four stylesheet-only releases shipped
    under the key the browser already held — and it now covers the icons too, which never
    moved the old key at all.

    The graph has to move as one version: the page's script tag carries the digest, and
    a static import resolves against the URL of the file importing it, so nothing in the
    graph can reach outside the release it started from. That is also why the snapshot
    never reads the disk again: a browser holding half of an older graph must be refused
    the other half, not handed current bytes under a stale name.

    A directory that cannot be read, or one with nothing shippable in it, used to raise
    at import time — a raw traceback for what is really the same kind of problem as a
    missing password, so it is recorded here and reported by startup_error() the same
    way instead.
    """
    global _asset_error
    files = {}
    digest = hashlib.sha256()
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as error:
        _asset_error = f'could not read the assets directory ({directory}): {error}'
        return '', {}
    for path in entries:
        kind = PUBLIC_ASSETS.get(path.suffix.lower())
        if kind is None or not path.is_file():
            continue
        data = path.read_bytes()
        files[path.name] = (data, kind)
        digest.update(path.name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(data)
        digest.update(b'\0')
    if not files:
        _asset_error = f'no assets found in {directory}; the image may be built incorrectly'
    else:
        # Cleared, not just set on failure: a later, successful call — as when a test
        # calls this directly more than once — must not leave a startup error standing
        # from a directory that was checked before this one and no longer applies.
        _asset_error = ''
    return digest.hexdigest()[:12], files


# Set by build_release() below if the assets directory could not be read at all, or held
# nothing shippable. Checked by startup_error() alongside every other configuration
# problem, rather than left to crash the process before that check ever runs.
_asset_error = ''
RELEASE_DIGEST, RELEASE_FILES = build_release()
RELEASE_BASE = f'/assets/{RELEASE_DIGEST}'


def index_page(csrf: str) -> bytes:
    """The whole interface, addressed by release.

    The page itself is no-store, so what it references is always the current release;
    the assets it names are immutable, because their URL is their content. And the
    script is a module, so its imports resolve inside the same namespace — which is
    what keeps the graph on one version without rewriting anything.
    """
    markup = MARKUP.read_text(encoding='utf-8').replace('__ASSETS__', RELEASE_BASE)
    return (f'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV Retention</title>
<link rel="icon" href="{RELEASE_BASE}/icon-32.png" sizes="32x32" type="image/png">
<link rel="icon" href="{RELEASE_BASE}/icon-16.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="{RELEASE_BASE}/icon-180.png">
<link rel="stylesheet" href="{RELEASE_BASE}/icons.css">
<link rel="stylesheet" href="{RELEASE_BASE}/app.css">
</head><body>
<div id="tv-retention" data-csrf="{csrf}" data-api="/api">
{markup}
</div>
<script type="module" src="{RELEASE_BASE}/app.js"></script>
</body></html>''').encode('utf-8')


LOGIN_PAGE = '''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV Retention</title>
<link rel="icon" href="__ASSETS__/icon-32.png" sizes="32x32" type="image/png">
<style>
  :root { color-scheme: dark light; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 14px/1.4 system-ui, sans-serif; background: #1c1c1c; color: #ddd; }
  form { display: grid; gap: 10px; width: 260px; }
  h1 { font-size: 17px; margin: 0 0 6px; font-weight: 600; }
  input { font: inherit; padding: 7px 9px; border-radius: 4px; color: inherit;
          border: 1px solid rgba(128,128,128,.4); background: transparent; }
  button { font: inherit; font-weight: 600; padding: 8px; border-radius: 4px; border: none;
           cursor: pointer; background: #ff8c2b; color: #1a1a1a; }
  p { margin: 0; color: #c0392b; min-height: 1.4em; font-size: 13px; }
</style>
</head><body>
<form method="post" action="/login">
  <h1>TV Retention</h1>
  <input name="username" placeholder="Username" autocomplete="username" autofocus required>
  <input name="password" type="password" placeholder="Password"
         autocomplete="current-password" required>
  <button type="submit">Sign in</button>
  <p>__ERROR__</p>
</form>
</body></html>'''


def login_page(error: str) -> bytes:
    return (LOGIN_PAGE
            .replace('__ERROR__', error)
            .replace('__ASSETS__', RELEASE_BASE)).encode('utf-8')


# -- the poster proxy -------------------------------------------------------

# Ten times the 250px size the comment below expects. Sonarr answering with something far
# larger than a poster — a misconfigured URL, a proxy's error page, anything that is not
# actually mediacover — must not be buffered into memory just because it arrived with a
# 200 status.
MAX_POSTER_BYTES = 2 * 1024 * 1024

# How long a poster cache is trusted before its instance and series are checked against
# what Sonarr currently holds. A series removed from Sonarr — or a whole instance removed
# from settings — otherwise left its posters on disk forever, since nothing ever asks for
# them again to trigger the per-series cleanup below. Bounded to once a day and run from
# an ordinary request rather than a scan on every one: artwork for a series that is gone
# is not urgent to reclaim, and a browser never waits on it either way.
POSTER_PRUNE_SECONDS = 24 * 3600
_last_poster_prune = 0.0


def prune_orphaned_posters(settings: dict) -> None:
    """Remove cached posters whose instance or series no longer exists.

    Conservative by design: an instance with no cached catalogue yet is left alone rather
    than guessed at, so a sync that has simply not run yet can never look like every one
    of its series was deleted.
    """
    directory = Path(state_dir(settings)) / 'posters'
    if not directory.is_dir():
        return
    instances = {i['id'] for i in settings.get('instances') or []}
    catalogue = main.read_cache(settings, 'catalogue.json')
    known_series = {}
    for path in directory.glob('*.jpg'):
        parts = path.stem.split('-')
        if len(parts) != 3:
            continue
        instance_id, series_id, _stamp = parts
        if instance_id not in instances:
            path.unlink(missing_ok=True)
            continue
        if instance_id not in known_series:
            entry = catalogue.get(instance_id) or {}
            known_series[instance_id] = ({str(s.get('series_id')) for s in entry.get('series') or []}
                                         if entry.get('schema') == main.SCHEMA else None)
        known = known_series[instance_id]
        if known is not None and series_id not in known:
            path.unlink(missing_ok=True)


def poster_bytes(query: dict) -> tuple[int, bytes]:
    """One poster, through the plugin rather than from it.

    Sonarr already stores every one, so this streams a copy through and keeps it on disk:
    a grid asks for fifty at once and Sonarr should be asked once. The browser never needs
    an API key, and the plugin never keeps a second copy of a 3.6 GB library.
    """
    series = int(query.get('series', ['0'])[0] or 0)
    wanted = query.get('instance', [''])[0]
    if series <= 0 or not wanted:
        return 400, b'No series'
    # It becomes part of a filename, so its shape is checked before it becomes one. Ids are
    # generated as twelve hex characters; the lookup below would reject anything unknown
    # anyway, but a path is not where that should be discovered.
    if not all(character in '0123456789abcdef' for character in wanted) or len(wanted) > 32:
        return 404, b'No such Sonarr instance'

    settings = load_settings()
    instance = next((i for i in settings.get('instances') or [] if i.get('id') == wanted), None)
    if not instance:
        return 404, b'No such Sonarr instance'

    global _last_poster_prune
    now = time.monotonic()
    if now - _last_poster_prune > POSTER_PRUNE_SECONDS:
        _last_poster_prune = now
        with contextlib.suppress(OSError):
            prune_orphaned_posters(settings)

    # Sonarr's own artwork path carries its last-write marker, so a new picture is a new
    # file. Keyed on the series alone, the first poster ever fetched was served for good.
    stamp = hashlib.md5(query.get('stamp', [''])[0].encode('utf-8')).hexdigest()[:12]
    directory = Path(state_dir(settings)) / 'posters'
    prefix = directory / f'{wanted}-{series}'
    cached = Path(f'{prefix}-{stamp}.jpg')
    if cached.is_file() and cached.stat().st_size:
        return 200, cached.read_bytes()

    directory.mkdir(parents=True, exist_ok=True)
    # 250px: eight kilobytes against sixty for the full size, and a card is smaller than
    # either. The larger ones stay in Sonarr, where they already are.
    url = f"{instance['url'].rstrip('/')}/api/v3/mediacover/{series}/poster-250.jpg"
    request = urllib.request.Request(url)
    request.add_header('X-Api-Key', instance['api_key'])
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            # One byte past the cap is enough to know it is oversized, without reading an
            # unbounded body into memory to find out.
            body = response.read(MAX_POSTER_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return 404, b''
    if not body or len(body) > MAX_POSTER_BYTES:
        return 404, b''
    # Whatever this series looked like before. Left behind, every artwork change would add
    # a file and remove none.
    for stale in directory.glob(f'{wanted}-{series}-*.jpg'):
        stale.unlink(missing_ok=True)
    cached.write_bytes(body)
    return 200, body


# -- the handler ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'TVRetention'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass          # the worker's own log is the record; this would double every line

    # -- helpers
    def send(self, status: int, body: bytes, content_type: str, headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode('utf-8')
        self.send(status, body, 'application/json; charset=utf-8',
                  [('Cache-Control', 'no-store')])

    def read_form(self) -> dict:
        """The whole body, always, before anything is decided about the request.

        On a keep-alive connection an unread body is not discarded — it stays in the socket
        and becomes the first bytes the server reads as the *next* request. A 401 that
        returned without reading turned the following POST into

            Unsupported method ('csrf_token=...&payload=%7B...%7DGET')

        which is the leftover body with the next request line stuck to the end of it. So
        this is called first and unconditionally, and an oversized body closes the
        connection rather than leaving a megabyte of it behind.
        """
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            self.close_connection = True
            return {}
        raw = self.rfile.read(length).decode('utf-8', 'replace') if length else ''
        return urllib.parse.parse_qs(raw)

    def session(self):
        return session_for(self.headers.get('Cookie'))

    # -- routes
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if route == '/health':
            return self.send_json(200, {'ok': True})

        if route.startswith('/assets/'):
            return self.serve_asset(route[len('/assets/'):])

        session = self.session()
        if route == '/login':
            if session:
                return self.redirect('/')
            return self.send(200, login_page(''), 'text/html; charset=utf-8',
                             [('Cache-Control', 'no-store')])

        if not session:
            return self.redirect('/login')

        if route == '/poster':
            status, body = poster_bytes(query)
            if status != 200:
                return self.send(status, body, 'text/plain; charset=utf-8')
            # A poster is addressed by a stamp that changes when the artwork does, so the
            # browser may keep it as long as it likes.
            return self.send(200, body, 'image/jpeg',
                             [('Cache-Control', 'private, max-age=604800, immutable')])

        if route == '/':
            return self.send(200, index_page(session['csrf']), 'text/html; charset=utf-8',
                             [('Cache-Control', 'no-store')])
        return self.send(404, b'Not found', 'text/plain; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        # Read first, decide second. See read_form: a body left unread poisons the next
        # request on the same connection, and every early return below is a way to leave
        # one behind.
        form = self.read_form()
        if route == '/login':
            return self.handle_login(form)
        if route == '/logout':
            return self.handle_logout()
        if route == '/api':
            return self.handle_api(form)
        return self.send(404, b'Not found', 'text/plain; charset=utf-8')

    def serve_asset(self, name: str):
        """The release namespace first, then the flat names that predate it.

        Every lookup goes through the startup snapshot, so membership in it is the
        traversal guard: no path, backslash or dot-prefixed string can be a key. The
        digest is compared for equality rather than pattern-matched, which pins its
        shape along with its value — anything but this exact release is refused.
        """
        digest, separator, filename = name.partition('/')
        if separator:
            if digest != RELEASE_DIGEST or filename not in RELEASE_FILES:
                # A digest the server does not hold must fail rather than serve the
                # current release: the caller is a browser holding part of an older
                # module graph, and completing it from a newer one would run a mixed
                # version. Not cacheable either, so the failure is not remembered.
                return self.send(404, b'Not found', 'text/plain; charset=utf-8',
                                 [('Cache-Control', 'no-store')])
            data, kind = RELEASE_FILES[filename]
            # The URL is the content, so it may be kept as long as the browser likes.
            return self.send(200, data, kind,
                             [('Cache-Control', 'public, max-age=31536000, immutable')])
        if name not in RELEASE_FILES:
            return self.send(404, b'Not found', 'text/plain; charset=utf-8',
                             [('Cache-Control', 'no-store')])
        # Flat names, served for whatever still references them. Nothing the interface
        # loads does: the module graph lives in the namespace above, and the page and
        # the login screen name the release explicitly.
        data, kind = RELEASE_FILES[name]
        self.send(200, data, kind, [('Cache-Control', 'public, max-age=604800')])

    def redirect(self, where: str):
        self.send_response(303)
        self.send_header('Location', where)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def handle_login(self, form):
        global _failures
        username = (form.get('username') or [''])[0]
        password = (form.get('password') or [''])[0]
        if OPEN or not credentials_match(username, password):
            # A flat delay rather than a lockout: it makes guessing impractical without
            # giving anyone a way to lock the operator out of their own tool. The counter
            # is shared across every connection this threaded server handles, so it is
            # guarded by the same lock _sessions already uses — the sleep itself happens
            # outside it, so one slow attempt cannot serialise every other one.
            with _lock:
                _failures += 1
                delay = min(2.0, 0.25 * _failures)
            time.sleep(delay)
            return self.send(401, login_page('Wrong username or password.'),
                             'text/html; charset=utf-8', [('Cache-Control', 'no-store')])
        with _lock:
            _failures = 0
        session = open_session()
        cookie = (f'{COOKIE}={session["token"]}; Path=/; HttpOnly; SameSite=Strict; '
                  f'Max-Age={SESSION_HOURS * 3600}')
        self.send_response(303)
        self.send_header('Location', '/')
        self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def handle_logout(self):
        jar = http.cookies.SimpleCookie()
        with contextlib.suppress(http.cookies.CookieError):
            jar.load(self.headers.get('Cookie') or '')
        morsel = jar.get(COOKIE)
        if morsel:
            with _lock:
                _sessions.pop(morsel.value, None)
        self.send_response(303)
        self.send_header('Location', '/login')
        self.send_header('Set-Cookie', f'{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def handle_api(self, form):
        session = self.session()
        if not session:
            # `expired: true` so the page can reload itself into the login rather than
            # showing an error about a session the reader cannot do anything about.
            return self.send_json(401, {'ok': False, 'expired': True,
                                        'error': 'Session expired. Reload the page.'})
        token = (form.get('csrf_token') or [''])[0]
        if not OPEN and not hmac.compare_digest(token, session['csrf']):
            return self.send_json(403, {'ok': False, 'expired': True,
                                        'error': 'Session token expired. Reload the page.'})
        try:
            request = json.loads((form.get('payload') or [''])[0] or '{}')
        except json.JSONDecodeError:
            return self.send_json(400, {'ok': False, 'error': 'Malformed request'})
        if not isinstance(request, dict):
            return self.send_json(400, {'ok': False, 'error': 'Malformed request'})
        result = dispatch(request)
        self.send_json(200 if result.get('ok') else 409, result)


def take_the_volume() -> None:
    """Own /config, then stop being root.

    A bind-mounted directory arrives owned by whoever created it, which is usually root,
    and a container running as a fixed user then cannot write to its own volume. The
    conventional answer is to tell people to go and chown a directory before the thing will
    start, which is a bad first five minutes and is the reason this project exists in the
    first place — the author bounced off a neighbour's install for exactly that.

    So: start as root if that is how we were run, take the volume, and immediately drop to
    PUID/PGID. Started with `user:` already set, there is nothing to do and nothing to drop.
    """
    if os.geteuid() != 0:
        return
    # 1000:1000 is the general default. Unraid's is 99:100 — `nobody:users`, which owns
    # everything under /mnt/user — and its template sets that. It matters less here than
    # for its neighbours: this owns one config directory and never touches a library, so
    # there is no shared media ownership to get wrong.
    # Parsed at import, alongside PORT and SESSION_HOURS, and already checked by
    # startup_error() — a malformed value never reaches here at all.
    uid, gid = PUID, PGID
    os.umask(UMASK)
    root = Path(env('TVR_CONFIG_DIR', '/config'))
    root.mkdir(parents=True, exist_ok=True)
    # Only what is not already right: a poster cache of three thousand files does not need
    # walking on every start.
    if root.stat().st_uid != uid or root.stat().st_gid != gid:
        for path in [root, *root.rglob('*')]:
            with contextlib.suppress(OSError):
                os.chown(path, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def serve() -> int:
    """One process: the schedule in a thread behind it, the interface in front.

    Not two containers and not a supervisor. The worker is the reason this exists and it
    runs whether or not anybody ever opens the page — the same way Sonarr downloads without
    anyone logged in. Authentication guards the interface, never the work.
    """
    problem = startup_error()
    if problem:
        print(problem)
        return 1
    take_the_volume()
    worker = threading.Thread(target=main.serve_forever, name='worker', daemon=True)
    worker.start()
    server = ThreadingHTTPServer(('', PORT), Handler)
    server.daemon_threads = True
    print(f'TV Retention listening on :{PORT}'
          + ('  (no authentication: TVR_AUTH=none)' if OPEN else ''), flush=True)
    server.serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(serve())
