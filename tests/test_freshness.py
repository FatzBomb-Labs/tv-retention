"""What a check costs, and when Sonarr is actually asked.

Retention is a pure function of a series' episodes and the rule over them, so re-deciding
what a rule would do must not need Sonarr. These tests hold that line: the only things
that reach the network are a forced read, a reading that has aged out, and a series Sonarr
itself reported as changed.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401
import main
from core import CACHE_SCHEMA, Rejected, rule_fingerprint, validate_settings
from sonarr import Sonarr

INSTANCE = {'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32}


def episode(number, monitored=True, has_file=True):
    aired = (dt.date.today() - dt.timedelta(days=400 - number)).isoformat()
    return {'episode_id': number, 'file_id': number if has_file else None, 'has_file': has_file,
            'series_id': 1, 'season': 1, 'episode': number, 'title': f'E{number}',
            'air_date': aired, 'air_source': 'sonarr', 'date_added': '', 'monitored': monitored,
            'path': f'/tv/A/S01E{number:02d}.mkv' if has_file else f'sonarr:episode:{number}',
            'size': 1000}


class Counter:
    """A Sonarr stand-in that records every call it is asked to make."""

    def __init__(self, episodes=None, series=None):
        self.calls = []
        self._episodes = episodes if episodes is not None else [episode(n) for n in range(1, 6)]
        self._series = series or {'series_id': 1, 'title': 'A', 'ended': False,
                                  'status': 'continuing', 'episode_file_count': 5, 'path': '/tv/A'}

    def episodes(self, series_id, files_only=True):
        self.calls.append(('episodes', series_id))
        return [dict(item) for item in self._episodes]

    def series(self):
        self.calls.append(('series', None))
        return [self._series]

    def series_one(self, series_id):
        self.calls.append(('series_one', series_id))
        return dict(self._series)


class Freshness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [INSTANCE],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'series_title': 'A', 'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.rule = self.settings['rules'][0]
        self.client = Counter()
        self.original = main.client_for
        main.client_for = lambda *args, **kwargs: self.client

    def tearDown(self):
        main.client_for = self.original
        self.temp.cleanup()

    # -- the episode store -------------------------------------------------

    def test_the_first_read_goes_to_sonarr_and_is_kept(self):
        episodes, series, read_at, from_cache = main.episodes_for(self.settings, self.rule)
        self.assertEqual(len(episodes), 5)
        self.assertFalse(from_cache)
        self.assertTrue(read_at)
        self.assertEqual(self.client.calls, [('episodes', 1), ('series_one', 1)])
        stored, series, stamp = main.episode_cache(self.settings, self.rule)
        self.assertEqual(len(stored), 5)
        self.assertEqual(stamp, read_at)
        self.assertEqual(series['title'], 'A', 'the lifecycle is part of the reading, not a second call')

    def test_a_second_read_asks_sonarr_nothing(self):
        main.episodes_for(self.settings, self.rule)
        self.client.calls.clear()
        episodes, series, read_at, from_cache = main.episodes_for(self.settings, self.rule)
        self.assertEqual(len(episodes), 5)
        self.assertTrue(from_cache)
        self.assertEqual(self.client.calls, [], 'the stored episodes must be served as they are')

    def test_forcing_reads_sonarr_again(self):
        main.episodes_for(self.settings, self.rule)
        self.client.calls.clear()
        _, _, _, from_cache = main.episodes_for(self.settings, self.rule, force=True)
        self.assertFalse(from_cache)
        self.assertEqual(self.client.calls, [('episodes', 1), ('series_one', 1)])

    def test_a_reading_that_aged_out_is_read_again(self):
        main.episodes_for(self.settings, self.rule)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
        entry = main.read_cache(self.settings, 'episodes/r1.json')
        entry['fetched_at'] = old
        main.write_cache(self.settings, 'episodes/r1.json', entry)
        self.client.calls.clear()
        _, _, _, from_cache = main.episodes_for(self.settings, self.rule)
        self.assertFalse(from_cache)

    def test_a_store_written_for_another_series_is_never_served(self):
        # The rule was re-bound to a different Sonarr series; its episodes are not these.
        main.episodes_for(self.settings, self.rule)
        self.rule['series_id'] = 2
        self.client.calls.clear()
        _, _, _, from_cache = main.episodes_for(self.settings, self.rule)
        self.assertFalse(from_cache)
        self.assertEqual(self.client.calls[0], ('episodes', 2))

    def test_a_store_of_an_older_shape_is_never_served(self):
        # Both caches hold mapped objects, so a mapping change has to retire them.
        main.episodes_for(self.settings, self.rule)
        entry = main.read_cache(self.settings, 'episodes/r1.json')
        entry['schema'] = CACHE_SCHEMA - 1
        main.write_cache(self.settings, 'episodes/r1.json', entry)
        self.assertIsNone(main.episode_cache(self.settings, self.rule)[0])

    def test_the_stored_reading_covers_the_lifecycle_too(self):
        # Otherwise every cached check still costs one call per series to ask "has it ended?"
        main.episodes_for(self.settings, self.rule)
        self.client.calls.clear()
        state = main.monitoring_for(self.settings, self.rule)
        self.assertEqual(self.client.calls, [])
        self.assertIn('ended', state)

    def test_each_rule_is_stored_on_its_own(self):
        # One file for everything would mean a single series check rewriting them all.
        main.episodes_for(self.settings, self.rule)
        self.assertTrue((Path(self.settings['state_dir']) / 'episodes' / 'r1.json').exists())

    def test_a_removed_rule_does_not_keep_its_episodes(self):
        main.episodes_for(self.settings, self.rule)
        main.forget_episodes(self.settings, 'r1')
        self.assertIsNone(main.episode_cache(self.settings, self.rule)[0])

    # -- re-deciding costs nothing ----------------------------------------

    def test_changing_the_keep_window_is_decided_without_sonarr(self):
        """The whole point: editing a rule re-decides, it does not re-read.

        core.py takes no network by design, so a new keep window is arithmetic over
        episodes already in hand. Before this, any change to a rule marked it stale and
        the next check paid for the series again.
        """
        self.rule['keep_days'] = 500
        first = main.monitoring_for(self.settings, self.rule)
        self.assertEqual(first['plan']['delete'], 0)
        self.client.calls.clear()
        self.rule['keep_days'] = 30
        second = main.monitoring_for(self.settings, self.rule)
        self.assertEqual(self.client.calls, [])
        self.assertTrue(second['from_cache'])
        self.assertGreater(second['plan']['delete'], first['plan']['delete'])

    def test_the_age_shown_is_the_age_of_the_reading(self):
        # A plan recomputed now over an hour-old reading is not an hour-old plan, and it
        # is not a live one either. The reading's age is the honest number.
        state = main.monitoring_for(self.settings, self.rule)
        self.assertTrue(state['read_at'])
        self.assertEqual(state['read_at'], main.episode_cache(self.settings, self.rule)[2])

    # -- lifecycle without the catalogue ----------------------------------

    def test_lifecycle_comes_from_one_series_not_the_library(self):
        self.assertEqual(main.series_record(self.settings, self.rule)['title'], 'A')
        self.assertEqual(self.client.calls, [('series_one', 1)],
                         'the twelve megabyte catalogue must not be pulled for one series')

    def test_a_fresh_catalogue_is_used_when_it_is_already_there(self):
        main.write_cache(self.settings, 'catalogue.json', {'i1': {
            'schema': CACHE_SCHEMA, 'fetched_at': main.now_iso(),
            'series': [{'series_id': 1, 'title': 'From catalogue'}]}})
        self.assertEqual(main.series_record(self.settings, self.rule)['title'], 'From catalogue')
        self.assertEqual(self.client.calls, [])

    def test_a_stale_catalogue_is_not_refreshed_to_read_one_series(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
        main.write_cache(self.settings, 'catalogue.json', {'i1': {
            'schema': CACHE_SCHEMA, 'fetched_at': old,
            'series': [{'series_id': 1, 'title': 'Old'}]}})
        self.assertEqual(main.series_record(self.settings, self.rule)['title'], 'A')
        self.assertEqual(self.client.calls, [('series_one', 1)])

    def test_a_series_sonarr_cannot_answer_for_does_not_break_the_check(self):
        def refuse(*args, **kwargs):
            raise Rejected('Sonarr is down')
        main.client_for = refuse
        self.assertEqual(main.series_record(self.settings, self.rule), {})


class Watch(unittest.TestCase):
    """Asking Sonarr what changed, instead of re-reading everything to find out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [INSTANCE],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A', 'keep_days': 30},
                      {'id': 'r2', 'instance_id': 'i1', 'series_id': 2, 'path': '/tv/B', 'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.touched = set()
        self.asked = []
        watcher = self

        class Stub:
            def __init__(self, instance):
                pass

            def changes_since(self, since):
                watcher.asked.append(since)
                return set(watcher.touched)

        self.original = main.Sonarr
        main.Sonarr = Stub

    def tearDown(self):
        main.Sonarr = self.original
        self.temp.cleanup()

    def test_the_first_look_starts_a_cursor_rather_than_replaying_history(self):
        health = {}
        self.assertTrue(main.watch_sonarr(self.settings, health, min_interval=0))
        self.assertTrue(health['watch']['i1']['cursor'])
        self.assertEqual(self.asked, [], 'a library of years of history must not be replayed')
        self.assertEqual(health.get('dirty'), [])

    def test_only_the_rules_whose_series_changed_are_marked(self):
        health = {}
        main.watch_sonarr(self.settings, health, min_interval=0)
        self.touched = {2, 99}
        main.watch_sonarr(self.settings, health, min_interval=0)
        self.assertEqual(health['dirty'], ['r2'])

    def test_the_window_overlaps_so_nothing_falls_between_two_looks(self):
        health = {}
        main.watch_sonarr(self.settings, health, min_interval=0)
        first = health['watch']['i1']['cursor']
        main.watch_sonarr(self.settings, health, min_interval=0)
        second = health['watch']['i1']['cursor']
        self.assertGreaterEqual(second, first, 'the cursor must never slide backwards')
        # Wound back far enough that a record written while the call was in flight is
        # covered by the next window rather than falling between the two.
        health['watch']['i1']['cursor'] = (dt.datetime.now(dt.timezone.utc)
                                           - dt.timedelta(hours=1)).isoformat()
        main.watch_sonarr(self.settings, health, min_interval=0)
        moved = dt.datetime.fromisoformat(health['watch']['i1']['cursor'])
        self.assertGreater((dt.datetime.now(dt.timezone.utc) - moved).total_seconds(), 60)

    def test_it_does_not_ask_again_within_the_interval(self):
        health = {}
        main.watch_sonarr(self.settings, health, min_interval=0)
        self.touched = {1}
        self.assertFalse(main.watch_sonarr(self.settings, health, min_interval=3600))
        self.assertEqual(health.get('dirty'), [])

    def test_a_disabled_instance_is_not_polled(self):
        self.settings['instances'][0]['enabled'] = False
        health = {}
        self.assertFalse(main.watch_sonarr(self.settings, health, min_interval=0))

    def test_an_unreachable_sonarr_leaves_the_cursor_where_it_was(self):
        health = {}
        main.watch_sonarr(self.settings, health, min_interval=0)
        cursor = health['watch']['i1']['cursor']

        class Broken:
            def __init__(self, instance):
                pass

            def changes_since(self, since):
                raise Rejected('unreachable')

        main.Sonarr = Broken
        main.watch_sonarr(self.settings, health, min_interval=0)
        self.assertEqual(health['watch']['i1']['cursor'], cursor,
                         'a failed look must not advance past changes it never saw')

    def test_a_changed_series_is_stale_however_young_its_result_is(self):
        health = {'rules': {'r1': {'checked_at': main.now_iso(),
                                   'fingerprint': rule_fingerprint(self.settings['rules'][0], self.settings)},
                            'r2': {'checked_at': main.now_iso(),
                                   'fingerprint': rule_fingerprint(self.settings['rules'][1], self.settings)}},
                  'dirty': ['r1']}
        self.assertEqual(main.stale_rule_ids(self.settings, health), ['r1'])


class HistoryQuery(unittest.TestCase):
    """The change feed itself, against the shape Sonarr returns."""

    def setUp(self):
        self.client = Sonarr(INSTANCE)
        self.queries = []

        def fake(method, path, query=None, body=None):
            self.queries.append((method, path, dict(query or {})))
            if query.get('eventType') == 3:
                return [{'seriesId': 4, 'eventType': 'downloadFolderImported'},
                        {'seriesId': 4, 'eventType': 'downloadFolderImported'}]
            return [{'seriesId': 9, 'eventType': 'episodeFileDeleted'}, {'noSeries': True}]

        self.client._request = fake

    def test_it_asks_only_about_what_changes_the_disk(self):
        self.client.changes_since('2026-09-01T00:00:00')
        events = [query['eventType'] for _, _, query in self.queries]
        self.assertEqual(events, [3, 5], 'a grab or a rename changes nothing this plugin acts on')
        self.assertEqual({path for _, path, _ in self.queries}, {'history/since'})

    def test_it_returns_the_series_that_changed(self):
        self.assertEqual(self.client.changes_since('2026-09-01T00:00:00'), {4, 9})

    def test_a_record_without_a_series_is_ignored(self):
        self.assertNotIn(None, self.client.changes_since('2026-09-01T00:00:00'))


if __name__ == '__main__':
    unittest.main()
