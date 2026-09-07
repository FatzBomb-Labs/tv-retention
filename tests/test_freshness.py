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


class Heartbeat(unittest.TestCase):
    """The per-minute tick's share of the work: ask, re-read what changed, announce it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [INSTANCE],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'series_title': 'A', 'keep_days': 30},
                      {'id': 'r2', 'instance_id': 'i1', 'series_id': 2, 'path': '/tv/B',
                       'series_title': 'B', 'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.checked = []
        self.notified = []
        self.touched = set()
        watcher = self

        class Stub:
            def __init__(self, instance):
                pass

            def changes_since(self, since):
                return set(watcher.touched)

        self.originals = (main.Sonarr, main.check_one_rule, main.notify)
        main.Sonarr = Stub
        main.check_one_rule = lambda settings, rule, *a, **k: self.checked.append(rule['id'])
        main.notify = lambda settings, subject, description, importance='normal', event='errors': \
            self.notified.append((subject, importance, event))

    def tearDown(self):
        main.Sonarr, main.check_one_rule, main.notify = self.originals
        self.temp.cleanup()

    def test_nothing_changed_means_nothing_is_re_read(self):
        main.watch_and_recheck(self.settings, min_interval=0)   # first look, sets the cursor
        self.assertEqual(main.watch_and_recheck(self.settings, min_interval=0), 0)
        self.assertEqual(self.checked, [])

    def test_only_the_series_sonarr_named_are_re_read(self):
        main.watch_and_recheck(self.settings, min_interval=0)
        self.touched = {2}
        self.assertEqual(main.watch_and_recheck(self.settings, min_interval=0), 1)
        self.assertEqual(self.checked, ['r2'])

    def test_a_disabled_rule_is_left_alone_however_much_it_changed(self):
        self.settings['rules'][1]['enabled'] = False
        main.watch_and_recheck(self.settings, min_interval=0)
        self.touched = {2}
        main.watch_and_recheck(self.settings, min_interval=0)
        self.assertEqual(self.checked, [])

    def test_a_new_problem_is_announced(self):
        """Otherwise the page is the only thing that ever knows, which defeats the point."""
        import alerts as alert_module
        before = [alert_module.make('unmatched', rule_id='r1', detail='was already wrong')]
        after = before + [alert_module.make('path-changed', rule_id='r2', detail='moved')]
        fresh = main.announce_alerts(self.settings, before, after)
        self.assertEqual([alert['kind'] for alert in fresh], ['path-changed'])
        self.assertEqual(len(self.notified), 1)

    def test_a_problem_that_was_already_there_is_not_announced_again(self):
        import alerts as alert_module
        standing = [alert_module.make('unmatched', rule_id='r1', detail='still wrong')]
        self.assertEqual(main.announce_alerts(self.settings, standing, standing), [])
        self.assertEqual(self.notified, [])

    def test_a_notice_is_not_worth_a_notification(self):
        import alerts as alert_module
        after = [alert_module.make('ended-expired', rule_id='r1')]
        self.assertEqual(main.announce_alerts(self.settings, [], after), [])
        self.assertEqual(self.notified, [])

    def test_asking_for_zero_means_now_not_the_default(self):
        # `or` turns an explicit zero into the default, and zero is exactly what a caller
        # passes when it means "ask now".
        main.action_watch(self.settings, {'min_interval': 0})
        first = main.load_health(self.settings)['watch']['i1']['checked_at']
        main.action_watch(self.settings, {'min_interval': 0})
        self.assertNotEqual(main.load_health(self.settings)['watch']['i1']['checked_at'], first)

    def test_the_work_itself_is_never_announced(self):
        """Retention is the job, not the news.

        Nothing about episodes being scheduled for deletion, or monitoring being brought
        into line with a keep window, is an alert kind at all — those live in the plan,
        which is never notified. This pins the other half: only structural problems carry
        the notify flag, so a kind added later cannot quietly start announcing the work.
        """
        import alerts as alert_module
        announced = {kind for kind, spec in alert_module.KINDS.items() if spec.get('notify')}
        self.assertEqual(announced, {'unmatched', 'path-changed', 'sonarr-unreachable',
                                     'no-recycle-bin', 'run-aborted'})
        for spec in alert_module.KINDS.values():
            self.assertIn('notify', spec, 'every kind must say whether it is worth a notification')

    def test_an_ended_series_is_announced_once_not_twice(self):
        # Sonarr reporting it ended is the news; the rule having nothing left to do is the
        # consequence, and saying both would say it twice.
        import alerts as alert_module
        self.assertFalse(alert_module.KINDS['ended-expired']['notify'])


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


class NewSeries(unittest.TestCase):
    """Noticing a series added to Sonarr, which no cheap endpoint will tell you."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({'instances': [INSTANCE], 'rules': []})
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.catalogue = [
            {'series_id': 1, 'title': 'Old One', 'added': '2020-01-01T00:00:00Z'},
            {'series_id': 2, 'title': 'Older', 'added': '2019-01-01T00:00:00Z'},
        ]
        self.notified = []
        holder = self

        class Stub:
            def series(self):
                return [dict(row) for row in holder.catalogue]

        self.originals = (main.client_for, main.notify)
        main.client_for = lambda *args, **kwargs: Stub()
        main.notify = lambda settings, subject, description, importance='normal', event='errors': \
            self.notified.append((subject, description, event))

    def tearDown(self):
        main.client_for, main.notify = self.originals
        self.temp.cleanup()

    def test_the_first_look_only_records_where_we_are(self):
        """Announcing three thousand series as newly added would be true and useless."""
        health = {}
        self.assertEqual(main.watch_new_series(self.settings, health, min_hours=0), [])
        self.assertEqual(self.notified, [])
        self.assertEqual(health['series_seen']['i1']['latest_added'], '2020-01-01T00:00:00Z')

    def test_a_series_added_since_is_announced(self):
        health = {}
        main.watch_new_series(self.settings, health, min_hours=0)
        self.catalogue.append({'series_id': 3, 'title': 'Brand New', 'added': '2026-09-07T12:00:00Z'})
        added = main.watch_new_series(self.settings, health, min_hours=0)
        self.assertEqual([series['title'] for series in added], ['Brand New'])
        self.assertEqual(len(self.notified), 1)
        self.assertIn('Brand New', self.notified[0][1])
        self.assertEqual(self.notified[0][2], 'series_added')

    def test_the_same_series_is_not_announced_twice(self):
        health = {}
        main.watch_new_series(self.settings, health, min_hours=0)
        self.catalogue.append({'series_id': 3, 'title': 'Brand New', 'added': '2026-09-07T12:00:00Z'})
        main.watch_new_series(self.settings, health, min_hours=0)
        self.notified.clear()
        main.watch_new_series(self.settings, health, min_hours=0)
        self.assertEqual(self.notified, [])

    def test_the_library_is_not_pulled_on_every_tick(self):
        health = {}
        main.watch_new_series(self.settings, health, min_hours=0)
        self.catalogue.append({'series_id': 3, 'title': 'Brand New', 'added': '2026-09-07T12:00:00Z'})
        self.assertEqual(main.watch_new_series(self.settings, health, min_hours=6), [],
                         '11.5 MiB is not a per-minute question')

    def test_the_added_date_survives_the_mapping(self):
        # It is stored in both caches, so the mapping carrying it means a schema bump.
        client = Sonarr(INSTANCE)
        client._request = lambda *a, **k: [
            {'id': 4, 'title': 'X', 'sortTitle': 'x', 'added': '2026-09-01T00:00:00Z',
             'path': '/tv/X', 'statistics': {}}]
        self.assertEqual(client.series()[0]['added'], '2026-09-01T00:00:00Z')
