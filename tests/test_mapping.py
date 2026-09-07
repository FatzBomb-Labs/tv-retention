"""The Sonarr payload as it actually arrives, mapped through the real client.

These exist because describe_lifecycle was tested against hand-written dicts while the
mapping that feeds it never carried the fields it reads, so the ended pill could never
appear. A test that starts from a Sonarr-shaped payload catches that; one that starts from
the shape the consumer wants cannot.
"""
import unittest

import context  # noqa: F401
from core import describe_lifecycle, describe_selectability
from sonarr import Sonarr

INSTANCE = {'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32}

# Trimmed from a real /api/v3/series response.
PAYLOAD = [
    {'id': 7, 'title': 'Firefly', 'sortTitle': 'firefly', 'tvdbId': 78874, 'year': 2002,
     'monitored': False, 'ended': True, 'status': 'ended', 'path': '/tv/Series/Firefly (2002) {tvdb-78874}',
     'statistics': {'episodeFileCount': 14, 'sizeOnDisk': 42}, 'tags': []},
    {'id': 8, 'title': 'Upcoming Thing', 'sortTitle': 'upcoming thing', 'tvdbId': 999, 'year': 2027,
     'monitored': True, 'ended': False, 'status': 'upcoming', 'path': '/tv/Series/Upcoming Thing (2027) {tvdb-999}',
     'statistics': {'episodeFileCount': 0, 'sizeOnDisk': 0}, 'tags': []},
    {'id': 9, 'title': 'Still Going', 'sortTitle': 'still going', 'tvdbId': 555, 'year': 2020,
     'monitored': True, 'ended': False, 'status': 'continuing', 'path': '/tv/Series/Still Going (2020) {tvdb-555}',
     'statistics': {'episodeFileCount': 30, 'sizeOnDisk': 99}, 'tags': []},
]


class SeriesMapping(unittest.TestCase):
    def setUp(self):
        self.client = Sonarr(INSTANCE)
        self.client._request = lambda method, path, query=None, body=None: PAYLOAD
        self.series = {entry['series_id']: entry for entry in self.client.series()}

    def test_the_ended_flag_survives_the_mapping(self):
        self.assertTrue(self.series[7]['ended'])
        self.assertFalse(self.series[9]['ended'])

    def test_the_status_string_survives_the_mapping(self):
        self.assertEqual(self.series[7]['status'], 'ended')
        self.assertEqual(self.series[8]['status'], 'upcoming')

    def test_sonarrs_own_path_is_kept_verbatim(self):
        # No mapping layer: Sonarr owns the filesystem, and its path is the only path.
        self.assertEqual(self.series[7]['path'], '/tv/Series/Firefly (2002) {tvdb-78874}')

    def test_file_counts_survive_the_mapping(self):
        self.assertEqual(self.series[7]['episode_file_count'], 14)
        self.assertEqual(self.series[8]['episode_file_count'], 0)

    def test_a_mapped_series_drives_the_lifecycle_verdict(self):
        # The end-to-end path that was broken: payload -> series() -> describe_lifecycle.
        spent = describe_lifecycle({'files_in_frame': 0, 'files_total': 14}, self.series[7])
        self.assertTrue(spent['ended'])
        self.assertEqual(spent['lifecycle'], 'ended_expired')

    def test_an_ended_show_with_no_files_reads_as_empty(self):
        ended_empty = dict(self.series[7], episode_file_count=0)
        outcome = describe_lifecycle({'files_in_frame': 0, 'files_total': 0}, ended_empty)
        self.assertEqual(outcome['lifecycle'], 'ended_empty')

    def test_an_ended_show_still_keeping_is_flagged_but_not_spent(self):
        outcome = describe_lifecycle({'files_in_frame': 6, 'files_total': 14}, self.series[7])
        self.assertEqual(outcome['lifecycle'], 'ended')

    def test_a_continuing_show_is_never_flagged(self):
        outcome = describe_lifecycle({'files_in_frame': 0, 'files_total': 30}, self.series[9])
        self.assertEqual(outcome['lifecycle'], '')

    def test_a_mapped_series_drives_selectability(self):
        outcome = describe_selectability(self.series[8], in_use=False)
        self.assertTrue(outcome['selectable'])
        self.assertTrue(outcome['awaiting'])

    def test_every_field_the_consumers_read_is_present(self):
        # Guards the whole class of bug: a consumer reading a key the mapping never sets.
        required = {'series_id', 'title', 'tvdb_id', 'path', 'monitored',
                    'ended', 'status', 'episode_file_count', 'size_on_disk', 'year'}
        for entry in self.series.values():
            self.assertEqual(required - set(entry), set())


class CacheSchema(unittest.TestCase):
    def test_the_schema_marker_retires_older_cached_results(self):
        """A cached result written before a field existed must not be shown missing it."""
        import core
        from core import rule_fingerprint, validate_settings
        settings = validate_settings({
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1,
                       'path': '/mnt/user/media/TV/A', 'keep_days': 30}],
        })
        before = rule_fingerprint(settings['rules'][0], settings)
        original = core.CACHE_SCHEMA
        try:
            core.CACHE_SCHEMA = original + 1
            self.assertNotEqual(before, rule_fingerprint(settings['rules'][0], settings))
        finally:
            core.CACHE_SCHEMA = original


if __name__ == '__main__':
    unittest.main()


class CatalogueCacheShape(unittest.TestCase):
    """The catalogue caches mapped series, so a mapping change must retire it."""

    def test_the_cache_schema_is_shared_by_both_caches(self):
        import main
        from core import CACHE_SCHEMA
        self.assertIs(main.CACHE_SCHEMA, CACHE_SCHEMA)

    def test_a_cached_catalogue_of_an_older_shape_is_refused(self):
        import tempfile
        from pathlib import Path
        import main
        from core import CACHE_SCHEMA
        with tempfile.TemporaryDirectory() as temp:
            settings = {'state_dir': str(Path(temp) / 'state'), 'catalogue_ttl_minutes': 60,
                        'instances': [dict(INSTANCE, enabled=True)]}
            # A cached entry written before the mapping carried 'ended', still within its TTL.
            main.write_cache(settings, 'catalogue.json', {'i1': {
                'fetched_at': main.now_iso(), 'schema': CACHE_SCHEMA - 1,
                'series': [{'series_id': 1, 'title': 'Old Shape'}]}})
            calls = []

            class Stub:
                def series(self):
                    calls.append(1)
                    return [{'series_id': 1, 'title': 'Fresh', 'ended': True, 'status': 'ended'}]

            original = main.client_for
            main.client_for = lambda *args, **kwargs: Stub()
            try:
                series = main.catalogue_for(settings, 'i1')
            finally:
                main.client_for = original
            self.assertEqual(calls, [1], 'a stale-shaped cache must be re-read, not served')
            self.assertIn('ended', series[0])

    def test_a_cached_catalogue_of_the_current_shape_is_served(self):
        import tempfile
        from pathlib import Path
        import main
        from core import CACHE_SCHEMA
        with tempfile.TemporaryDirectory() as temp:
            settings = {'state_dir': str(Path(temp) / 'state'), 'catalogue_ttl_minutes': 60,
                        'instances': [dict(INSTANCE, enabled=True)]}
            main.write_cache(settings, 'catalogue.json', {'i1': {
                'fetched_at': main.now_iso(), 'schema': CACHE_SCHEMA,
                'series': [{'series_id': 1, 'title': 'Cached', 'ended': False, 'status': 'continuing'}]}})

            def explode(*args, **kwargs):
                raise AssertionError('the cache should have been served without calling Sonarr')

            original = main.client_for
            main.client_for = explode
            try:
                self.assertEqual(main.catalogue_for(settings, 'i1')[0]['title'], 'Cached')
            finally:
                main.client_for = original
