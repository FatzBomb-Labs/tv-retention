"""The HTTP front end: what it refuses, what it guards, and what it lets through.

These exercise the pieces that used to be someone else's problem. The plugin was handed
authentication and a CSRF token by emhttp and never had to be right about either.
"""
import os
import unittest

import context  # noqa: F401


def load(**environment):
    """Import server.py with a given environment, freshly each time.

    The configuration is read at import, which is what makes "refuse to start" possible at
    all — so a test that wants a different configuration needs a different import.
    """
    import importlib
    import sys
    previous = {key: os.environ.get(key) for key in
                ('TVR_USERNAME', 'TVR_PASSWORD', 'TVR_AUTH', 'TVR_PORT', 'TVR_SESSION_HOURS')}
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
        source = (context.ROOT / 'src' / 'tv-retention' / 'worker' / 'server.py').read_text()
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
        source = (context.ROOT / 'src' / 'tv-retention' / 'worker' / 'server.py').read_text()
        # `==` on the username leaks whether it was right, which is half the secret.
        self.assertIn('hmac.compare_digest(username, USERNAME)', source)


class Assets(unittest.TestCase):
    def setUp(self):
        self.server = load(TVR_AUTH='none')

    def test_the_cache_key_moves_when_either_asset_does(self):
        # Joining two digests and truncating takes every character from the first, which is
        # how four stylesheet-only releases shipped under the key the browser already held.
        source = (context.ROOT / 'src' / 'tv-retention' / 'worker' / 'server.py').read_text()
        block = source.split('def asset_key()')[1].split('def ')[0]
        self.assertIn("for name in ('app.js', 'app.css', 'icons.css')", block)
        self.assertIn('digest.update', block)
        self.assertEqual(len(self.server.asset_key()), 12)

    def test_the_page_carries_the_session_token_and_the_api_path(self):
        page = self.server.index_page('a-token').decode()
        self.assertIn('data-csrf="a-token"', page)
        self.assertIn('data-api="/api"', page)
        self.assertIn('id="tv-retention"', page)

    def test_every_icon_the_interface_uses_is_one_we_ship(self):
        """The plugin borrowed Unraid's Font Awesome, which is not ours and is not there.

        An icon name with no glyph behind it renders as an empty, zero-sized element: the
        button is present and there is nothing to click.
        """
        import re
        assets = context.ROOT / 'src' / 'tv-retention' / 'assets'
        markup = (context.ROOT / 'src' / 'tv-retention' / 'include' / 'interface.html').read_text()
        wanted = set(re.findall(r'fa fa-([a-z-]+)', (assets / 'app.js').read_text() + markup))
        wanted = {name for name in wanted if not name.endswith('-')}   # built from a template
        icons = (assets / 'icons.css').read_text()
        for name in sorted(wanted):
            self.assertIn(f'.fa-{name} {{', icons, f'fa-{name} has no glyph')

    def test_an_asset_name_cannot_describe_a_path(self):
        source = (context.ROOT / 'src' / 'tv-retention' / 'worker' / 'server.py').read_text()
        block = source.split('def serve_asset')[1].split('def ')[0]
        self.assertIn("'/' in name or '\\\\' in name or name.startswith('.')", block)


class Worker(unittest.TestCase):
    def test_the_schedule_runs_whether_or_not_anyone_is_logged_in(self):
        """Authentication guards the interface, never the work.

        Sonarr downloads without anyone logged in; this deletes on its schedule the same
        way. A login is for looking at it and changing it.
        """
        source = (context.ROOT / 'src' / 'tv-retention' / 'worker' / 'server.py').read_text()
        block = source.split('def serve()')[1]
        self.assertIn('threading.Thread(target=main.serve_forever', block)
        self.assertIn('daemon=True', block)
        # Started before the socket opens, so a container with nobody watching still works.
        self.assertLess(block.index('worker.start()'), block.index('serve_forever()'))


if __name__ == '__main__':
    unittest.main()


class KeepAlive(unittest.TestCase):
    """Two requests down one connection, where the first one is rejected.

    A string-matching test would not have found this. It needs a real socket, because the
    fault is entirely in what is left in it.
    """

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        self.server_module = load(TVR_USERNAME='someone', TVR_PASSWORD='a-long-enough-password')
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.server_module.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

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
        source = (context.ROOT / 'src' / 'tv-retention' / 'assets' / 'app.js').read_text()
        self.assertIn("if (data.expired) { window.location.href = '/login';", source)

    def test_an_oversized_body_closes_the_connection_instead_of_being_left_behind(self):
        source = (context.ROOT / 'src' / 'tv-retention' / 'worker' / 'server.py').read_text()
        block = source.split('def read_form')[1].split('def ')[0]
        self.assertIn('self.close_connection = True', block)
