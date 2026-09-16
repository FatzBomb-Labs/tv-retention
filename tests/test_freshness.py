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
import actions
import main
import store
from core import Rejected, rule_fingerprint, validate_settings
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
        entry['schema'] = store.SCHEMA + '-old'
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


class Sync(unittest.TestCase):
    """One reading a day, and the only time the plugin reads Sonarr unasked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [INSTANCE],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'series_title': 'A', 'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        for rule in self.settings['rules']:
            rule['match_status'] = 'matched'
        self.rule = self.settings['rules'][0]
        self.library = [{'series_id': 1, 'title': 'A', 'added': '2020-01-01T00:00:00Z',
                         'ended': False, 'status': 'continuing', 'path': '/tv/A',
                         'episode_file_count': 5, 'sort_title': 'a'}]
        self.episodes = [episode(n) for n in range(1, 6)]
        self.notified = []
        holder = self

        class Stub:
            def series(self):
                return [dict(row) for row in holder.library]

            def series_one(self, series_id):
                for row in holder.library:
                    if row['series_id'] == series_id:
                        return dict(row)
                raise Rejected('no such series')

            def episodes(self, series_id, files_only=True):
                return [dict(item) for item in holder.episodes]

        self.originals = (main.client_for, main.Sonarr, main.notify, main.check_one_rule,
                          main.save_settings)
        main.client_for = lambda *a, **k: Stub()
        main.Sonarr = lambda instance: Stub()
        main.notify = lambda settings, subject, description, importance='normal', event='errors': \
            self.notified.append((subject, event))
        main.check_one_rule = lambda *a, **k: None
        main.save_settings = lambda settings: None

    def tearDown(self):
        main.client_for, main.Sonarr, main.notify, main.check_one_rule, \
            main.save_settings = self.originals
        self.temp.cleanup()

    def test_the_first_sync_stores_everything_and_announces_nothing(self):
        """Announcing three thousand series as newly added is true and useless."""
        report = main.sync_from_sonarr(self.settings)
        self.assertEqual(report['series_added'], [])
        self.assertEqual(self.notified, [])
        self.assertEqual(len(main.catalogue_for(self.settings, 'i1')), 1)
        self.assertEqual(len(main.episode_cache(self.settings, self.settings['rules'][0])[0]), 5)

    def test_it_reports_what_moved_since_the_last_one(self):
        main.sync_from_sonarr(self.settings)
        self.library.append({'series_id': 2, 'title': 'Brand New', 'added': '2026-09-08T00:00:00Z',
                             'ended': False, 'status': 'continuing', 'path': '/tv/B',
                             'episode_file_count': 0, 'sort_title': 'brand new'})
        self.library[0]['ended'] = True
        report = main.sync_from_sonarr(self.settings)
        self.assertEqual(report['series_added'], ['Brand New'])
        self.assertEqual(report['series_changed'], 1)
        self.assertEqual([event for _, event in self.notified], ['series_added'])

    def test_a_series_leaving_sonarr_is_counted(self):
        main.sync_from_sonarr(self.settings)
        self.library.clear()
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_removed'], 1)

    def test_a_managed_series_that_did_not_move_is_not_reported(self):
        main.sync_from_sonarr(self.settings)
        self.assertEqual(main.sync_from_sonarr(self.settings)['episodes_changed'], [])
        self.episodes[0]['monitored'] = not self.episodes[0]['monitored']
        self.assertEqual(main.sync_from_sonarr(self.settings)['episodes_changed'], ['A'])

    def arm(self, watermark=None):
        """The real sequence: the rule ran, the series ended, then somebody armed it.

        Order matters. The rule has to have been enabled for a sync so its episodes are
        stored — the mark is taken from them, and a rule armed with nothing cached falls
        back to the status trigger.
        """
        main.sync_from_sonarr(self.settings)
        self.library[0].update(ended=True, status='ended')
        self.rule.update(enabled=False, auto_reenable=True)
        self.rule['auto_reenable_after'] = \
            main.latest_air_date(self.settings, self.rule) if watermark is None else watermark
        main.sync_from_sonarr(self.settings)

    def add_episode(self, number, days_ahead=0, season=1):
        aired = (dt.date.today() + dt.timedelta(days=days_ahead)).isoformat()
        self.episodes.append(dict(episode(number), season=season, air_date=aired))

    def test_a_sync_reenables_an_armed_series_that_sonarr_says_resumed(self):
        # The slow case: a return announced long before any date exists.
        self.arm()
        self.library[0].update(ended=False, status='continuing')
        report = main.sync_from_sonarr(self.settings)
        self.assertEqual(report['series_reenabled'], ['A'])
        self.assertTrue(self.rule['enabled'])
        self.assertFalse(self.rule['auto_reenable'])
        self.assertEqual(self.rule['auto_reenable_after'], '', 'the mark goes with the arming')

    def test_a_whole_season_dropped_between_two_syncs_still_reenables(self):
        """The case a status check cannot see.

        A streaming service drops a season at once, so Sonarr un-ends the series and
        re-ends it within hours. Sync runs daily, so both readings say ended and the
        transition is never observed — but the newest air date has moved, and that only
        ever moves forward.
        """
        self.arm()
        self.add_episode(6, days_ahead=1)
        report = main.sync_from_sonarr(self.settings)
        self.assertEqual(report['series_reenabled'], ['A'])
        self.assertTrue(self.rule['enabled'])

    def test_a_scheduled_episode_reenables_before_it_lands(self):
        # Retention should be live before the episodes arrive, not after.
        self.arm()
        self.add_episode(6, days_ahead=30)
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], ['A'])

    def test_nothing_new_leaves_it_alone(self):
        self.arm()
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], [])
        self.assertFalse(self.rule['enabled'])

    def test_a_date_moving_backwards_is_not_news(self):
        # Sonarr revising dates on a refresh must not read as new material.
        self.arm()
        self.episodes[-1]['air_date'] = (dt.date.today() - dt.timedelta(days=900)).isoformat()
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], [])

    def test_a_special_does_not_reenable_a_rule_that_excludes_specials(self):
        """The watermark is per rule, not Sonarr's specials-blind `previousAiring`.

        A Christmas special on a show that genuinely finished would otherwise re-enable a
        rule whose owner excludes season 0.
        """
        self.arm()
        self.add_episode(7, days_ahead=1, season=0)
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], [])

    def test_a_special_does_reenable_when_the_series_keeps_specials(self):
        self.rule['include_specials'] = True
        self.arm()
        self.add_episode(7, days_ahead=1, season=0)
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], ['A'])

    def test_an_unarmed_rule_is_never_reenabled(self):
        self.arm()
        self.rule['auto_reenable'] = False
        self.add_episode(6, days_ahead=1)
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], [])
        self.assertFalse(self.rule['enabled'])

    def test_a_rule_armed_before_watermarks_existed_falls_back_to_status(self):
        # Migrated forward with no mark: the air-date trigger cannot fire, and must not
        # fire on an empty string either.
        self.arm(watermark='')
        self.add_episode(6, days_ahead=1)
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], [])
        self.library[0].update(ended=False, status='continuing')
        self.assertEqual(main.sync_from_sonarr(self.settings)['series_reenabled'], ['A'])

    def test_the_interval_decides_when_it_is_due(self):
        self.assertTrue(main.sync_is_due(self.settings), 'nothing stored means overdue')
        main.sync_from_sonarr(self.settings)
        self.assertFalse(main.sync_is_due(self.settings))
        stored = main.last_sync(self.settings)
        stored['synced_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
        main.write_cache(self.settings, 'sync.json', stored)
        self.assertTrue(main.sync_is_due(self.settings))

    def test_browsing_the_library_never_reaches_sonarr(self):
        """The whole point: the picker, a keep window, a plan — all from the stored reading."""
        main.sync_from_sonarr(self.settings)

        def explode(*args, **kwargs):
            raise AssertionError('the interface must not go to Sonarr between syncs')

        main.client_for = explode
        self.assertEqual(len(main.catalogue_for(self.settings, 'i1')), 1)
        self.assertEqual(main.series_record(self.settings, self.settings['rules'][0])['title'], 'A')
        state = main.monitoring_for(self.settings, self.settings['rules'][0])
        self.assertTrue(state['ok'])

    def test_an_unreachable_instance_does_not_lose_what_is_stored(self):
        main.sync_from_sonarr(self.settings)

        class Broken:
            def series(self):
                raise Rejected('unreachable')

            def episodes(self, series_id, files_only=True):
                raise Rejected('unreachable')

        main.client_for = lambda *a, **k: Broken()
        main.Sonarr = lambda instance: Broken()
        report = main.sync_from_sonarr(self.settings)
        self.assertTrue(report['errors'])
        self.assertEqual(len(main.catalogue_for(self.settings, 'i1')), 1, 'yesterday beats nothing')

    def test_a_sync_cannot_write_to_sonarr(self):
        """Opening the plugin must never delete anything.

        The sync runs unattended — on a tick, and on a page load when the reading has
        aged out — so "it only reads" cannot be a matter of reading the code carefully.
        Every method that changes something in Sonarr fails the test if it is reached.
        """
        forbidden = []

        class ReadOnly:
            def series(self):
                return [dict(row) for row in holder.library]

            def series_one(self, series_id):
                return dict(holder.library[0])

            def episodes(self, series_id, files_only=True):
                return [dict(item) for item in holder.episodes]

            def __getattr__(self, name):
                forbidden.append(name)
                raise AssertionError(f'a sync must not call Sonarr.{name}')

        holder = self
        main.client_for = lambda *a, **k: ReadOnly()
        main.Sonarr = lambda instance: ReadOnly()
        main.check_one_rule = self.originals[3]      # the real one, so it reads for itself
        main.sync_from_sonarr(self.settings)
        self.assertEqual(forbidden, [])

    def test_a_sync_never_starts_a_run(self):
        # Separate decisions on the tick: one keeps the data current, the other acts on it.
        started = []
        original = main.run
        main.run = lambda *a, **k: started.append(True)
        try:
            main.sync_from_sonarr(self.settings)
        finally:
            main.run = original
        self.assertEqual(started, [])

    def test_a_sync_stands_aside_for_a_run_rather_than_waiting(self):
        # A page opening mid-run must not hang behind it; the lock refuses, it does not queue.
        with main.run_lock():
            with self.assertRaises(Rejected):
                with main.run_lock():
                    pass


class AutoReenable(unittest.TestCase):
    def setUp(self):
        self.rule = {'id': 'r1', 'instance_id': 'i1', 'series_id': 1,
                     'series_title': 'Returning', 'path': '/tv/Returning',
                     'enabled': False, 'auto_reenable': True, 'match_status': 'matched',
                     'include_specials': None}
        self.settings = {'rules': [self.rule], 'automation': {'exclude_specials': True}}
        self.before = {('i1', 1): {
            'ended': True, 'total_episode_count': 20,
            'seasons': [{'season': 0, 'episodes': 2}, {'season': 1, 'episodes': 18}],
        }}

    def test_a_resumed_series_is_reenabled_once(self):
        changed = main.reenable_returning_rules(
            self.settings, self.before,
            {('i1', 1): {'title': 'Returning', 'ended': False, 'total_episode_count': 20}})
        self.assertEqual(changed, [('Returning', 'the series resumed')])
        self.assertTrue(self.rule['enabled'])
        self.assertFalse(self.rule['auto_reenable'])

    def test_an_episode_count_is_no_longer_a_trigger(self):
        """It approximated the air-date watermark and needed a guard to be trusted.

        `relevant_episode_count` declined whenever Sonarr's season breakdown failed to
        reconcile, which is a comparison saying it cannot be relied on. The watermark
        answers the same question from a value Sonarr sets directly, so counts moving on
        their own — a renumbering, a refresh — mean nothing here.
        """
        changed = main.reenable_returning_rules(
            self.settings, self.before,
            {('i1', 1): {
                'title': 'Returning', 'ended': True, 'total_episode_count': 21,
                'seasons': [{'season': 0, 'episodes': 2}, {'season': 1, 'episodes': 19}],
            }})
        self.assertEqual(changed, [])
        self.assertFalse(self.rule['enabled'])

    def test_an_unarmed_or_unmatched_rule_stays_disabled(self):
        for field, value in (('auto_reenable', False), ('match_status', 'unmatched')):
            self.rule[field] = value
            changed = main.reenable_returning_rules(
                self.settings, self.before,
                {('i1', 1): {'title': 'Returning', 'ended': False, 'total_episode_count': 21}})
            self.assertEqual(changed, [])
            self.assertFalse(self.rule['enabled'])
            self.rule.update(auto_reenable=True, match_status='matched')

    def test_no_baseline_means_no_automatic_change(self):
        changed = main.reenable_returning_rules(
            self.settings, {},
            {('i1', 1): {'title': 'Returning', 'ended': False, 'total_episode_count': 21}})
        self.assertEqual(changed, [])
        self.assertFalse(self.rule['enabled'])

    def test_an_active_series_cannot_use_a_stale_arm(self):
        changed = main.reenable_returning_rules(
            self.settings,
            {('i1', 1): {'ended': False, 'total_episode_count': 20}},
            {('i1', 1): {'title': 'Returning', 'ended': False, 'total_episode_count': 21}})
        self.assertEqual(changed, [])
        self.assertFalse(self.rule['enabled'])


class TMDBGate(unittest.TestCase):
    """An API key is the only switch TMDB has.

    `validate_settings` never keeps an `enabled` flag for this section — migration
    deletes it (test_migration.py asserts that), and DEFAULTS never re-adds it — so a
    key alone must be enough to build the provider a run uses. Gating on both left every
    configured key permanently unreachable while "Test TMDB" kept reporting success,
    because it builds its own client straight from the typed key.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = {'state_dir': str(Path(self.temp.name) / 'state')}

    def tearDown(self):
        self.temp.cleanup()

    def test_a_configured_key_alone_builds_the_provider(self):
        self.settings['tmdb'] = {'api_key': 'b' * 32}
        tmdb = main.tmdb_provider(self.settings)
        self.assertIsNotNone(tmdb)
        self.assertEqual(tmdb.key, 'b' * 32)

    def test_the_shape_validate_settings_actually_produces_still_works(self):
        # No 'enabled' key at all - exactly what validate_settings and migration leave
        # behind, and the shape test_migration.py asserts for a key that survives.
        settings = validate_settings({'tmdb': {'api_key': 'c' * 32}})
        self.assertNotIn('enabled', settings['tmdb'])
        settings['state_dir'] = self.settings['state_dir']
        self.assertIsNotNone(main.tmdb_provider(settings))

    def test_no_key_means_no_provider(self):
        self.assertIsNone(main.tmdb_provider({}))
        self.assertIsNone(main.tmdb_provider({'tmdb': {}}))
        self.assertIsNone(main.tmdb_provider({'tmdb': {'api_key': ''}}))

    def test_a_stale_enabled_flag_from_before_the_bug_fix_changes_nothing(self):
        # Even if something upstream still hands a settings dict an 'enabled' key
        # from an old build, the key alone must decide it either way.
        self.settings['tmdb'] = {'api_key': 'd' * 32, 'enabled': False}
        self.assertIsNotNone(main.tmdb_provider(self.settings))
