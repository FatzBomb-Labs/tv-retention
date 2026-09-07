#!/usr/bin/env python3
"""Minimal Sonarr v3 API client.

Only the endpoints this plugin needs are implemented, using the standard library so the
plugin has no dependencies beyond the Python that ships with Unraid. Every call is time
limited; a slow or offline Sonarr must fail a run rather than hang the scheduler.
"""
from __future__ import annotations

import datetime as dt
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from core import Rejected, normalise

TIMEOUT = 30
USER_AGENT = 'TV-Delete/1.0 (Unraid plugin)'


class SonarrError(Rejected):
    """A Sonarr call failed. The message is written for the person reading the UI."""


class Sonarr:
    def __init__(self, instance: dict, timeout: int = TIMEOUT):
        self.id = instance['id']
        self.name = instance['name']
        self.url = instance['url'].rstrip('/')
        self.key = instance['api_key']
        self.timeout = timeout
        self.context = None
        if not instance.get('verify_tls', True):
            # Only reachable when the operator explicitly turns verification off for a
            # self-signed reverse proxy on their own LAN.
            self.context = ssl._create_unverified_context()

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, query=None, body=None):
        url = f'{self.url}/api/v3/{path.lstrip("/")}'
        if query:
            url += '?' + urllib.parse.urlencode(query)
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header('X-Api-Key', self.key)
        request.add_header('Accept', 'application/json')
        request.add_header('User-Agent', USER_AGENT)
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise SonarrError(f'{self.name}: Sonarr rejected the API key')
            if error.code == 404:
                raise SonarrError(f'{self.name}: Sonarr returned "not found" for {path}')
            raise SonarrError(f'{self.name}: Sonarr returned HTTP {error.code} for {path}')
        except urllib.error.URLError as error:
            raise SonarrError(f'{self.name}: cannot reach {self.url} ({error.reason})')
        except (TimeoutError, OSError) as error:
            raise SonarrError(f'{self.name}: connection to {self.url} failed ({error})')
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise SonarrError(f'{self.name}: Sonarr sent a response that was not JSON. '
                              'Check that the URL points at Sonarr and not a login page.')

    # -- reads -------------------------------------------------------------
    def status(self) -> dict:
        payload = self._request('GET', 'system/status')
        if not isinstance(payload, dict) or 'version' not in payload:
            raise SonarrError(f'{self.name}: that URL did not answer like Sonarr')
        return payload

    def series(self) -> list:
        payload = self._request('GET', 'series')
        if not isinstance(payload, list):
            raise SonarrError(f'{self.name}: unexpected series response')
        results = []
        for entry in payload:
            path = entry.get('path') or ''
            # Sonarr only creates a series folder once it imports something, so the file
            # count decides whether a missing folder is a problem or simply not needed yet.
            statistics = entry.get('statistics') or {}
            results.append({
                'instance_id': self.id,
                'instance_name': self.name,
                'series_id': entry.get('id'),
                'title': entry.get('title') or '',
                'sort_title': entry.get('sortTitle') or entry.get('title') or '',
                # Sonarr's own URL segment, so the interface can link straight to it.
                'slug': entry.get('titleSlug') or '',
                'tvdb_id': entry.get('tvdbId'),
                'tmdb_id': entry.get('tmdbId'),
                'year': entry.get('year'),
                'monitored': bool(entry.get('monitored')),
                # Lifecycle: Sonarr sets both, and describe_lifecycle reads both.
                'ended': bool(entry.get('ended')),
                'status': entry.get('status') or '',
                'episode_file_count': int(statistics.get('episodeFileCount') or 0),
                'size_on_disk': int(statistics.get('sizeOnDisk') or 0),
                'path': normalise(path) if path else '',
                'tags': entry.get('tags') or [],
            })
        results.sort(key=lambda item: item['sort_title'].lower())
        return results

    def episodes(self, series_id: int, files_only: bool = True) -> list:
        """Episodes normalised into the shape core.evaluate expects.

        With files_only off, episodes Sonarr has no file for are included too. Deletion
        only ever looks at episodes with files; the monitoring view needs all of them,
        because an episode's monitored flag matters whether or not it is on disk.
        """
        payload = self._request('GET', 'episode', {'seriesId': series_id, 'includeEpisodeFile': 'true'})
        if not isinstance(payload, list):
            raise SonarrError(f'{self.name}: unexpected episode response for series {series_id}')
        episodes = []
        for entry in payload:
            file_info = entry.get('episodeFile') or {}
            file_id = entry.get('episodeFileId') or file_info.get('id')
            has_file = bool(entry.get('hasFile') and file_id and file_info.get('path'))
            if files_only and not has_file:
                continue
            added = file_info.get('dateAdded')
            air = entry.get('airDateUtc') or entry.get('airDate')
            air_date = None
            if air:
                try:
                    air_date = dt.date.fromisoformat(str(air)[:10])
                except ValueError:
                    air_date = None
            episodes.append({
                'episode_id': entry.get('id'),
                'file_id': file_id if has_file else None,
                'has_file': has_file,
                'series_id': series_id,
                'season': entry.get('seasonNumber'),
                'episode': entry.get('episodeNumber'),
                'title': entry.get('title') or '',
                'air_date': air_date.isoformat() if air_date else None,
                'air_source': 'sonarr' if air_date else '',
                # When Sonarr imported the file. More meaningful than the filesystem's
                # modification time, which a copy or a permission change can rewrite, and
                # it arrives with the episode rather than needing the disk.
                'date_added': str(added) if added else '',
                'monitored': bool(entry.get('monitored')),
                # A fileless episode still needs a stable key for the retention pass.
                'path': normalise(file_info['path']) if has_file
                        else f'sonarr:episode:{entry.get("id")}',
                'size': file_info.get('size') or 0,
            })
        return episodes

    # -- writes ------------------------------------------------------------
    def delete_episode_file(self, file_id: int) -> None:
        """Delete through Sonarr so its database stays correct and its recycle bin applies."""
        self._request('DELETE', f'episodefile/{int(file_id)}')

    def first_acquired(self, series_id: int) -> dict:
        """When each episode of a series was first acquired, from Sonarr's history.

        The oldest record for an episode does not move when the file is later upgraded,
        which is what makes this steadier than the file's own dateAdded. The response is
        large — several megabytes for a long-running series — so callers only ask when an
        episode has no air date from any other source, which is rare.
        """
        payload = self._request('GET', 'history/series',
                                {'seriesId': int(series_id), 'includeEpisode': 'false'})
        records = payload if isinstance(payload, list) else (payload or {}).get('records') or []
        earliest = {}
        for record in records:
            episode_id, stamp = record.get('episodeId'), record.get('date')
            if not episode_id or not stamp:
                continue
            if episode_id not in earliest or stamp < earliest[episode_id]:
                earliest[episode_id] = stamp
        return earliest

    def set_monitored(self, episode_ids, monitored: bool) -> None:
        ids = [int(value) for value in episode_ids if value]
        if not ids:
            return
        self._request('PUT', 'episode/monitor', body={'episodeIds': ids, 'monitored': bool(monitored)})

    def unmonitor(self, episode_ids) -> None:
        self.set_monitored(episode_ids, False)

    def remonitor(self, episode_ids) -> None:
        """Re-arm episodes a narrower rule previously unmonitored. Sonarr may re-download them."""
        self.set_monitored(episode_ids, True)

    def root_folders(self) -> list:
        payload = self._request('GET', 'rootfolder')
        return [entry.get('path') for entry in payload or [] if entry.get('path')]

    def delete_series(self, series_id: int, delete_files: bool = True) -> None:
        """Remove a series from Sonarr, optionally with every file it owns.

        The most destructive call this client makes. Nothing reaches it without an
        explicit, typed confirmation from the operator, and never while dry run is on.
        """
        self._request('DELETE', f'series/{int(series_id)}',
                      query={'deleteFiles': 'true' if delete_files else 'false',
                             'addImportListExclusion': 'false'})

    def search_episodes(self, episode_ids) -> None:
        """Ask Sonarr to look for specific episodes. Only ever the ones just monitored."""
        ids = [int(value) for value in episode_ids if value]
        if not ids:
            return
        self._request('POST', 'command', body={'name': 'EpisodeSearch', 'episodeIds': ids})

    def rescan(self, series_id: int) -> None:
        self._request('POST', 'command', body={'name': 'RescanSeries', 'seriesId': int(series_id)})


def match_rule(rule: dict, catalogue: list) -> dict:
    """Bind a rule to exactly one Sonarr series, or explain why it cannot be bound.

    A rule that does not resolve to a series is never processed. Matching prefers the
    stored series id, falls back to the TVDB id (which survives a library move), and
    finally to the folder path.
    """
    by_id = {entry['series_id']: entry for entry in catalogue}
    if rule.get('series_id') and rule['series_id'] in by_id:
        return {'ok': True, 'series': by_id[rule['series_id']], 'how': 'series id'}
    if rule.get('tvdb_id'):
        hits = [entry for entry in catalogue if entry.get('tvdb_id') == rule['tvdb_id']]
        if len(hits) == 1:
            return {'ok': True, 'series': hits[0], 'how': 'TVDB id'}
    target = normalise(rule.get('path') or '')
    hits = [entry for entry in catalogue if entry['path'] and normalise(entry['path']) == target]
    if len(hits) == 1:
        return {'ok': True, 'series': hits[0], 'how': 'folder path'}
    if len(hits) > 1:
        return {'ok': False, 'error': 'That folder matches more than one Sonarr series; '
                                      'pick the series from the list instead.'}
    return {'ok': False, 'error': 'No Sonarr series has this folder. Check the instance path '
                                  'mapping, or select the series from Sonarr instead of typing a folder.'}
