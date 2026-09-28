"""The HTTP front end: what it refuses, what it guards, and what it lets through.

These exercise the pieces that used to be someone else's problem. The plugin was handed
authentication and a CSRF token by emhttp and never had to be right about either.
"""
import os
import unittest
from pathlib import Path

import context  # noqa: F401


def load(**environment):
    """Import server.py with a given environment, freshly each time.

    The configuration is read at import, which is what makes "refuse to start" possible at
    all — so a test that wants a different configuration needs a different import.
    """
    import importlib
    import sys
    previous = {key: os.environ.get(key) for key in
                ('TVR_USERNAME', 'TVR_PASSWORD', 'TVR_AUTH', 'TVR_PORT', 'TVR_SESSION_HOURS',
                 'PUID', 'PGID', 'UMASK')}
    os.environ.update({key: '' for key in previous})
    os.environ.update(environment)
    try:
        sys.modules.pop('server', None)
        return importlib.import_module('server')
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Startup(unittest.TestCase):
    def test_it_refuses_to_start_with_no_login_configured(self):
        """Rather than running with the interface locked away.

        That combination leaves the half that deletes running unsupervised while the half
        that would notice is unreachable, and the realistic way to arrive there is a typo
        in a compose file months later.
        """
        server = load(TVR_USERNAME='', TVR_PASSWORD='')
        problem = server.startup_error()
        self.assertIn('will not start', problem)
        self.assertIn('TVR_AUTH=none', problem, 'it must say how to run open on purpose')

    def test_running_open_is_possible_and_explicit(self):
        server = load(TVR_AUTH='none')
        self.assertEqual(server.startup_error(), '')
        self.assertTrue(server.OPEN)

    def test_a_header_can_never_turn_authentication_off(self):
        """Sonarr kept a "disabled for local addresses" mode and it became CVE-2026-30975.

        X-Forwarded-For was spoofed to look local and skip authentication. Only an
        environment variable decides here, and nothing on the wire can forge one.
        """
        source = (context.ROOT / 'src' / 'worker' / 'server.py').read_text()
        # Where the request claims to come from is never read, in any form.
        self.assertNotIn("X-Forwarded-For')", source)
        self.assertNotIn('X-Real-IP', source)
        self.assertNotIn('client_address', source)
        self.assertIn("OPEN = env('TVR_AUTH').lower() == 'none'", source)

    def test_a_short_password_is_refused(self):
        self.assertIn('at least 8', load(TVR_USERNAME='a', TVR_PASSWORD='short').startup_error())

    def test_a_variable_set_to_nothing_is_a_variable_that_is_not_set(self):
        """`TVR_PORT=` is a realistic way to write it in a compose file.

        os.environ.get's default applies to a variable that is absent, not to one set to
        the empty string, so int('') took the container down before it served anything.
        """
        server = load(TVR_AUTH='none', TVR_PORT='', TVR_SESSION_HOURS='')
        self.assertEqual(server.PORT, 8787)
        self.assertEqual(server.SESSION_HOURS, 720)

    def test_a_malformed_port_is_a_clear_startup_error_not_a_crash(self):
        # int('abc') used to raise uncaught at import time, taking the whole process
        # down with a traceback instead of the message every other bad setting gets.
        server = load(TVR_AUTH='none', TVR_PORT='abc')
        self.assertEqual(server.PORT, 8787, 'the fallback keeps the module importable')
        problem = server.startup_error()
        self.assertIn('TVR_PORT', problem)
        self.assertIn('abc', problem)

    def test_a_malformed_session_hours_is_a_clear_startup_error(self):
        server = load(TVR_AUTH='none', TVR_SESSION_HOURS='forever')
        self.assertEqual(server.SESSION_HOURS, 720)
        self.assertIn('TVR_SESSION_HOURS', server.startup_error())

    def test_a_malformed_puid_is_a_clear_startup_error(self):
        server = load(TVR_AUTH='none', PUID='not-a-number')
        self.assertEqual(server.PUID, 1000)
        self.assertIn('PUID', server.startup_error())

    def test_a_malformed_pgid_is_a_clear_startup_error(self):
        server = load(TVR_AUTH='none', PGID='not-a-number')
        self.assertEqual(server.PGID, 1000)
        self.assertIn('PGID', server.startup_error())

    def test_a_malformed_umask_is_a_clear_startup_error(self):
        server = load(TVR_AUTH='none', UMASK='not-octal')
        self.assertEqual(server.UMASK, 0o22)
        self.assertIn('UMASK', server.startup_error())

    def test_well_formed_puid_pgid_and_umask_still_work(self):
        server = load(TVR_AUTH='none', PUID='99', PGID='100', UMASK='002')
        self.assertEqual(server.PUID, 99)
        self.assertEqual(server.PGID, 100)
        self.assertEqual(server.UMASK, 0o002)
        self.assertEqual(server.startup_error(), '')

    def test_running_open_does_not_hide_a_malformed_integer(self):
        # TVR_AUTH=none skips the login check; it must not also skip this one.
        server = load(TVR_AUTH='none', TVR_PORT='not-a-port')
        self.assertNotEqual(server.startup_error(), '')



class Sessions(unittest.TestCase):
    def setUp(self):
        self.server = load(TVR_USERNAME='someone', TVR_PASSWORD='a-long-enough-password')

    def test_no_cookie_is_no_session(self):
        self.assertIsNone(self.server.session_for(None))
        self.assertIsNone(self.server.session_for('tvr_session=nonsense'))

    def test_a_session_carries_its_own_csrf_token(self):
        # Per session, not per process: a token shared by everyone is not a token.
        first = self.server.open_session()
        second = self.server.open_session()
        self.assertNotEqual(first['csrf'], second['csrf'])
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(self.server.session_for(f'tvr_session={first["token"]}')['csrf'],
                         first['csrf'])

    def test_an_expired_session_is_not_a_session(self):
        session = self.server.open_session()
        self.server._sessions[session['token']]['expires'] = 0
        self.assertIsNone(self.server.session_for(f'tvr_session={session["token"]}'))

    def test_credentials_are_compared_in_constant_time(self):
        self.assertTrue(self.server.credentials_match('someone', 'a-long-enough-password'))
        self.assertFalse(self.server.credentials_match('someone', 'wrong'))
        self.assertFalse(self.server.credentials_match('wrong', 'a-long-enough-password'))
        source = (context.ROOT / 'src' / 'worker' / 'server.py').read_text()
        # `==` on the username leaks whether it was right, which is half the secret.
        self.assertIn('hmac.compare_digest(username, USERNAME)', source)


class Assets(unittest.TestCase):
    def setUp(self):
        self.server = load(TVR_AUTH='none')

    def test_the_page_carries_the_session_token_and_the_api_path(self):
        page = self.server.index_page('a-token').decode()
        self.assertIn('data-csrf="a-token"', page)
        self.assertIn('data-api="/api"', page)
        self.assertIn('id="tv-retention"', page)

    def test_the_page_addresses_every_asset_by_release(self):
        """One digest in every URL, so the module graph cannot straddle versions.

        The script is a module and its imports resolve against its own URL, which is
        why the entry must carry the digest: an unversioned child would let the
        browser serve a week-old file inside a current graph.
        """
        page = self.server.index_page('a-token').decode()
        base = self.server.RELEASE_BASE
        for name in ('app.js', 'app.css', 'icons.css',
                     'icon-16.png', 'icon-32.png', 'icon-180.png'):
            self.assertIn(f'{base}/{name}', page, f'{name} is not addressed by release')
        self.assertIn('<script type="module"', page)
        self.assertIn(f'src="{base}/icon-64.png"', page, 'the markup icon was not rewritten')
        self.assertNotIn('__ASSETS__', page, 'a placeholder reached the browser')
        self.assertNotIn('/assets/app.js?', page, 'a query-stringed URL survived')

    def test_the_login_page_is_addressed_by_release_too(self):
        page = self.server.login_page('').decode()
        self.assertIn(f'{self.server.RELEASE_BASE}/icon-32.png', page)
        self.assertNotIn('__ASSETS__', page)
        self.assertNotIn('__ERROR__', page)

    def test_the_release_covers_the_assets_the_interface_names(self):
        for name in ('app.js', 'app.css', 'icons.css',
                     'icon-16.png', 'icon-32.png', 'icon-64.png', 'icon-180.png'):
            self.assertIn(name, self.server.RELEASE_FILES, f'{name} is not shipped')
        self.assertRegex(self.server.RELEASE_DIGEST, r'^[0-9a-f]{12}$')

    def test_every_icon_the_interface_uses_is_one_we_ship(self):
        """The plugin borrowed Unraid's Font Awesome, which is not ours and is not there.

        An icon name with no glyph behind it renders as an empty, zero-sized element: the
        button is present and there is nothing to click.
        """
        import re
        assets = context.ROOT / 'src' / 'assets'
        markup = (context.ROOT / 'src' / 'include' / 'interface.html').read_text(encoding='utf-8')
        # Every module, not just the entry: an icon is asked for wherever the element that
        # carries it is built, and moving that code to another module does not ship a glyph.
        scripts = ''.join(path.read_text(encoding='utf-8')
                          for path in sorted(assets.glob('*.js')))
        wanted = set(re.findall(r'fa fa-([a-z-]+)', scripts + markup))
        wanted = {name for name in wanted if not name.endswith('-')}   # built from a template
        icons = (assets / 'icons.css').read_text(encoding='utf-8')
        for name in sorted(wanted):
            self.assertIn(f'.fa-{name} {{', icons, f'fa-{name} has no glyph')


class Release(unittest.TestCase):
    """The digest itself: what moves it, and what is not part of it.

    One digest over the whole shipped set is what makes a release atomic. The old key
    hashed three named files and joined them, so a stylesheet-only change could move it
    while an icon change never could — and either way, versioning only the entry script
    leaves a module graph free to straddle versions.
    """

    def setUp(self):
        self.server = load(TVR_AUTH='none')

    def release(self, base):
        import tempfile
        directory = Path(base) / 'assets'
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def test_the_digest_moves_when_any_shipped_file_does(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            directory = self.release(base)
            (directory / 'app.js').write_text('one')
            (directory / 'app.css').write_text('one')
            (directory / 'icon-32.png').write_bytes(b'one')
            digest, files = self.server.build_release(directory)
            self.assertEqual(sorted(files), ['app.css', 'app.js', 'icon-32.png'])
            self.assertEqual(self.server.build_release(directory)[0], digest)

            def moved(**edits):
                for name, content in edits.items():
                    (directory / name).write_bytes(content)
                return self.server.build_release(directory)[0] != digest

            self.assertTrue(moved(**{'app.js': b'two'}), 'a script change must move it')
            self.assertTrue(moved(**{'app.css': b'two'}), 'a stylesheet change must move it')
            self.assertTrue(moved(**{'icon-32.png': b'two'}),
                            'an icon change must move it — PNGs never moved the old key')

    def test_adding_or_removing_a_shipped_file_moves_the_digest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            directory = self.release(base)
            (directory / 'app.js').write_text('one')
            digest = self.server.build_release(directory)[0]
            (directory / 'dom.js').write_text('new module')
            self.assertNotEqual(self.server.build_release(directory)[0], digest)
            (directory / 'dom.js').unlink()
            self.assertEqual(self.server.build_release(directory)[0], digest)

    def test_a_file_of_an_unsupported_type_is_not_part_of_the_release(self):
        """A stray backup or notes file must not become fetchable, nor move the digest.

        The release is an allowlist, not a directory listing: the interface ships
        scripts, styles and icons, and nothing else is public whatever it is named.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            directory = self.release(base)
            (directory / 'app.js').write_text('one')
            (directory / 'notes.txt').write_text('stray')
            (directory / 'app.js~').write_text('backup')
            (directory / 'icon.svg').write_text('<svg/>')
            digest, files = self.server.build_release(directory)
            self.assertEqual(sorted(files), ['app.js'])

            bare = Path(base) / 'bare'
            bare.mkdir()
            (bare / 'app.js').write_text('one')
            self.assertEqual(self.server.build_release(bare)[0], digest,
                             'an unsupported file changed the digest')

    def test_a_missing_assets_directory_is_a_clear_startup_error_not_a_crash(self):
        # Path.iterdir() on a directory that does not exist used to raise uncaught at
        # import time, taking the whole process down with a traceback instead of the
        # message every other configuration problem gets.
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / 'does-not-exist'
            digest, files = self.server.build_release(missing)
            self.assertEqual((digest, files), ('', {}))
            problem = self.server.startup_error()
            self.assertIn('assets directory', problem)
            self.assertIn(str(missing), problem)

    def test_an_assets_directory_with_nothing_shippable_is_a_clear_startup_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            directory = self.release(base)
            (directory / 'notes.txt').write_text('nothing this server will serve')
            digest, files = self.server.build_release(directory)
            self.assertEqual(files, {})
            self.assertIn('no assets found', self.server.startup_error())

    def test_a_healthy_directory_after_a_broken_one_clears_the_startup_error(self):
        # The error must describe the directory just checked, not the last one that
        # happened to fail — a transient problem must not become a standing one.
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            self.server.build_release(Path(base) / 'does-not-exist')
            self.assertNotEqual(self.server.startup_error(), '')
            directory = self.release(base)
            (directory / 'app.js').write_text('one')
            self.server.build_release(directory)
            self.assertEqual(self.server.startup_error(), '')

    def test_the_real_shipped_assets_build_without_a_startup_error(self):
        # The fix must not turn a healthy image into one that refuses to start.
        digest, files = self.server.build_release(self.server.ASSETS)
        self.assertTrue(files)
        self.assertEqual(self.server.startup_error(), '')


class Worker(unittest.TestCase):
    def test_the_schedule_runs_whether_or_not_anyone_is_logged_in(self):
        """Authentication guards the interface, never the work.

        Sonarr downloads without anyone logged in; this deletes on its schedule the same
        way. A login is for looking at it and changing it.
        """
        source = (context.ROOT / 'src' / 'worker' / 'server.py').read_text()
        block = source.split('def serve()')[1]
        self.assertIn('threading.Thread(target=main.serve_forever', block)
        self.assertIn('daemon=True', block)
        # Started before the socket opens, so a container with nobody watching still works.
        self.assertLess(block.index('worker.start()'), block.index('http_thread.start()'))


if __name__ == '__main__':
    unittest.main()


class Serves(unittest.TestCase):
    """A real socket, because the faults worth proving here live on one.

    A string match proves what the source says; a request proves what the server does.
    """

    def start_http(self, module):
        import threading
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        """One GET, answered whole: status line, headers and body.

        `Connection: close` ends the stream, so nothing depends on reading the exact
        number of bytes the response promised.
        """
        import socket
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
            sock.sendall(f'GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n'.encode())
            raw = b''
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        head, _, body = raw.partition(b'\r\n\r\n')
        lines = head.decode('utf-8', 'replace').split('\r\n')
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()
        return lines[0], headers, body

    def post(self, sock, reader, body):
        """One request, and exactly one response read back off the stream.

        Reading with a bare recv() is what makes this test lie: the response arrives in
        whatever pieces the kernel felt like, so the next read picks up the tail of the
        last one and the failure looks like the server's rather than the test's.
        """
        request = (f'POST /api HTTP/1.1\r\nHost: x\r\nContent-Type: '
                   f'application/x-www-form-urlencoded\r\nContent-Length: {len(body)}\r\n\r\n{body}')
        sock.sendall(request.encode())
        status = reader.readline().decode('utf-8', 'replace')
        length = 0
        while True:
            line = reader.readline().decode('utf-8', 'replace')
            if line in ('\r\n', '\n', ''):
                break
            name, _, value = line.partition(':')
            if name.strip().lower() == 'content-length':
                length = int(value.strip())
        return status, reader.read(length).decode('utf-8', 'replace')


class ReleaseRoutes(Serves):
    """What the wire says about the release namespace.

    These are behavioural because the guarantee is behavioural: a module graph is
    served one version or it is refused, and only the response headers can prove
    which of those a browser would see.
    """

    def setUp(self):
        self.server = load(TVR_AUTH='none')
        self.start_http(self.server)

    def test_a_namespaced_asset_is_served_immutable(self):
        digest = self.server.RELEASE_DIGEST
        for name, kind in (('app.js', 'text/javascript'), ('app.css', 'text/css'),
                           ('icons.css', 'text/css'), ('icon-32.png', 'image/png')):
            status, headers, body = self.get(f'/assets/{digest}/{name}')
            self.assertIn('200', status, name)
            self.assertEqual(body, self.server.RELEASE_FILES[name][0], name)
            # A module script under nosniff is executed only for a JavaScript MIME
            # type, so the type is pinned rather than asked of the system's tables.
            self.assertEqual(headers['content-type'], kind, name)
            self.assertEqual(headers['cache-control'], 'public, max-age=31536000, immutable', name)

    def test_a_digest_the_server_does_not_hold_is_refused(self):
        """A browser holding half an old graph must not be handed the current release.

        Serving current bytes under a stale name would complete the graph from a newer
        version and run the mixture. Refusing fails safe: the page reports the error,
        and a reload picks up the new release whole — and the refusal is no-store, so
        it is not what gets remembered.
        """
        first = self.server.RELEASE_DIGEST[0]
        stale = ('0' if first != '0' else '1') + self.server.RELEASE_DIGEST[1:]
        status, headers, body = self.get(f'/assets/{stale}/app.js')
        self.assertIn('404', status)
        self.assertEqual(headers.get('cache-control'), 'no-store')
        self.assertNotIn('location', headers, 'a stale digest must not redirect')
        self.assertEqual(body, b'Not found')

    def test_a_name_the_release_does_not_hold_is_refused(self):
        """Membership in the startup snapshot is the traversal guard.

        Nothing containing a separator, a dot prefix or an unsupported type can be a
        key in it, so each of these must fall at the same wall.
        """
        digest = self.server.RELEASE_DIGEST
        for path in (f'/assets/{digest}/nope.js',        # unknown file
                     f'/assets/{digest}/app.js/x',       # a path, not a name
                     f'/assets/{digest}/../app.js',      # traversal
                     f'/assets/{digest}/..%5capp.js',    # encoded traversal
                     '/assets/.hidden',                  # dot-prefixed, flat
                     '/assets/app.js~',                  # unsupported type, flat
                     '/assets/'):                        # nothing at all
            status, headers, _ = self.get(path)
            self.assertIn('404', status, path)
            self.assertEqual(headers.get('cache-control'), 'no-store', path)

    def test_flat_asset_names_still_serve_for_compatibility(self):
        """The names the page used before the namespace, for whatever still holds them.

        Nothing the interface loads references them: the module graph lives in the
        namespace, and the page and login name the release explicitly.
        """
        status, headers, body = self.get('/assets/app.js')
        self.assertIn('200', status)
        self.assertEqual(body, self.server.RELEASE_FILES['app.js'][0])
        self.assertEqual(headers['cache-control'], 'public, max-age=604800')
        self.assertNotIn('immutable', headers['cache-control'])

    def test_served_bytes_come_from_the_startup_snapshot_not_the_disk(self):
        """A digest already handed out keeps meaning the bytes it was computed from.

        The files are baked into the image, so disk and snapshot cannot drift in
        production — this guards the design anyway, because the failure mode is the
        bad one: a half-old module graph assembled from a disk that moved.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base) / 'assets'
            directory.mkdir()
            (directory / 'app.js').write_bytes(b'console.log(1)\n')
            digest, files = self.server.build_release(directory)
            self.server.RELEASE_DIGEST, self.server.RELEASE_FILES = digest, files
            status, _, body = self.get(f'/assets/{digest}/app.js')
            self.assertIn('200', status)
            self.assertEqual(body, b'console.log(1)\n')

            (directory / 'app.js').write_bytes(b'console.log(2)\n')
            status, _, body = self.get(f'/assets/{digest}/app.js')
            self.assertIn('200', status)
            self.assertEqual(body, b'console.log(1)\n', 'the disk moved under a live digest')
            self.assertNotEqual(self.server.build_release(directory)[0], digest,
                                'a rebuilt release did not notice the change')


class KeepAlive(Serves):
    """Two requests down one connection, where the first one is rejected.

    A string-matching test would not have found this. It needs a real socket, because the
    fault is entirely in what is left in it.
    """

    def setUp(self):
        self.server_module = load(TVR_USERNAME='someone', TVR_PASSWORD='a-long-enough-password')
        self.start_http(self.server_module)

    def test_a_rejected_request_does_not_poison_the_next_one(self):
        """The body of a 401 stayed in the socket and became the next request line.

            Unsupported method ('csrf_token=...&payload=%7B...%7DGET')

        Which is the leftover body with the following request stuck to the end of it. It
        showed up the moment the container was restarted, because sessions live in memory
        and every open page then had a cookie for a session that was gone.
        """
        import socket
        body = 'csrf_token=stale&payload=%7B%22action%22%3A%22watch%22%7D'
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
            reader = sock.makefile('rb')
            first, _ = self.post(sock, reader, body)
            self.assertIn('401', first)
            second, _ = self.post(sock, reader, body)
        self.assertIn('401', second, 'the connection was left dirty')
        self.assertNotIn('501', second)
        self.assertNotIn('Unsupported method', second)

    def test_the_page_is_told_the_session_is_gone_rather_than_that_something_broke(self):
        import json as jsonlib
        import socket
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
            _, body = self.post(sock, sock.makefile('rb'), 'csrf_token=stale&payload=%7B%7D')
        self.assertTrue(jsonlib.loads(body)['expired'])
        scripts = ''.join(path.read_text(encoding='utf-8') for path
                          in sorted((context.ROOT / 'src' / 'assets').glob('*.js')))
        self.assertIn("if (data.expired) { window.location.href = '/login';", scripts)

    def test_an_oversized_body_closes_the_connection_instead_of_being_left_behind(self):
        source = (context.ROOT / 'src' / 'worker' / 'server.py').read_text()
        block = source.split('def read_form')[1].split('def ')[0]
        self.assertIn('self.close_connection = True', block)


class Posters(unittest.TestCase):
    """The poster cache: bounded per response, and pruned of what Sonarr no longer holds."""

    def setUp(self):
        import tempfile
        self.server = load(TVR_AUTH='none')
        self.temp = tempfile.TemporaryDirectory()
        self.settings = {'state_dir': str(Path(self.temp.name) / 'state'),
                         'instances': [{'id': 'aaaaaaaaaaaa', 'name': 'Sonarr',
                                       'url': 'http://sonarr.test', 'api_key': 'k'}]}
        self.original_load_settings = self.server.load_settings
        self.server.load_settings = lambda: self.settings
        self.posters = Path(self.settings['state_dir']) / 'posters'
        self.posters.mkdir(parents=True)

    def tearDown(self):
        self.server.load_settings = self.original_load_settings
        self.temp.cleanup()

    def poster_file(self, instance_id, series_id, stamp='abc'):
        path = self.posters / f'{instance_id}-{series_id}-{stamp}.jpg'
        path.write_bytes(b'fake poster bytes')
        return path

    def test_a_poster_for_a_removed_instance_is_pruned(self):
        gone = self.poster_file('bbbbbbbbbbbb', 1)
        self.server.prune_orphaned_posters(self.settings)
        self.assertFalse(gone.exists())

    def test_a_poster_for_a_series_no_longer_in_the_cached_catalogue_is_pruned(self):
        gone = self.poster_file('aaaaaaaaaaaa', 999)
        self.server.main.write_cache(
            self.settings, 'catalogue.json',
            {'aaaaaaaaaaaa': {'schema': self.server.main.SCHEMA,
                              'series': [{'series_id': 1}, {'series_id': 2}]}})
        self.server.prune_orphaned_posters(self.settings)
        self.assertFalse(gone.exists())

    def test_a_poster_for_a_series_still_in_the_catalogue_survives(self):
        kept = self.poster_file('aaaaaaaaaaaa', 1)
        self.server.main.write_cache(
            self.settings, 'catalogue.json',
            {'aaaaaaaaaaaa': {'schema': self.server.main.SCHEMA, 'series': [{'series_id': 1}]}})
        self.server.prune_orphaned_posters(self.settings)
        self.assertTrue(kept.exists())

    def test_an_instance_with_no_cached_catalogue_yet_is_left_alone(self):
        # A sync that has simply not run yet must not look like every series was deleted.
        kept = self.poster_file('aaaaaaaaaaaa', 1)
        self.server.prune_orphaned_posters(self.settings)
        self.assertTrue(kept.exists())

    def test_poster_bytes_rejects_a_response_larger_than_the_cap(self):
        from unittest import mock

        class Oversized:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return b'x' * limit   # exactly at the cap-plus-one the code asks for
        with mock.patch('server.urllib.request.urlopen', return_value=Oversized()):
            status, body = self.server.poster_bytes(
                {'series': ['1'], 'instance': ['aaaaaaaaaaaa'], 'stamp': ['s']})
        self.assertEqual(status, 404)
        self.assertEqual(body, b'')
        self.assertFalse(list(self.posters.glob('*.jpg')), 'an oversized response is never written to disk')

    def test_poster_bytes_accepts_a_response_within_the_cap(self):
        from unittest import mock

        class Fits:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return b'x' * 100
        with mock.patch('server.urllib.request.urlopen', return_value=Fits()):
            status, body = self.server.poster_bytes(
                {'series': ['1'], 'instance': ['aaaaaaaaaaaa'], 'stamp': ['s']})
        self.assertEqual(status, 200)
        self.assertEqual(body, b'x' * 100)

    def test_a_pruning_failure_never_breaks_the_poster_request_it_rode_in_on(self):
        # contextlib.suppress(OSError) used to be the only guard here; a malformed
        # catalogue entry — not written by this app's own code, but not impossible —
        # raises AttributeError instead, which was not suppressed and would have taken
        # the whole request down with it.
        from unittest import mock
        self.poster_file('aaaaaaaaaaaa', 1)
        self.server.main.write_cache(
            self.settings, 'catalogue.json',
            {'aaaaaaaaaaaa': {'schema': self.server.main.SCHEMA, 'series': ['not-a-dict']}})

        class Fits:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return b'freshly fetched'
        with mock.patch('server.urllib.request.urlopen', return_value=Fits()):
            status, body = self.server.poster_bytes(
                {'series': ['2'], 'instance': ['aaaaaaaaaaaa'], 'stamp': ['s']})
        self.assertEqual(status, 200)
        self.assertEqual(body, b'freshly fetched')



class GracefulShutdown(unittest.TestCase):
    """A container runs this as PID 1, and a container is stopped with SIGTERM.

    Confirmed against a real container, not assumed: an idle one took the full `docker
    stop` timeout and only died to the SIGKILL that follows it, every time, because the
    kernel does not apply a default action for an unhandled signal to PID 1 the way it
    would for any other process, and nothing here had ever registered one. Fixed to
    10.19s -> 0.21s and exit 137 -> 0, measured the same way, before this was written.

    `signal.signal` is mocked here rather than really installed: a real SIGTERM sent to
    the shared test process, mistimed by even a little, terminates the whole gate rather
    than failing one test — a worse failure than the one being guarded against. Mocking
    it also sidesteps the constraint that only the main thread may call the real
    function, which would otherwise force serve() onto the test runner's own main
    thread. The mock still proves the real thing: it captures the exact callable passed
    to signal.signal(signal.SIGTERM, ...) and invokes it as the OS would, so what runs is
    the production closure, not a stand-in for it.
    """

    def test_the_registered_handler_unblocks_serve_and_it_returns_promptly(self):
        import signal
        import threading
        import time
        from unittest import mock
        server_module = load(TVR_AUTH='none')

        class FakeHTTPServer:
            def __init__(self, *args, **kwargs):
                self.daemon_threads = False
                self._stop = threading.Event()

            def serve_forever(self):
                # The real server's loop; calling shutdown() from this same thread's
                # call frame is exactly the deadlock this design avoids.
                self._stop.wait()

            def shutdown(self):
                self._stop.set()

        captured = {}

        def fake_signal(sig, handler):
            if sig == signal.SIGTERM:
                captured['handler'] = handler

        with mock.patch.object(server_module, 'startup_error', return_value=''), \
                mock.patch.object(server_module, 'take_the_volume'), \
                mock.patch.object(server_module.main, 'serve_forever'), \
                mock.patch.object(server_module, 'ThreadingHTTPServer', FakeHTTPServer), \
                mock.patch.object(server_module, 'PORT', 0), \
                mock.patch.object(server_module.signal, 'signal', side_effect=fake_signal):
            result = {}
            thread = threading.Thread(
                target=lambda: result.__setitem__('code', server_module.serve()), daemon=True)
            thread.start()
            try:
                for _ in range(200):
                    if 'handler' in captured:
                        break
                    time.sleep(0.01)
                else:
                    self.fail('serve() never registered a SIGTERM handler')

                # A thread still alive here means the registration happened but the main
                # thread has not yet reached its wait — giving the handler nothing to
                # unblock would prove nothing about it, so this would be the test lying.
                self.assertTrue(thread.is_alive(),
                                'serve() returned before any signal arrived')

                started = time.monotonic()
                captured['handler'](signal.SIGTERM, None)  # exactly what the OS would call
                thread.join(timeout=5)
                elapsed = time.monotonic() - started
            finally:
                thread.join(timeout=1)  # never leave a stray thread behind on failure

            self.assertFalse(thread.is_alive(), 'serve() did not return after the signal')
            self.assertLess(elapsed, 2, 'the handler must unblock it promptly, not '
                            'leave it to an external timeout')
            self.assertEqual(result.get('code'), 0)


class CalendarRequestLog(unittest.TestCase):
    def test_slow_read_logs_the_browser_deadline_and_completion(self):
        import json
        from unittest import mock
        server = load(TVR_AUTH='none')
        entries = []
        class Fake:
            def session(self): return {'csrf': ''}
            def send_json(self, status, payload): self.status = status

        def dispatch(request):
            return {'ok': True, 'events': []}

        class ImmediateDeadline:
            def __init__(self, *args, **kwargs):
                self.target = kwargs['target']
                self.args = kwargs.get('args', ())
            def start(self):
                self.target(*self.args)

        class TimedOut:
            def wait(self, timeout):
                return False
            def set(self):
                pass

        handler = Fake()
        with mock.patch.object(server, 'OPEN', True), \
                mock.patch.object(server, 'load_settings', return_value={'logging': {'level': 'info'}}), \
                mock.patch.object(server, 'log_line', side_effect=lambda _, level, msg: entries.append((level, msg))), \
                mock.patch.object(server, 'dispatch', side_effect=dispatch), \
                mock.patch.object(server.threading, 'Thread', ImmediateDeadline), \
                mock.patch.object(server.threading, 'Event', TimedOut):
            server.Handler.handle_api(handler, {'payload': [json.dumps({'action': 'calendar'})]})
        self.assertEqual(handler.status, 200)
        self.assertTrue(any('still in progress after 55s' in msg for _, msg in entries))
        self.assertTrue(any('finished after' in msg for _, msg in entries))

    def test_calendar_failure_is_recorded_in_the_application_log(self):
        import json
        from unittest import mock
        server = load(TVR_AUTH='none')
        entries = []

        class Fake:
            def session(self): return {'csrf': ''}
            def send_json(self, status, payload): self.status = status

        handler = Fake()
        with mock.patch.object(server, 'OPEN', True), \
                mock.patch.object(server, 'load_settings', return_value={'logging': {'level': 'info'}}), \
                mock.patch.object(server, 'log_line', side_effect=lambda _, level, msg: entries.append((level, msg))), \
                mock.patch.object(server, 'dispatch', return_value={'ok': False, 'error': 'bad cache'}):
            server.Handler.handle_api(handler, {'payload': [json.dumps({'action': 'calendar'})]})
        self.assertEqual(handler.status, 409)
        self.assertTrue(any(level == 'error' and 'bad cache' in msg for level, msg in entries))


class Readiness(unittest.TestCase):
    """/health answers whether this thing is doing its job, and its job is the schedule.

    It used to return `{"ok": true}` unconditionally, which says only that the HTTP
    thread can answer a socket — something it does perfectly well with the scheduler
    dead behind it. Demonstrated against a real container before this was written: a
    settings file that would not parse killed the worker thread at startup, and the
    container reported `running / healthy` and served `{"ok": true}` while nothing
    would ever be scheduled again.
    """

    def health(self, server, thread, progress=None):
        from unittest import mock
        captured = {}

        class Fake:
            headers = {}
            path = '/health'
            def send_json(self, status, body): captured['status'] = status; captured['body'] = body

        default = {'ticks': 1, 'failures': 0, 'last_error': '', 'stuck': False}
        with mock.patch.object(server, '_worker_thread', thread),                 mock.patch.object(server.main, 'worker_progress',
                                  return_value=progress or default):
            server.Handler.do_GET(Fake())
        return captured

    def test_a_live_worker_is_reported_ready(self):
        import threading
        server = load(TVR_AUTH='none')
        running = threading.Event()
        thread = threading.Thread(target=running.wait, daemon=True)
        thread.start()
        try:
            answer = self.health(server, thread)
            self.assertEqual(answer['status'], 200)
            self.assertTrue(answer['body']['ok'])
            self.assertEqual(answer['body']['worker'], 'running')
        finally:
            running.set()
            thread.join(timeout=2)

    def test_a_dead_worker_is_not_reported_healthy(self):
        import threading
        server = load(TVR_AUTH='none')
        thread = threading.Thread(target=lambda: None, daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), 'the fixture thread must actually be finished')

        answer = self.health(server, thread)
        self.assertEqual(answer['status'], 503,
                         'a dead scheduler must not answer 200 — the healthcheck reads the status')
        self.assertFalse(answer['body']['ok'])
        self.assertEqual(answer['body']['worker'], 'stopped')

    def test_no_worker_at_all_is_not_reported_healthy(self):
        server = load(TVR_AUTH='none')
        answer = self.health(server, None)
        self.assertEqual(answer['status'], 503)
        self.assertFalse(answer['body']['ok'])

    def test_a_worker_that_cannot_complete_a_tick_is_not_ready(self):
        """Alive is not the same as working.

        A settings file that will not parse leaves the loop running and getting nowhere.
        Before this, surviving that was indistinguishable from being fine: the container
        reported healthy and said nothing, which is quieter than the crash it replaced
        but no more use.
        """
        import threading
        server = load(TVR_AUTH='none')
        running = threading.Event()
        thread = threading.Thread(target=running.wait, daemon=True)
        thread.start()
        try:
            answer = self.health(server, thread, {
                'ticks': 0, 'failures': 3, 'stuck': True,
                'last_error': 'Settings file is unreadable',
            })
            self.assertEqual(answer['status'], 503)
            self.assertFalse(answer['body']['ok'])
            self.assertEqual(answer['body']['worker'], 'stuck')
            self.assertIn('unreadable', answer['body']['last_error'],
                          'the reason belongs in the answer, not only in a log it cannot write')
        finally:
            running.set()
            thread.join(timeout=2)

    def test_one_bad_tick_does_not_make_the_container_unhealthy(self):
        # Flapping on a single failed read would train an operator to ignore this.
        import threading
        server = load(TVR_AUTH='none')
        running = threading.Event()
        thread = threading.Thread(target=running.wait, daemon=True)
        thread.start()
        try:
            answer = self.health(server, thread, {
                'ticks': 5, 'failures': 1, 'stuck': False, 'last_error': 'one bad read',
            })
            self.assertEqual(answer['status'], 200)
            self.assertTrue(answer['body']['ok'])
        finally:
            running.set()
            thread.join(timeout=2)


class WorkerSurvivesBadSettings(unittest.TestCase):
    def test_the_startup_log_cannot_kill_the_loop(self):
        """It reads the settings, and reading them raises on a file that will not parse.

        Above the guard, a corrupt document killed the worker thread before the loop it
        protects had started — and because the thread is a daemon, the process carried on
        serving. Fixing the file then changed nothing until someone restarted the
        container, because there was no longer a loop to notice.
        """
        source = (context.ROOT / 'src' / 'worker' / 'main.py').read_text(encoding='utf-8')
        block = source.split('def serve_forever()')[1]
        started = block.index("'worker started'")
        loop = block.index('while True:')
        self.assertLess(started, loop, 'the startup log still belongs before the loop')
        guard = block.rindex('contextlib.suppress', 0, started)
        self.assertLess(guard, started, 'the startup log must sit inside a suppressed block')
