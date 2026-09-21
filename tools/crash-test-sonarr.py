#!/usr/bin/env python3
"""A synthetic Sonarr endpoint for one purpose: proving that killing the worker mid-write
survives and recovers correctly, without a real Sonarr or any real media in the loop.

This is not part of the shipped image and is never imported by the application. It exists
so PLAN's "shutdown during a run" item can be evidenced with a genuine process kill against
a genuine HTTP boundary, while remaining entirely synthetic: one fixed series, one episode,
a delete that sleeps long enough to be interrupted on command.

Usage: python3 crash-test-sonarr.py [port] [delete-delay-seconds]
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18989
DELETE_DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

SERIES = {'id': 1, 'title': 'Crash Test Fixture', 'sortTitle': 'Crash Test Fixture',
          'tvdbId': 999999, 'path': '/tv/Crash Test Fixture', 'monitored': True,
          'status': 'continuing', 'statistics': {'episodeFileCount': 1}}

EPISODE = {'id': 101, 'seriesId': 1, 'seasonNumber': 1, 'episodeNumber': 1,
          'title': 'Crash Test Episode', 'airDateUtc': '2000-01-01T00:00:00Z',
          'monitored': True, 'hasFile': True, 'episodeFileId': 501,
          'episodeFile': {'id': 501, 'path': '/tv/Crash Test Fixture/S01E01.mkv',
                          'size': 1234, 'dateAdded': '2000-01-02T00:00:00Z'}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write('[fake-sonarr] ' + (fmt % args) + '\n')

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == '/api/v3/system/status':
            self._send(200, {'version': '4.0.0.0', 'appName': 'Sonarr (crash-test fixture)'})
        elif path == '/api/v3/config/mediamanagement':
            self._send(200, {'id': 1, 'recycleBin': '/tv/.recycle', 'recycleBinCleanupDays': 7})
        elif path == '/api/v3/series':
            self._send(200, [SERIES])
        elif path == '/api/v3/series/1':
            self._send(200, SERIES)
        elif path == '/api/v3/episode':
            series_id = query.get('seriesId', ['1'])[0]
            self._send(200, [EPISODE] if series_id == '1' else [])
        elif path == '/api/v3/rootfolder':
            self._send(200, [{'path': '/tv'}])
        else:
            self._send(404, {'message': f'no fixture route for GET {path}'})

    def do_PUT(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)
        if urlparse(self.path).path == '/api/v3/episode/monitor':
            self._send(202, {})
        elif urlparse(self.path).path.startswith('/api/v3/config/mediamanagement/'):
            self._send(202, {})
        else:
            self._send(404, {'message': f'no fixture route for PUT {self.path}'})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith('/api/v3/episodefile/'):
            sys.stderr.write(f'[fake-sonarr] delete requested; sleeping {DELETE_DELAY}s '
                             'so the caller can be killed mid-flight\n')
            time.sleep(DELETE_DELAY)
            self._send(200, {})
        else:
            self._send(404, {'message': f'no fixture route for DELETE {path}'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)
        self._send(201, {})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[fake-sonarr] listening on :{PORT}, delete delay {DELETE_DELAY}s', flush=True)
    server.serve_forever()
