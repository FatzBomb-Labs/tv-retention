"""Scripted HTTP boundary for the real Sonarr client; no listening socket or live URL."""
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
from unittest.mock import patch


class FakeSonarr:
    """Exact ordered requests, independent responses, and fail-closed network access.

    Queue an exception to model an unavailable service or a lost acknowledgement.
    Queue successive payloads to model a changed file or authoritative reread.
    """

    url = 'http://sonarr.invalid'
    api_key = 'fixtureOnlyNotARealSecret00000000'

    def __init__(self):
        self.requests = []
        self.pending = deque()

    def expect(self, method, path, response=None, *, query=None, body=None):
        self.pending.append((method, path, query or {}, deepcopy(body), deepcopy(response)))

    def urlopen(self, request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        if f'{parsed.scheme}://{parsed.netloc}' != self.url:
            raise AssertionError(f'Unexpected outbound target: {parsed.scheme}://{parsed.netloc}')
        query = urllib.parse.parse_qs(parsed.query)
        body = json.loads(request.data) if request.data is not None else None
        actual = (request.get_method(), parsed.path, query, body)
        self.requests.append(actual)
        if not self.pending:
            raise AssertionError(f'Unexpected Sonarr request: {actual!r}')
        method, path, wanted_query, wanted_body, response = self.pending.popleft()
        expected = (method, '/api/v3/' + path.lstrip('/'), wanted_query, wanted_body)
        if actual != expected:
            raise AssertionError(f'Expected {expected!r}, received {actual!r}')
        if request.get_header('X-api-key') != self.api_key:
            raise AssertionError('Request did not use fixture credentials')
        if isinstance(response, Exception):
            raise response
        payload = b'' if response is None else json.dumps(response).encode('utf-8')
        return BytesIO(payload)

    def assert_finished(self):
        if self.pending:
            raise AssertionError(f'{len(self.pending)} expected Sonarr request(s) were not sent')

    @property
    def mutations(self):
        return [request for request in self.requests if request[0] != 'GET']


def episode_payload(episode_id=101, *, number=1, file_id=99, monitored=True):
    """Sonarr-shaped entry; using the same file ID models multi-episode membership."""
    row = {'id': episode_id, 'seriesId': 1, 'seasonNumber': 1,
           'episodeNumber': number, 'title': f'Episode {number}',
           'airDateUtc': '2020-01-01T00:00:00Z', 'monitored': monitored,
           'hasFile': file_id is not None, 'episodeFileId': file_id or 0}
    if file_id is not None:
        row['episodeFile'] = {'id': file_id, 'path': f'/tv/Fixture/{file_id}.mkv',
                              'size': 1000, 'dateAdded': '2020-01-02T00:00:00Z'}
    return row


SERIES = {'id': 1, 'title': 'Fixture', 'sortTitle': 'Fixture', 'tvdbId': 10,
          'path': '/tv/Fixture', 'monitored': True, 'status': 'ended',
          'statistics': {'episodeFileCount': 1}}


class IsolatedWorker:
    """Fresh persistence and restored module globals for each Linux behavior test."""

    def __init__(self):
        self.stack = ExitStack()
        self.sonarr = FakeSonarr()

    def __enter__(self):
        try:
            self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
            config = self.root / 'settings.json'
            runtime = self.root / 'runtime'
            self.stack.enter_context(patch.dict(os.environ, {
                'TVR_CONFIG_DIR': str(self.root), 'TVR_CONFIG': str(config),
                'TVR_RUNTIME': str(runtime), 'TVR_DEVELOPMENT': '1',
            }))
            # Block even direct socket access; urlopen is replaced separately so the
            # real client's JSON serialization and payload mapping remain exercised.
            for target in ('socket.create_connection', 'socket.socket.connect',
                           'socket.socket.connect_ex', 'socket.getaddrinfo'):
                self.stack.enter_context(patch(target, side_effect=AssertionError(
                    'Network access outside the scripted Sonarr fixture')))
            self.stack.enter_context(patch('urllib.request.urlopen', self.sonarr.urlopen))
            import main
            import store
            self.main, self.store = main, store
            for module in (store, main):
                self.stack.enter_context(patch.object(module, 'CONFIG', config))
                self.stack.enter_context(patch.object(module, 'RUNTIME', runtime))
            self.stack.enter_context(patch.object(store, 'CONFIG_DIR', self.root))
            self.stack.enter_context(patch.object(store, '_writable_since', {}))
            if hasattr(main, '_ACTIVE_INTENT'):
                self.stack.enter_context(patch.object(main, '_ACTIVE_INTENT', None))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def settings(self, *, test_mode=True, removal='unmonitor-all'):
        from core import validate_settings
        settings = validate_settings({
            'instances': [{'id': 'fake', 'name': 'Fixture', 'url': self.sonarr.url,
                           'api_key': self.sonarr.api_key}],
            'schedule': {'test_mode': test_mode},
            'rules': [{'id': 'r1', 'instance_id': 'fake', 'series_id': 1,
                       'series_title': 'Fixture', 'path': '/tv/Fixture', 'tvdb_id': 10, 'keep_days': 30,
                       'queue': {'removal': {'action': removal, 'target': {
                           'instance_id': 'fake', 'url': self.sonarr.url, 'series_id': 1,
                           'tvdb_id': 10, 'path': '/tv/Fixture'}}}}],
        })
        settings['state_dir'] = str(self.root / 'state')
        self.store.save_settings(settings)
        return settings
