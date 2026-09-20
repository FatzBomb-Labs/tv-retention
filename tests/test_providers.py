import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401
from core import validate_air_dates
from tmdb import TMDB
from tvmaze import TVMaze


class TMDBIdentity(unittest.TestCase):
    def test_ambiguous_external_id_lookup_is_rejected(self):
        client = TMDB('test-key')
        client._get = lambda path, query=None: {
            'tv_results': [{'id': 101}, {'id': 202}],
        }

        self.assertIsNone(client.series_id(42))

    def test_unique_external_id_lookup_returns_the_series_id(self):
        client = TMDB('test-key')
        client._get = lambda path, query=None: {'tv_results': [{'id': 101}]}

        self.assertEqual(client.series_id(42), 101)

    def test_saved_air_date_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tmdb-cache.json'
            first = TMDB('test-key', cache_path=path)
            first._get = lambda path, query=None: (
                {'tv_results': [{'id': 101}]} if path.startswith('find/')
                else {'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'}]})
            self.assertEqual(first.air_dates(42, 1), {1: '2020-01-01'})
            first.save()

            second = TMDB('test-key', cache_path=path)
            second._get = lambda *args, **kwargs: self.fail('fresh provider data was requested')
            self.assertEqual(second.air_dates(42, 1), {1: '2020-01-01'})

    def test_expired_air_date_cache_is_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tmdb-cache.json'
            path.write_text(json.dumps({
                '42:1': {'fetched': (dt.date.today() - dt.timedelta(days=30)).isoformat(),
                         'dates': {'1': '2019-01-01'}}
            }))
            client = TMDB('test-key', cache_path=path)
            calls = []

            def fetch(endpoint, query=None):
                calls.append(endpoint)
                return {'tv_results': [{'id': 101}]} if endpoint.startswith('find/') else {
                    'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'}]}

            client._get = fetch
            self.assertEqual(client.air_dates(42, 1), {1: '2020-01-01'})
            self.assertEqual(calls, ['find/42', 'tv/101/season/1'])


class ProviderSafety(unittest.TestCase):
    def test_only_providers_with_a_retention_contract_can_be_enabled(self):
        found = validate_air_dates({'enabled': ['anilist', 'plex', 'jellyfin', 'tvmaze']})
        self.assertEqual(found['enabled'], ['tvmaze'])

    def test_tvmaze_preserves_numeric_specials_season_zero(self):
        client = TVMaze()
        client._show_id = lambda tvdb_id: 7
        client._get = lambda path, query=None: [
            {'season': 0, 'number': 1, 'airdate': '2020-01-01'},
            {'season': 1, 'number': 1, 'airdate': '2021-01-01'},
        ]

        self.assertEqual(client.air_dates(42, 0), {1: '2020-01-01'})

    def test_provider_fill_never_replaces_a_sonarr_date(self):
        from tmdb import fill_air_dates

        class Provider:
            def air_dates(self, tvdb_id, season):
                return {1: '2030-01-01', 2: '2030-01-02'}

            def save(self):
                pass

        episodes = [
            {'season': 1, 'episode': 1, 'air_date': '2020-01-01', 'air_source': 'sonarr'},
            {'season': 1, 'episode': 2, 'air_date': None, 'air_source': ''},
        ]

        self.assertEqual(fill_air_dates(episodes, Provider(), 42), 1)
        self.assertEqual(episodes[0]['air_date'], '2020-01-01')
        self.assertEqual(episodes[0]['air_source'], 'sonarr')
        self.assertEqual(episodes[1]['air_date'], '2030-01-02')
        self.assertEqual(episodes[1]['air_source'], 'tmdb')

    def test_provider_failure_leaves_a_missing_date_unresolved(self):
        from tmdb import TMDBError, fill_air_dates

        class Provider:
            def air_dates(self, tvdb_id, season):
                raise TMDBError('provider unavailable')

            def save(self):
                pass

        episodes = [{'season': 1, 'episode': 1, 'air_date': None, 'air_source': ''}]
        self.assertEqual(fill_air_dates(episodes, Provider(), 42), 0)
        self.assertIsNone(episodes[0]['air_date'])
        self.assertEqual(episodes[0]['air_source'], '')


if __name__ == '__main__':
    unittest.main()