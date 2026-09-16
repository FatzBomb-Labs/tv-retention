"""Small TVMaze client used as a credential-free air-date provider."""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

from core import Rejected, atomic_json

BASE = 'https://api.tvmaze.com'
TIMEOUT = 20
CACHE_DAYS = 30


class TVMazeError(Rejected):
    pass


class TVMaze:
    def __init__(self, cache_path=None, timeout=TIMEOUT):
        self.cache_path = cache_path
        self.timeout = timeout
        self.cache = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self.cache = {}

    def _get(self, path, query=None):
        url = f'{BASE}/{path.lstrip("/")}'
        if query:
            url += '?' + urllib.parse.urlencode(query)
        try:
            request = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise TVMazeError(f'TVMaze returned HTTP {error.code}')
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise TVMazeError(f'TVMaze request failed ({error})')

    def check(self):
        self._get('shows/1')
        return True

    def _show_id(self, tvdb_id):
        payload = self._get('lookup/shows', {'thetvdb': int(tvdb_id)}) if tvdb_id else None
        return payload.get('id') if isinstance(payload, dict) else None

    def air_dates(self, tvdb_id, season):
        key = f'{tvdb_id}:{int(season)}'
        entry = self.cache.get(key)
        if entry:
            try:
                if (dt.date.today() - dt.date.fromisoformat(entry.get('fetched', ''))).days < CACHE_DAYS:
                    return {int(number): date for number, date in (entry.get('dates') or {}).items()}
            except ValueError:
                pass
        show_id = self._show_id(tvdb_id)
        dates = {}
        if show_id:
            episodes = self._get(f'shows/{show_id}/episodes', {'specials': 1}) or []
            for episode in episodes:
                if int(episode.get('season') or -1) != int(season):
                    continue
                number, date = episode.get('number'), episode.get('airdate')
                if number and date:
                    dates[int(number)] = str(date)[:10]
        self.cache[key] = {'fetched': dt.date.today().isoformat(),
                           'dates': {str(number): date for number, date in dates.items()}}
        return dates

    def fill(self, episodes, tvdb_id):
        missing = [episode for episode in episodes
                   if not episode.get('air_date') and episode.get('season') is not None]
        filled = 0
        for season in sorted({episode['season'] for episode in missing}):
            try:
                dates = self.air_dates(tvdb_id, season)
            except TVMazeError:
                continue
            for episode in missing:
                if episode.get('season') == season and episode.get('episode') in dates:
                    episode['air_date'] = dates[episode['episode']]
                    episode['air_source'] = 'tvmaze'
                    filled += 1
        self.save()
        return filled

    def save(self):
        if self.cache_path:
            try:
                atomic_json(self.cache_path, self.cache)
            except OSError:
                pass
