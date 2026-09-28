"""Current saved-mode boundary through real actions/client transport, never live Sonarr."""
import datetime as dt
import json
import unittest
import urllib.error
from unittest.mock import patch

import context  # noqa: F401
from fake_sonarr import IsolatedWorker, SERIES, episode_payload


QUERY = {'seriesId': ['1'], 'includeEpisodeFile': ['true']}


def cached_rule(fixture, test_mode):
    settings = fixture.settings(test_mode=test_mode)
    rule = settings['rules'][0]
    rule.update(queue={}, match_status='matched', combine='all')
    fixture.store.save_settings(settings)
    rows = [episode_payload(), episode_payload(102, number=2, file_id=None, monitored=False)]
    rows[1]['airDateUtc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    fixture.sonarr.expect('GET', 'episode', rows, query=QUERY)
    client = fixture.main.client_for(settings, 'fake')
    episodes = client.episodes(1, files_only=False)
    fixture.store.store_episodes(settings, rule, episodes, client._map_series(SERIES))
    return settings


class ModeBoundary(unittest.TestCase):
    def test_picker_rejects_in_test_mode_before_sending_a_write(self):
        import actions
        with IsolatedWorker() as fixture:
            fixture.settings()
            result = actions.dispatch({'action': 'set-monitored', 'rule_id': 'r1',
                                       'monitor': [101]})
            self.assertFalse(result['ok'], result)
            self.assertIn('Test Mode', result['error'])
            self.assertEqual(fixture.sonarr.mutations, [])
            fixture.sonarr.assert_finished()

    def test_saved_rule_and_scope_pass_in_both_modes(self):
        import actions
        for mode in (True, False):
            with self.subTest(mode=mode), IsolatedWorker() as fixture:
                settings = cached_rule(fixture, mode)
                settings['rules'][0]['keep_days'] = 60
                saved = actions.dispatch({'action': 'settings', 'settings': settings})
                self.assertTrue(saved['ok'], saved)
                self.assertEqual(fixture.store.load_settings()['rules'][0]['keep_days'], 60)
                if not mode:
                    fixture.sonarr.expect('PUT', 'episode/monitor', body={
                        'episodeIds': [102], 'monitored': True})
                    fixture.sonarr.expect('PUT', 'episode/monitor', body={
                        'episodeIds': [101], 'monitored': False})
                result = actions.dispatch({'action': 'scope-pass', 'rule_id': 'r1',
                                           'monitor_new': True, 'unmonitor_outside': True})
                self.assertTrue(result['ok'], result)
                self.assertEqual((result['monitored'], result['unmonitored']),
                                 (0, 0) if mode else (1, 1))
                if mode:
                    self.assertTrue(result['skipped'])
                    self.assertEqual((result['skipped_monitored'], result['skipped_unmonitored']), (1, 1))
                    self.assertIn('Test Mode', result['message'])
                self.assertEqual(len(fixture.sonarr.mutations), 0 if mode else 2)
                fixture.sonarr.assert_finished()

    def test_noop_scope_pass_still_reports_test_mode(self):
        import actions
        with IsolatedWorker() as fixture:
            cached_rule(fixture, True)
            result = actions.dispatch({'action': 'scope-pass', 'rule_id': 'r1'})
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['skipped'])
            self.assertEqual(result['monitored'], 0)
            self.assertEqual(fixture.sonarr.mutations, [])
            fixture.sonarr.assert_finished()

    def test_picker_live_counterpart_updates_cache(self):
        import actions
        with IsolatedWorker() as fixture:
            settings = cached_rule(fixture, False)
            fixture.sonarr.expect('PUT', 'episode/monitor', body={
                'episodeIds': [102], 'monitored': True})
            fixture.sonarr.expect('PUT', 'episode/monitor', body={
                'episodeIds': [101], 'monitored': False})
            result = actions.dispatch({'action': 'set-monitored', 'rule_id': 'r1',
                                       'monitor': [102], 'unmonitor': [101]})
            self.assertTrue(result['ok'], result)
            self.assertEqual((result['monitored'], result['unmonitored']), (1, 1))
            rows, _, _ = fixture.store.episode_cache(settings, settings['rules'][0])
            self.assertEqual([row['monitored'] for row in rows], [False, True])
            fixture.sonarr.assert_finished()

    def test_recycle_bin_in_both_modes(self):
        import actions
        for mode in (True, False):
            with self.subTest(mode=mode), IsolatedWorker() as fixture:
                fixture.settings(test_mode=mode)
                fixture.sonarr.expect('GET', 'config/mediamanagement', {'id': 1, 'recycleBin': ''})
                if not mode:
                    fixture.sonarr.expect('PUT', 'config/mediamanagement/1', body={
                        'id': 1, 'recycleBin': '/recycle', 'recycleBinCleanupDays': 7})
                result = actions.dispatch({'action': 'enable-recycle-bin',
                                           'instance_id': 'fake', 'path': '/recycle'})
                self.assertEqual(result['ok'], not mode, result)
                if mode:
                    self.assertIn('Test Mode', result['error'])
                self.assertEqual(len(fixture.sonarr.mutations), 0 if mode else 1)
                fixture.sonarr.assert_finished()

    def test_reused_client_checks_saved_mode_and_fails_closed(self):
        from core import Rejected
        with IsolatedWorker() as fixture:
            stale = fixture.settings(test_mode=False)
            client = fixture.main.client_for(stale, 'fake')
            for contents in ('{', json.dumps({'schedule': {'test_mode': False}, 'rules': 'bad'}),
                             json.dumps({'schedule': {'test_mode': True}}), '{}'):
                with self.subTest(contents=contents):
                    fixture.store.CONFIG.write_text(contents)
                    with self.assertRaises(Rejected):
                        client.search_episodes([101])
            fixture.store.CONFIG.unlink()
            with self.assertRaisesRegex(Rejected, 'Test Mode'):
                client.delete_episode_file(99)
            fixture.store.save_settings(stale)
            fixture.sonarr.expect('POST', 'command', body={'name': 'EpisodeSearch', 'episodeIds': [101]})
            client.search_episodes([101])
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            fixture.sonarr.assert_finished()

    def test_mode_toggle_after_first_run_write_blocks_next_write(self):
        import actions
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled), IsolatedWorker() as fixture:
                settings = fixture.settings(test_mode=False)
                settings['rules'][0].update(queue={}, combine='all')
                fixture.store.save_settings(settings)
                fixture.sonarr.expect('GET', 'series', [SERIES])
                fixture.sonarr.expect('GET', 'episode', [episode_payload()], query=QUERY)
                fixture.sonarr.expect('PUT', 'episode/monitor', body={
                    'episodeIds': [101], 'monitored': False})
                original = fixture.sonarr.urlopen
                def toggle(request, **kwargs):
                    response = original(request, **kwargs)
                    if request.get_method() == 'PUT':
                        current = fixture.store.load_settings()
                        current['schedule']['test_mode'] = True
                        saved = actions.dispatch({'action': 'settings', 'settings': current})
                        self.assertTrue(saved['ok'], saved)
                    return response
                with patch('urllib.request.urlopen', toggle):
                    result = fixture.main.run(scheduled=True) if scheduled else actions.dispatch(
                        {'action': 'run'})['result']
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['deleted'], 0)
                self.assertIn('Test Mode', '; '.join(result['errors']))
                self.assertEqual([op['status'] for op in fixture.store.load_intent(settings)['operations']],
                                 ['done', 'failed'])
                self.assertEqual(len(fixture.sonarr.mutations), 1)
                fixture.sonarr.assert_finished()

    def test_manual_scheduled_preview_and_series_removal_matrix(self):
        import actions
        for mode in (True, False):
            for entry in ('run', 'scheduled', 'preview'):
                with self.subTest(mode=mode, entry=entry), IsolatedWorker() as fixture:
                    settings = fixture.settings(test_mode=mode, removal='delete-series')
                    before = fixture.store.CONFIG.read_bytes()
                    dry = mode or entry == 'preview'
                    if not dry:
                        fixture.sonarr.expect('GET', 'series', [SERIES])
                    fixture.sonarr.expect('GET', 'series/1', SERIES)
                    if not dry:
                        fixture.sonarr.expect('GET', 'series/1', SERIES)
                        fixture.sonarr.expect('DELETE', 'series/1', query={
                            'deleteFiles': ['false'], 'addImportListExclusion': ['false']})
                    result = fixture.main.run(scheduled=True) if entry == 'scheduled' else actions.dispatch(
                        {'action': entry})['result']
                    self.assertEqual(result['dry_run'], dry)
                    self.assertEqual(len(fixture.sonarr.mutations), 0 if dry else 1)
                    if dry:
                        self.assertEqual(fixture.store.CONFIG.read_bytes(), before)
                        self.assertFalse((fixture.root / 'state').exists())
                    else:
                        self.assertEqual(result['status'], 'complete')
                    fixture.sonarr.assert_finished()

    def test_test_mode_retry_does_not_change_persisted_intent(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            fixture.sonarr.expect('GET', 'series/1', SERIES)
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query=QUERY)
            fixture.sonarr.expect('GET', 'series/1', SERIES)
            fixture.sonarr.expect('PUT', 'episode/monitor', urllib.error.URLError('lost ack'),
                                   body={'episodeIds': [101], 'monitored': False})
            fixture.main.run()
            settings = fixture.store.load_settings()
            settings['schedule']['test_mode'] = True
            fixture.store.save_settings(settings)
            before = fixture.store.load_intent(settings)
            fixture.sonarr.expect('GET', 'series/1', SERIES)
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query=QUERY)
            self.assertTrue(fixture.main.run()['test_mode'])
            self.assertEqual(fixture.store.load_intent(settings), before)
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            fixture.sonarr.assert_finished()

    def test_cli_run_in_both_modes(self):
        import io
        for mode in (True, False):
            with self.subTest(mode=mode), IsolatedWorker() as fixture:
                fixture.settings(test_mode=mode)
                if not mode:
                    fixture.sonarr.expect('GET', 'series', [SERIES])
                fixture.sonarr.expect('GET', 'series/1', SERIES)
                fixture.sonarr.expect('GET', 'episode', [episode_payload()], query=QUERY)
                if not mode:
                    fixture.sonarr.expect('GET', 'series/1', SERIES)
                    fixture.sonarr.expect('PUT', 'episode/monitor', body={
                        'episodeIds': [101], 'monitored': False})
                with patch('sys.argv', ['main.py', 'run', '--scheduled']), patch('sys.stdout', io.StringIO()):
                    self.assertEqual(fixture.main.cli(), 0)
                self.assertEqual(len(fixture.sonarr.mutations), 0 if mode else 1)
                fixture.sonarr.assert_finished()

    def test_due_tick_calls_real_run_in_both_modes(self):
        for mode in (True, False):
            with self.subTest(mode=mode), IsolatedWorker() as fixture:
                settings = fixture.settings(test_mode=mode)
                settings['schedule']['enabled'] = True
                fixture.store.save_settings(settings)
                fixture.store.save_job_state(settings, {'last_connectivity': fixture.store.now_iso()})
                # Isolate due/connectivity/background sync decisions, not the run or client.
                with patch.object(fixture.main.schedules, 'due_occurrence',
                                  return_value=fixture.main.dt.datetime.now(fixture.main.dt.timezone.utc)), \
                        patch.object(fixture.main, 'sync_is_due', return_value=False), \
                        patch.object(fixture.main, 'sonarr_reachable', return_value=True), \
                        patch.object(fixture.main, 'sync_from_sonarr', return_value={}) as sync:
                    if not mode:
                        fixture.sonarr.expect('GET', 'series', [SERIES])
                    fixture.sonarr.expect('GET', 'series/1', SERIES)
                    fixture.sonarr.expect('GET', 'episode', [episode_payload()], query=QUERY)
                    if not mode:
                        fixture.sonarr.expect('GET', 'series/1', SERIES)
                        fixture.sonarr.expect('PUT', 'episode/monitor', body={
                            'episodeIds': [101], 'monitored': False})
                    self.assertEqual(fixture.main.tick(), 0)
                    self.assertEqual([call.kwargs['reason'] for call in sync.call_args_list],
                                     ['before the run'] if mode else ['before the run', 'after the run'])
                self.assertEqual(len(fixture.sonarr.mutations), 0 if mode else 1)
                fixture.sonarr.assert_finished()

    def test_post_run_sync_failure_keeps_the_completed_schedule(self):
        from core import Rejected
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['schedule']['enabled'] = True
            fixture.store.save_settings(settings)
            fixture.store.save_job_state(settings, {'last_connectivity': fixture.store.now_iso()})
            with patch.object(fixture.main.schedules, 'due_occurrence',
                              return_value=fixture.main.dt.datetime.now(fixture.main.dt.timezone.utc)), \
                    patch.object(fixture.main, 'sync_is_due', return_value=False), \
                    patch.object(fixture.main, 'sonarr_reachable', return_value=True), \
                    patch.object(fixture.main, 'sync_from_sonarr', side_effect=[{}, Rejected('offline')]), \
                    patch.object(fixture.main, 'run', return_value={'test_mode': False}) as run:
                self.assertEqual(fixture.main.tick(), 0)
            run.assert_called_once_with(preview=False, scheduled=True)
            state = fixture.store.job_state(settings)
            self.assertIsNotNone(state['last_run'])
            self.assertIsNotNone(state['last_occurrence'])
            self.assertTrue(fixture.main.sync_is_due(settings))
            fixture.sonarr.assert_finished()

    def test_recovered_pending_run_is_not_scheduled_again_in_the_same_tick(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            settings['schedule']['enabled'] = True
            fixture.store.save_settings(settings)
            pending = '2026-09-20T04:00:00+00:00'
            fixture.store.save_job_state(settings, {
                'last_connectivity': None,
                'pending_run': pending,
            })
            with patch.object(fixture.main, 'age_seconds', return_value=999), \
                    patch.object(fixture.main, 'check_connectivity', return_value=True), \
                    patch.object(fixture.main, 'sync_is_due', return_value=False), \
                    patch.object(fixture.main, 'run', return_value={
                        'test_mode': True, 'planned': 0, 'rules': []}) as recovered, \
                    patch.object(fixture.main.schedules, 'due_occurrence',
                                 side_effect=AssertionError('recovery was scheduled twice')):
                self.assertEqual(fixture.main.tick(), 0)
            recovered.assert_called_once_with(preview=False, scheduled=True)
            state = fixture.store.job_state(settings)
            self.assertIsNone(state['pending_run'])
            self.assertEqual(state['last_occurrence'], pending)
            fixture.sonarr.assert_finished()

    def test_tick_does_not_read_or_write_while_another_run_holds_the_lock(self):
        from core import Rejected
        with IsolatedWorker() as fixture:
            fixture.settings()
            jobs = fixture.root / 'state' / 'jobs.json'
            before = jobs.read_bytes() if jobs.exists() else None
            with fixture.main.run_lock():
                with patch.object(fixture.main, 'load_settings', side_effect=AssertionError(
                        'tick read settings while the run lock was held')):
                    with self.assertRaisesRegex(Rejected, 'already in progress'):
                        fixture.main.tick()
            self.assertEqual(fixture.sonarr.requests, [])
            self.assertEqual(jobs.read_bytes() if jobs.exists() else None, before)
            fixture.sonarr.assert_finished()

    def test_scope_toggle_reports_only_acknowledged_writes_as_applied(self):
        import actions
        with IsolatedWorker() as fixture:
            cached_rule(fixture, False)
            fixture.sonarr.expect('PUT', 'episode/monitor', body={
                'episodeIds': [102], 'monitored': True})
            original = fixture.sonarr.urlopen
            def toggle(request, **kwargs):
                response = original(request, **kwargs)
                current = fixture.store.load_settings()
                current['schedule']['test_mode'] = True
                fixture.store.save_settings(current)
                return response
            with patch('urllib.request.urlopen', toggle):
                result = actions.dispatch({'action': 'scope-pass', 'rule_id': 'r1',
                                           'monitor_new': True, 'unmonitor_outside': True})
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['skipped'])
            self.assertEqual((result['monitored'], result['unmonitored']), (1, 0))
            self.assertEqual((result['skipped_monitored'], result['skipped_unmonitored']), (0, 1))
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            fixture.sonarr.assert_finished()

    def test_restore_refused_in_both_modes_without_activation(self):
        import actions
        import backup
        for mode in (True, False):
            with self.subTest(mode=mode), IsolatedWorker() as fixture:
                fixture.settings(test_mode=mode)
                before = fixture.store.CONFIG.read_bytes()
                with patch.object(backup, 'restore', side_effect=AssertionError('Must not activate')):
                    result = actions.dispatch({'action': 'backup', 'operation': 'restore',
                                               'file': 'tv-retention-fixture.zip', 'confirm': 'RESTORE'})
                self.assertFalse(result['ok'])
                self.assertIn('temporarily unavailable', result['error'])
                self.assertEqual(fixture.store.CONFIG.read_bytes(), before)
                self.assertEqual(fixture.sonarr.requests, [])


if __name__ == '__main__':
    unittest.main()


class UpgradeIsNotAMissedRun(unittest.TestCase):
    """`last_occurrence` arrived after `last_run`, and an absent key reads as work owed.

    Deploying build 23 over build 22 fired a catch-up test run a minute after start, for
    an occurrence the previous build had already answered that morning. Harmless under
    Test Mode; with Test Mode off it is an unscheduled real run.
    """

    SCHEDULE = {'enabled': True, 'frequency': 'daily', 'hour': 1, 'minute': 0,
                'weekday': 0, 'monthly_mode': 'day', 'monthly_day': 1,
                'monthly_weekday': '', 'cron': '0 4 * * *',
                # Eastern, so 01:00 local is 05:00Z. Not America/Detroit, which the
                # container ships tzdata for but the gate host's trimmed zone database
                # does not have -- the zone name is incidental to what is under test.
                'timezone': 'America/New_York'}

    def test_a_run_recorded_without_its_occurrence_is_credited_with_one(self):
        import main
        state = {'last_run': '2026-09-20T05:00:26+00:00'}   # 01:00 EDT, as build 22 wrote it
        self.assertTrue(main.seed_last_occurrence(self.SCHEDULE, state))
        self.assertEqual(state['last_occurrence'], '2026-09-20T05:00:00+00:00')

    def test_the_credited_occurrence_stops_the_scheduler_owing_it(self):
        import main
        import schedules
        state = {'last_run': '2026-09-20T05:00:26+00:00'}
        noon = dt.datetime(2026, 9, 20, 16, 0, tzinfo=dt.timezone.utc)      # 12:00 EDT
        self.assertIsNotNone(schedules.due_occurrence(self.SCHEDULE, noon, None),
                             'without the seed the morning occurrence reads as owed')
        main.seed_last_occurrence(self.SCHEDULE, state)
        self.assertIsNone(schedules.due_occurrence(self.SCHEDULE, noon,
                                                   state['last_occurrence']))

    def test_a_genuinely_missed_occurrence_still_catches_up(self):
        """The seed credits one occurrence, not every occurrence since."""
        import main
        import schedules
        state = {'last_run': '2026-09-17T05:00:26+00:00'}   # three days ago
        main.seed_last_occurrence(self.SCHEDULE, state)
        noon = dt.datetime(2026, 9, 20, 16, 0, tzinfo=dt.timezone.utc)
        self.assertIsNotNone(schedules.due_occurrence(self.SCHEDULE, noon,
                                                      state['last_occurrence']))

    def test_a_fresh_install_is_left_alone(self):
        import main
        for state in ({}, {'last_connectivity': '2026-09-20T05:00:00+00:00'}):
            self.assertFalse(main.seed_last_occurrence(self.SCHEDULE, dict(state)))

    def test_an_existing_occurrence_is_never_rewritten(self):
        import main
        state = {'last_run': '2026-09-20T05:00:26+00:00',
                 'last_occurrence': '2026-09-19T05:00:00+00:00'}
        self.assertFalse(main.seed_last_occurrence(self.SCHEDULE, state))
        self.assertEqual(state['last_occurrence'], '2026-09-19T05:00:00+00:00')

    def test_an_unusable_schedule_or_timestamp_does_not_break_the_tick(self):
        import main
        broken_clock = {'last_run': 'not a timestamp'}
        self.assertFalse(main.seed_last_occurrence(self.SCHEDULE, broken_clock))
        broken_schedule = dict(self.SCHEDULE, frequency='Daily')
        self.assertFalse(main.seed_last_occurrence(
            broken_schedule, {'last_run': '2026-09-20T05:00:26+00:00'}))

    def test_the_upgrade_tick_does_not_run_and_records_the_seed(self):
        """End to end: build 22's state file, a real tick, and no run."""
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True)
            settings['schedule'] = dict(self.SCHEDULE)
            fixture.store.save_settings(settings)
            # Exactly what build 22 left behind: a run, and no record of its occurrence.
            ran_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
            fixture.store.save_job_state(settings, {
                'last_run': ran_at, 'last_connectivity': fixture.store.now_iso()})
            with patch.object(fixture.main, 'sync_is_due', return_value=False), \
                    patch.object(fixture.main, 'run') as never:
                self.assertEqual(fixture.main.tick(), 0)
                never.assert_not_called()
            self.assertTrue(fixture.store.job_state(settings).get('last_occurrence'),
                            'the seed must persist, or every tick recomputes it')
            fixture.sonarr.assert_finished()
