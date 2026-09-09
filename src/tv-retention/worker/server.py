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
import mimetypes
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


PORT = int(env('TVR_PORT', '8787'))
USERNAME = env('TVR_USERNAME')
PASSWORD = env('TVR_PASSWORD')
# The only way to run without a login, and it is a value nobody can forge. Sonarr kept a
# "disabled for local addresses" mode and it became CVE-2026-30975: a caller spoofed
# X-Forwarded-For to look local and skipped authentication entirely. A header cannot be
# trusted to say where a request came from; an environment variable can be trusted to say
# what the operator chose.
OPEN = env('TVR_AUTH').lower() == 'none'
SESSION_HOURS = int(env('TVR_SESSION_HOURS', '720'))

MAX_BODY = 1024 * 1024
COOKIE = 'tvr_session'


def startup_error() -> str:
    """What is wrong with the configuration, or an empty string.

    Refusing to start is deliberate. The alternative — run, but nobody can log in — leaves
    the half that deletes running unsupervised while the half that would notice is locked
    away, and the realistic way to reach it is a typo in a compose file six months from now.
    """
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

def asset_key() -> str:
    """A cache key over both assets, hashed together.

    Joining two digests and truncating takes every character from the first, which is how
    four stylesheet-only releases shipped under the key the browser already held.
    """
    digest = hashlib.sha256()
    for name in ('app.js', 'app.css', 'icons.css'):
        with open(ASSETS / name, 'rb') as handle:
            digest.update(handle.read())
    return digest.hexdigest()[:12]


def index_page(csrf: str) -> bytes:
    key = asset_key()
    return (f'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV Retention</title>
<link rel="stylesheet" href="/assets/icons.css?v={key}">
<link rel="stylesheet" href="/assets/app.css?v={key}">
</head><body>
<div id="tv-retention" data-csrf="{csrf}" data-api="/api">
{MARKUP.read_text(encoding='utf-8')}
</div>
<script src="/assets/app.js?v={key}"></script>
</body></html>''').encode('utf-8')


LOGIN_PAGE = '''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV Retention</title>
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


# -- the poster proxy -------------------------------------------------------

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
            body = response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return 404, b''
    if not body:
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
            return self.send(200, LOGIN_PAGE.replace('__ERROR__', '').encode('utf-8'),
                             'text/html; charset=utf-8', [('Cache-Control', 'no-store')])

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
        # No traversal: one flat directory, and the name may not describe a path at all.
        if '/' in name or '\\' in name or name.startswith('.'):
            return self.send(404, b'Not found', 'text/plain; charset=utf-8')
        path = ASSETS / name
        if not path.is_file():
            return self.send(404, b'Not found', 'text/plain; charset=utf-8')
        kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        # Addressed with a content hash, so a change is a different URL.
        self.send(200, path.read_bytes(), kind,
                  [('Cache-Control', 'public, max-age=604800')])

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
            # giving anyone a way to lock the operator out of their own tool.
            _failures += 1
            time.sleep(min(2.0, 0.25 * _failures))
            page = LOGIN_PAGE.replace('__ERROR__', 'Wrong username or password.')
            return self.send(401, page.encode('utf-8'), 'text/html; charset=utf-8',
                             [('Cache-Control', 'no-store')])
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
    uid, gid = int(env('PUID', '1000')), int(env('PGID', '1000'))
    os.umask(int(env('UMASK', '022'), 8))
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
