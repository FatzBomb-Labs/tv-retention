"""Credential-free AniList GraphQL client for anime air dates."""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request

from core import Rejected, atomic_json

BASE = 'https://graphql.anilist.co'
TIMEOUT = 20
CACHE_DAYS = 30


class AniListError(Rejected):
    pass


class AniList:
    def __init__(self, cache_path=None, timeout=TIMEOUT):
        self.cache_path = cache_path
        self.timeout = timeout
        self.cache = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self.cache = {}

    def _query(self, query, variables):
        body = json.dumps({'query': query, 'variables': variables}).encode('utf-8')
        request = urllib.request.Request(BASE, data=body, method='POST',
                                         headers={'Accept': 'application/json',
                                                  'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                json.JSONDecodeError) as error:
            raise AniListError(f'AniList request failed ({error})')
        if payload.get('errors'):
            raise AniListError('AniList did not return the requested series')
        return payload.get('data') or {}

    def check(self):
        self._query('query { Media(id: 1) { id } }', {})
        return True

    def air_dates(self, title, season):
        key = f'{title}:{int(season)}'
        entry = self.cache.get(key)
        if entry:
            try:
                if (dt.date.today() - dt.date.fromisoformat(entry.get('fetched', ''))).days < CACHE_DAYS:
                    return {int(number): date for number, date in (entry.get('dates') or {}).items()}
            except ValueError:
                pass
        query = '''query($search:String) { Media(search:$search, type:ANIME) {
                    episodes, startDate { year month day }, endDate { year month day },
                    airingSchedule(notYetAired:false, perPage:100) {
                      nodes { airingAt episode }
                    }
                  } }'''
        data = self._query(query, {'search': title or ''})
        media = data.get('Media') or {}
        dates = {}
        # AniList's airing schedule is episode-numbered and includes both historical and
        # upcoming entries. It has no season number, so the season argument remains part
        # of the cache key and the caller's Sonarr season is the boundary we can know.
        # A provider date is still useful for anime where AniList is the only source; when
        # an anime has multiple seasons, Sonarr's own dates win and the first matching
        # episode numbers are only used for blanks.
        nodes = ((media.get('airingSchedule') or {}).get('nodes') or [])
        for node in nodes:
            number = node.get('episode')
            stamp = node.get('airingAt')
            if not number or not stamp:
                continue
            try:
                if isinstance(stamp, (int, float)):
                    date = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).date().isoformat()
                else:
                    date = str(stamp)[:10]
                    dt.date.fromisoformat(date)
            except (TypeError, ValueError, OSError):
                continue
            dates[int(number)] = date
        self.cache[key] = {'fetched': dt.date.today().isoformat(),
                           'dates': {str(number): date for number, date in dates.items()},
                           'season': media}
        return dates

    def fill(self, episodes, title):
        filled = 0
        for season in sorted({episode.get('season') for episode in episodes
                              if not episode.get('air_date') and episode.get('season') is not None}):
            dates = self.air_dates(title, season)
            for episode in episodes:
                if episode.get('season') == season and episode.get('episode') in dates:
                    episode['air_date'] = dates[episode['episode']]
                    episode['air_source'] = 'anilist'
                    filled += 1
        self.save()
        return filled

    def save(self):
        if self.cache_path:
            try:
                atomic_json(self.cache_path, self.cache)
            except OSError:
                pass
