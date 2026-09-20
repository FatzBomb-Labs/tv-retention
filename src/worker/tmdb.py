#!/usr/bin/env python3
"""Optional TMDB lookup used only to fill air dates Sonarr leaves blank.

Sonarr is always the primary source. TMDB is consulted for episodes with no air date at
all, which is common for long-running daily shows imported from incomplete metadata.
Results are cached on disk so a run costs at most one request per series.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

from core import Rejected, atomic_json

BASE = 'https://api.themoviedb.org/3'
TIMEOUT = 20
CACHE_DAYS = 30


class TMDBError(Rejected):
    pass


class TMDB:
    def __init__(self, api_key: str, cache_path=None, timeout: int = TIMEOUT):
        self.key = api_key
        self.timeout = timeout
        self.cache_path = cache_path
        self.cache = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self.cache = {}

    def _get(self, path: str, query=None):
        query = dict(query or {})
        query['api_key'] = self.key
        url = f'{BASE}/{path.lstrip("/")}?' + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise TMDBError('TMDB rejected the API key')
            raise TMDBError(f'TMDB returned HTTP {error.code}')
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise TMDBError(f'TMDB request failed ({error})')

    def check(self) -> bool:
        self._get('configuration')
        return True

    def series_id(self, tvdb_id: int):
        """Resolve a TVDB id to a TMDB series id via TMDB's external-id lookup."""
        payload = self._get(f'find/{int(tvdb_id)}', {'external_source': 'tvdb_id'})
        results = payload.get('tv_results') or []
        return results[0].get('id') if len(results) == 1 else None

    def air_dates(self, tvdb_id: int, season: int) -> dict:
        """Air dates for one season, keyed by episode number. Cached for CACHE_DAYS."""
        key = f'{tvdb_id}:{season}'
        entry = self.cache.get(key)
        today = dt.date.today()
        if entry:
            try:
                fetched = dt.date.fromisoformat(entry.get('fetched', ''))
                if (today - fetched).days < CACHE_DAYS:
                    return {int(k): v for k, v in entry.get('dates', {}).items()}
            except ValueError:
                pass
        tmdb_id = self.series_id(tvdb_id)
        dates = {}
        if tmdb_id:
            payload = self._get(f'tv/{tmdb_id}/season/{int(season)}')
            for episode in payload.get('episodes') or []:
                number, air = episode.get('episode_number'), episode.get('air_date')
                if number and air:
                    dates[int(number)] = str(air)[:10]
        self.cache[key] = {'fetched': today.isoformat(), 'dates': {str(k): v for k, v in dates.items()}}
        return dates

    def save(self) -> None:
        if self.cache_path:
            try:
                atomic_json(self.cache_path, self.cache)
            except OSError:
                pass


def fill_air_dates(episodes, tmdb: 'TMDB', tvdb_id) -> int:
    """Fill blank air dates in place. Returns how many episodes were filled."""
    if not tmdb or not tvdb_id:
        return 0
    missing = [e for e in episodes if not e.get('air_date') and e.get('season') is not None]
    filled = 0
    for season in sorted({e['season'] for e in missing}):
        try:
            dates = tmdb.air_dates(tvdb_id, season)
        except TMDBError:
            continue
        for episode in missing:
            if episode['season'] == season and episode.get('episode') in dates:
                episode['air_date'] = dates[episode['episode']]
                episode['air_source'] = 'tmdb'
                filled += 1
    tmdb.save()
    return filled
