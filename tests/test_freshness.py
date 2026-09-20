"""What a check costs, and when Sonarr is actually asked.

Retention is a pure function of a series' episodes and the rule over them, so re-deciding
what a rule would do must not need Sonarr. These tests hold that line: the only things
that reach the network are a forced read, a reading that has aged out, and a series Sonarr
itself reported as changed.

The scheduled library sweep lives in test_sync.py.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401
import main
import store
from core import Rejected, validate_settings
from library_fixture import INSTANCE, episode


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

    def set_monitored(self, episode_ids, monitored):
        self.calls.append(('set_monitored', tuple(episode_ids), monitored))

    def unmonitor(self, episode_ids):
        self.calls.append(('unmonitor', tuple(episode_ids)))

    def delete_episode_file(self, file_id):
        self.calls.append(('delete_episode_file', file_id))


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
        entry['schema'] = store.SCHEMA + '-old'
        main.write_cache(self.settings, 'episodes/r1.json', entry)
        self.assertIsNone(main.episode_cache(self.settings, self.rule)[0])

    def test_a_malformed_episode_cache_is_refetched_not_served_as_empty(self):
        path = Path(self.settings['state_dir']) / 'episodes' / 'r1.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not-json')
        episodes, _, _, from_cache = main.episodes_for(self.settings, self.rule)
        self.assertEqual(len(episodes), 5)
        self.assertFalse(from_cache)
        self.assertEqual(self.client.calls, [('episodes', 1), ('series_one', 1)])

    def test_offline_planning_rejects_a_malformed_episode_cache(self):
        path = Path(self.settings['state_dir']) / 'episodes' / 'r1.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not-json')
        with self.assertRaisesRegex(Rejected, 'Nothing has been read'):
            main.episodes_for(self.settings, self.rule, offline=True)
        self.assertEqual(self.client.calls, [])

    def test_the_stored_reading_covers_the_lifecycle_too(self):
        # Otherwise every cached check still costs one call per series to ask "has it ended?"
        main.episodes_for(self.settings, self.rule)
        self.client.calls.clear()
        state = main.monitoring_for(self.settings, self.rule)
        self.assertEqual(self.client.calls, [])
        self.assertIn('ended', state)

    def test_a_run_reads_each_matched_series_once(self):
        # process_rule used to fetch the series' episodes for the deletion decision, then
        # ask monitoring_for(force=True) to fetch the same series again for the same run —
        # doubling the one call Sonarr's episode list actually costs.
        outcome = main.process_rule(self.settings, self.rule, None, dry_run=True)
        self.assertTrue(outcome['ok'], outcome.get('error'))
        episode_calls = [call for call in self.client.calls if call[0] == 'episodes']
        self.assertEqual(episode_calls, [('episodes', 1)],
                         'one Sonarr episode read for the whole rule, not two')

    def test_rule_planner_refuses_legacy_execution_before_any_sonarr_call(self):
        # Execution belongs to run() and its checkpoints, never to the rule planner.
        for test_mode in (True, False):
            with self.subTest(test_mode=test_mode):
                self.settings['schedule']['test_mode'] = test_mode
                self.client.calls.clear()
                with self.assertRaisesRegex(Rejected, 'planner.*run'):
                    main.process_rule(self.settings, self.rule, None, dry_run=False)
                self.assertEqual(self.client.calls, [])

    def test_a_run_still_reconciles_monitoring_from_the_one_read(self):
        # The optimisation must not cost the reconciliation its own data: fileless
        # episodes still need to be there for monitoring even though evaluate() never
        # sees them, and the store still ends up with the full reading.
        main.process_rule(self.settings, self.rule, None, dry_run=True)
        stored, series, stamp = main.episode_cache(self.settings, self.rule)
        self.assertEqual(len(stored), 5)
        self.assertTrue(stamp)

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
            'schema': store.SCHEMA, 'fetched_at': main.now_iso(),
            'series': [{'series_id': 1, 'title': 'From catalogue'}]}})
        self.assertEqual(main.series_record(self.settings, self.rule)['title'], 'From catalogue')
        self.assertEqual(self.client.calls, [])

    def test_the_stored_library_is_served_whatever_its_age(self):
        """Age is not what decides here any more; the sync is.

        A stored reading was once refused once it passed a TTL, which meant browsing could
        trigger a twelve megabyte fetch at any moment. Now the sync is the only thing that
        refreshes it, so an old reading is served — with its age said plainly elsewhere —
        and nothing the interface does surprises anyone with a wait.
        """
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
        main.write_cache(self.settings, 'catalogue.json', {'i1': {
            'schema': store.SCHEMA, 'fetched_at': old,
            'series': [{'series_id': 1, 'title': 'Old'}]}})
        self.assertEqual(main.series_record(self.settings, self.rule)['title'], 'Old')
        self.assertEqual(self.client.calls, [], 'browsing must not reach Sonarr')

    def test_a_series_not_in_the_store_yet_is_read_on_its_own(self):
        # Added to Sonarr since the last sync: thirty milliseconds, not a resync.
        main.write_cache(self.settings, 'catalogue.json', {'i1': {
            'schema': store.SCHEMA, 'fetched_at': main.now_iso(), 'series': []}})
        self.assertEqual(main.series_record(self.settings, self.rule)['title'], 'A')
        self.assertEqual(self.client.calls, [('series_one', 1)])

    def test_a_series_sonarr_cannot_answer_for_does_not_break_the_check(self):
        def refuse(*args, **kwargs):
            raise Rejected('Sonarr is down')
        main.client_for = refuse
        self.assertEqual(main.series_record(self.settings, self.rule), {})

if __name__ == '__main__':
    unittest.main()


class LiveWhileWatching(unittest.TestCase):
    """What an open page updates by itself, and at what cost."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [INSTANCE],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'series_title': 'A', 'keep_days': 500}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.rule = self.settings['rules'][0]
        self.client = Counter()
        self.original = main.client_for
        main.client_for = lambda *args, **kwargs: self.client
        main.episodes_for(self.settings, self.rule)      # one reading, as a page load makes
        self.client.calls.clear()

    def tearDown(self):
        main.client_for = self.original
        self.temp.cleanup()

    def test_a_heartbeat_re_decides_without_asking_sonarr_anything(self):
        health = main.load_health(self.settings)
        main.recompute_plans(self.settings, health)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(health['rules']['r1']['plan']['delete'], 0)

    def test_a_keep_window_that_moved_is_picked_up(self):
        # Nothing changed in Sonarr; the window did. A page left open must not go on
        # showing the plan it was handed when it loaded.
        health = main.load_health(self.settings)
        main.recompute_plans(self.settings, health)
        self.rule['keep_days'] = 30
        self.assertEqual(main.recompute_plans(self.settings, health), 1)
        self.assertEqual(health['rules']['r1']['plan']['delete'], 5)
        self.assertEqual(self.client.calls, [])

    def test_an_unchanged_result_is_not_rewritten(self):
        # Otherwise an open page rewrites the cache every fifteen seconds to say nothing.
        health = main.load_health(self.settings)
        main.recompute_plans(self.settings, health)
        stamp = health['rules']['r1']['checked_at']
        self.assertEqual(main.recompute_plans(self.settings, health), 0)
        self.assertEqual(health['rules']['r1']['checked_at'], stamp)

    def test_a_series_never_read_is_left_to_the_queue(self):
        self.settings['rules'].append({'id': 'r2', 'instance_id': 'i1', 'series_id': 2,
                                       'path': '/tv/B', 'enabled': True, 'keep_days': 30,
                                       'match_status': 'matched'})
        health = main.load_health(self.settings)
        main.recompute_plans(self.settings, health)
        self.assertNotIn('r2', health.get('rules') or {})
        self.assertEqual(self.client.calls, [], 'the heartbeat must never go out to Sonarr')

    def test_a_disabled_rule_is_not_recomputed(self):
        self.rule['enabled'] = False
        self.assertEqual(main.recompute_plans(self.settings, main.load_health(self.settings)), 0)


