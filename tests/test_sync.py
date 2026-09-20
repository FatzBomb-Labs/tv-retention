"""The scheduled Sonarr library sweep, and what it re-enables.

Sync is the only time the application reads Sonarr unasked. These tests hold what one
sweep costs, what it reports as moved, and the narrow conditions under which an ended
series that resumed may switch its own rule back on.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401
import main
from core import Rejected, validate_settings
from library_fixture import INSTANCE, episode


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

        self.originals = (main.client_for, main.sonarr_client, main.notify, main.check_one_rule,
                          main.save_settings)
        main.client_for = lambda *a, **k: Stub()
        main.sonarr_client = lambda instance: Stub()
        main.notify = lambda settings, subject, description, importance='normal', event='errors': \
            self.notified.append((subject, event))
        main.check_one_rule = lambda *a, **k: None
        main.save_settings = lambda settings: None

    def tearDown(self):
        main.client_for, main.sonarr_client, main.notify, main.check_one_rule, \
            main.save_settings = self.originals
        self.temp.cleanup()

    def test_the_first_sync_stores_everything_and_announces_nothing(self):
        """Announcing three thousand series as newly added is true and useless."""
        report = main.sync_from_sonarr(self.settings)
        self.assertEqual(report['series_added'], [])
        self.assertEqual(self.notified, [])
        self.assertEqual(len(main.catalogue_for(self.settings, 'i1')), 1)
        self.assertEqual(len(main.episode_cache(self.settings, self.settings['rules'][0])[0]), 5)

    def test_it_reports_what_moved_since_the_last_one_without_outbound_notification(self):
        main.sync_from_sonarr(self.settings)
        self.library.append({'series_id': 2, 'title': 'Brand New', 'added': '2026-09-08T00:00:00Z',
                             'ended': False, 'status': 'continuing', 'path': '/tv/B',
                             'episode_file_count': 0, 'sort_title': 'brand new'})
        self.library[0]['ended'] = True
        report = main.sync_from_sonarr(self.settings)
        self.assertEqual(report['series_added'], ['Brand New'])
        self.assertEqual(report['series_changed'], 1)
        self.assertEqual(self.notified, [], 'library changes stay in the in-app journal')

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
        stored['synced_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)).isoformat()
        main.write_cache(self.settings, 'sync.json', stored)
        self.assertTrue(main.sync_is_due(self.settings))

    def test_page_refresh_uses_the_shorter_idle_threshold(self):
        main.sync_from_sonarr(self.settings)
        stored = main.last_sync(self.settings)
        stored['synced_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=6)).isoformat()
        main.write_cache(self.settings, 'sync.json', stored)
        self.assertFalse(main.sync_is_due(self.settings), 'resident interval is one hour')
        self.assertTrue(main.sync_is_due(self.settings, main.PAGE_REFRESH_SECONDS))

    def test_recycle_bin_alert_waits_for_an_actual_instance_check(self):
        health = {'instances': {}, 'alerts': []}
        self.assertEqual(main.system_alerts(self.settings, health), [])
        health['instances']['i1'] = {
            'checked_at': main.now_iso(), 'reachable': True, 'recycle_bin': '',
        }
        self.assertEqual([alert['kind'] for alert in main.system_alerts(self.settings, health)],
                         ['no-recycle-bin'])

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
        main.sonarr_client = lambda instance: Broken()
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
        main.sonarr_client = lambda instance: ReadOnly()
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


