"""Blocked one-time requests must not replay or globally block ordinary retention."""
import copy
from io import BytesIO
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import context  # noqa: F401
from fake_sonarr import IsolatedWorker, SERIES, episode_payload


class MixedRecovery(unittest.TestCase):
    def test_blocked_removal_allows_fresh_independent_retention(self):
        for change in ('unavailable', 'canceled', 'excluded', 'subset'):
            with self.subTest(change=change), IsolatedWorker() as f:
                settings = f.settings(test_mode=False)
                settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                    path='/tv/Second', series_title='Second', queue={}))
                f.store.save_settings(settings)
                series = [SERIES, dict(SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)]
                ordinary = dict(episode_payload(102, file_id=199), seriesId=2)
                f.sonarr.expect('GET', 'series', series)
                for series_id, row in ((1, episode_payload()), (2, ordinary)):
                    f.sonarr.expect('GET', 'episode', [row], query={
                        'seriesId': [str(series_id)], 'includeEpisodeFile': ['true']})
                f.sonarr.expect('PUT', 'episode/monitor', urllib.error.URLError('lost reply'),
                    body={'episodeIds': [101], 'monitored': False})
                first = f.main.run()
                f.sonarr.assert_finished()
                old = copy.deepcopy(f.store.load_intent(settings))
                current = f.store.load_settings()
                if change == 'canceled':
                    current['rules'][0]['queue'] = {}
                if change == 'excluded':
                    current['rules'][1]['exclusions'] = [{'season': 1, 'episode': 1}]
                f.store.save_settings(current)
                writes, reads = [], []

                def respond(request, **kwargs):
                    parsed = urllib.parse.urlsplit(request.full_url)
                    self.assertEqual(f'{parsed.scheme}://{parsed.netloc}', f.sonarr.url)
                    if request.get_method() != 'GET':
                        body = json.loads(request.data) if request.data else None
                        writes.append((request.get_method(), parsed.path, body))
                        return BytesIO(b'')
                    reads.append((parsed.path, parsed.query))
                    if parsed.path == '/api/v3/series':
                        return BytesIO(json.dumps(series).encode())
                    self.assertEqual(parsed.path, '/api/v3/episode')
                    target = urllib.parse.parse_qs(parsed.query)['seriesId'][0]
                    if target == '1':
                        raise urllib.error.URLError('removal target unavailable')
                    self.assertEqual(target, '2')
                    return BytesIO(json.dumps([ordinary]).encode())

                with patch('urllib.request.urlopen', side_effect=respond):
                    second = f.main.run(scheduled=True, rule_ids=['r2'] if change == 'subset' else None)
                expected = [] if change == 'excluded' else [
                    ('PUT', '/api/v3/episode/monitor', {'episodeIds': [102], 'monitored': False}),
                    ('DELETE', '/api/v3/episodefile/199', None)]
                self.assertEqual(writes, expected)
                self.assertNotEqual(first['id'], second['id'])
                self.assertEqual(second['deleted'], 0 if change == 'excluded' else 1)
                self.assertEqual(second['freed_bytes'], 0 if change == 'excluded' else 1000)
                self.assertEqual(second['status'], 'complete' if change == 'subset' else 'incomplete')
                if change in ('canceled', 'subset'):
                    self.assertFalse(any('seriesId=1' in query for _, query in reads))
                archives = list((f.root / 'state' / 'run-history').glob('*.json'))
                self.assertIn(old, [json.loads(path.read_text()) for path in archives])

    def test_removal_only_history_does_not_capture_unrelated_subset(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f)
            old['rules'] = []
            f.store.save_intent(settings, old)
            settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                path='/tv/Second', series_title='Second', queue={}))
            f.store.save_settings(settings)
            series = [SERIES, dict(SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)]
            f.sonarr.expect('GET', 'series', series)
            f.sonarr.expect('GET', 'series', series)
            f.sonarr.expect('GET', 'episode', [dict(episode_payload(102, file_id=None), seriesId=2)],
                query={'seriesId': ['2'], 'includeEpisodeFile': ['true']})
            f.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [102], 'monitored': False})
            result = f.main.run(rule_ids=['r2'])
            self.assertEqual(result['status'], 'complete')
            self.assertNotEqual(result['id'], old['id'])
            ledger = f.store.load_removal_ledger(settings)
            self.assertEqual(ledger['batches'][0]['operations'], old['operations'])
            self.assertEqual(result['removals'], [])
            f.sonarr.assert_finished()

    def test_completed_intent_with_unresolved_removal_uses_ledger(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f)
            old['status'] = 'complete'
            old['rules'] = []
            f.store.save_intent(settings, old)
            settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                path='/tv/Second', series_title='Second', queue={}))
            f.store.save_settings(settings)
            series = [SERIES, dict(SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)]
            f.sonarr.expect('GET', 'series', series)
            f.sonarr.expect('GET', 'series', series)
            f.sonarr.expect('GET', 'episode', [dict(episode_payload(102, file_id=None), seriesId=2)],
                query={'seriesId': ['2'], 'includeEpisodeFile': ['true']})
            f.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [102], 'monitored': False})
            result = f.main.run(rule_ids=['r2'])
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['removals'], [])
            ledger = f.store.load_removal_ledger(settings)
            self.assertEqual(ledger['batches'][0]['operations'], old['operations'])
            f.sonarr.assert_finished()

    def test_completed_intent_with_awaiting_finalization_finalizes_locally(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f, status='done')
            old['status'] = 'complete'
            old['rules'] = []
            f.store.save_intent(settings, old)
            f.sonarr.expect('GET', 'series', [SERIES])
            result = f.main.run()
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(f.store.load_settings()['rules'], [])
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()

    def test_failed_staging_read_retries_live_request_and_finishes(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            request_id = settings['rules'][0]['queue']['removal']['request_id']
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', urllib.error.URLError('staging unavailable'),
                query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            first = f.main.run()
            old = f.store.load_intent(settings)
            self.assertEqual(first['status'], 'incomplete')
            self.assertEqual(old['operations'], [])
            self.assertIn('staging unavailable', first['errors'][0])
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()

            # Connectivity returns. The still-live queue is fresh operator intent;
            # no operation from the first run was dispatched or can be replayed.
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            f.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [101], 'monitored': False})
            second = f.main.run()
            self.assertEqual(second['status'], 'complete')
            self.assertEqual(second['errors'], [])
            self.assertEqual(f.store.load_settings()['rules'], [])
            ledger = f.store.load_removal_ledger(settings)
            operations = ledger['batches'][0]['operations']
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]['request_id'], request_id)
            self.assertEqual(operations[0]['status'], 'done')
            self.assertEqual(operations[0]['attempts'], 1)
            self.assertEqual(len(f.sonarr.mutations), 1)
            archives = list((f.root / 'state' / 'run-history').glob('*.json'))
            self.assertIn(old, [json.loads(path.read_text()) for path in archives])
            third = f.main.run()
            self.assertEqual(third['removals'], [])
            self.assertEqual(len(f.sonarr.mutations), 1)
            f.sonarr.assert_finished()

    def test_record_only_cancellation_releases_hold_without_success(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted), IsolatedWorker() as f:
                settings = f.settings(test_mode=False)
                f.sonarr.expect('GET', 'series', [SERIES])
                f.sonarr.expect('GET', 'episode', urllib.error.URLError('staging unavailable'),
                    query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                f.main.run()
                current = f.store.load_settings()
                if deleted:
                    current['rules'] = []
                else:
                    current['rules'][0]['queue'] = {}
                f.store.save_settings(current)
                if not deleted:
                    f.sonarr.expect('GET', 'series', [SERIES])
                    f.sonarr.expect('GET', 'episode', [episode_payload(file_id=None)], query={
                        'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                    f.sonarr.expect('PUT', 'episode/monitor',
                        body={'episodeIds': [101], 'monitored': False})
                result = f.main.run()
                self.assertEqual(result['status'], 'incomplete')
                self.assertIn('staging unavailable', result['errors'][0])
                self.assertIn('canceled or rule removed', result['errors'][0])
                self.assertEqual(len(f.store.load_settings()['rules']), 0 if deleted else 1)
                self.assertEqual(len(f.sonarr.mutations), 0 if deleted else 1)
                batch = f.store.load_removal_ledger(settings)['batches'][0]
                self.assertEqual(batch['operations'], [])
                self.assertTrue(batch['removals'][0]['retired_without_operation'])
                if not deleted:
                    f.sonarr.expect('GET', 'series', [SERIES])
                    f.sonarr.expect('GET', 'episode', [episode_payload(file_id=None, monitored=False)],
                        query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                self.assertEqual(f.main.run()['removals'], [])
                f.sonarr.assert_finished()

    def test_restage_checkpoint_failure_prevents_dispatch(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', urllib.error.URLError('staging unavailable'),
                query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            f.main.run()
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            save, calls = f.main.save_removal_ledger, []

            def fail_restage(settings, ledger):
                calls.append(1)
                if len(calls) == 2:
                    raise OSError('restage checkpoint unavailable')
                save(settings, ledger)

            with patch.object(f.main, 'save_removal_ledger', side_effect=fail_restage):
                with self.assertRaisesRegex(OSError, 'restage checkpoint'):
                    f.main.run()
            self.assertEqual(f.sonarr.mutations, [])
            self.assertEqual(f.store.load_removal_ledger(settings)['batches'][0]['operations'], [])
            f.sonarr.assert_finished()

    def test_record_only_restage_requires_current_original_target(self):
        for change in ('unavailable', 'replacement', 'legacy'):
            with self.subTest(change=change), IsolatedWorker() as f:
                settings = f.settings(test_mode=False)
                f.sonarr.expect('GET', 'series', [SERIES])
                f.sonarr.expect('GET', 'episode', urllib.error.URLError('staging unavailable'),
                    query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                f.main.run()
                if change == 'legacy':
                    old = f.store.load_intent(settings)
                    old['removals'][0].pop('request_id')
                    f.store.save_intent(settings, old)
                response = (urllib.error.URLError('catalogue unavailable') if change == 'unavailable'
                            else [dict(SERIES, id=2)] if change == 'replacement' else [SERIES])
                f.sonarr.expect('GET', 'series', response)
                f.sonarr.expect('GET', 'series', response)
                result = f.main.run()
                self.assertEqual(result['status'], 'incomplete')
                self.assertIn('target cannot be verified', result['errors'][0])
                self.assertEqual(f.sonarr.mutations, [])
                self.assertEqual(f.store.load_removal_ledger(settings)['batches'][0]['operations'], [])
                f.sonarr.assert_finished()

    def test_record_only_replacement_respects_other_target_owner_in_any_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), IsolatedWorker() as f:
                settings, old = self.seed_mixed(f)
                settings['rules'][0]['id'] = 'replacement'
                f.store.save_settings(settings)
                record = dict(old['removals'][0], rule_id='replacement', instance_id='fake', series_id=1,
                    request_id=settings['rules'][0]['queue']['removal']['request_id'])
                batches = [{key: old[key] for key in ('id', 'operations', 'removals')},
                           {'id': 'unstaged', 'operations': [], 'removals': [record]}]
                f.store.save_removal_ledger(settings, {'version': 1, 'batches': batches[::-1] if reverse else batches})
                f.sonarr.expect('GET', 'series', [SERIES])
                f.sonarr.expect('GET', 'series', [SERIES])
                f.sonarr.expect('GET', 'series', [SERIES])
                result = f.main.run()
                self.assertEqual(result['status'], 'incomplete')
                self.assertTrue(any('Target held' in error for error in result['errors']))
                self.assertEqual(f.sonarr.mutations, [])
                f.sonarr.assert_finished()

    def test_changed_record_only_action_reports_current_staging_failure(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', urllib.error.URLError('old staging failure'),
                query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            f.main.run()
            old = f.store.load_intent(settings)
            current = f.store.load_settings()
            current['rules'][0]['queue']['removal'] = {'action': 'delete-series-files'}
            f.store.save_settings(current)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'series/1', urllib.error.URLError('new staging failure'))
            f.sonarr.expect('GET', 'series', [SERIES])
            result = f.main.run()
            self.assertEqual(result['removals'][0]['action'], 'delete-series-files')
            self.assertIn('new staging failure', result['errors'][0])
            self.assertEqual(f.sonarr.mutations, [])
            archives = list((f.root / 'state' / 'run-history').glob('*.json'))
            self.assertIn(old, [json.loads(path.read_text()) for path in archives])
            f.sonarr.assert_finished()

    def seed_mixed(self, f, status='failed', records=True):
        settings = f.settings(test_mode=False)
        operation = f.main._operation('set-monitored', settings['rules'][0],
            removal_action='unmonitor-all', episode_ids=[101], monitored=False)
        operation.update(status=status, attempts=1)
        record = {'rule_id': 'r1', 'action': 'unmonitor-all', 'ok': True, 'error': ''}
        old = {'id': 'old-mixed', 'status': 'incomplete', 'rules': [{'rule_id': 'r2'}],
               'operations': [operation], 'removals': [record] if records else []}
        f.store.save_intent(settings, old)
        return settings, old

    def test_interrupted_ledger_checkpoints_preserve_single_owner(self):
        # Exceptions simulate interruption; this is not a process-kill test.
        for failure in (1, 2, 3, 4):
            with self.subTest(checkpoint=failure), IsolatedWorker() as f:
                settings, old = self.seed_mixed(f)
                f.sonarr.expect('GET', 'series', [SERIES])
                if failure > 1:
                    f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                        'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                if failure == 4:
                    f.sonarr.expect('PUT', 'episode/monitor',
                        body={'episodeIds': [101], 'monitored': False})
                save, calls = f.main.save_removal_ledger, []

                def interrupted(settings, ledger):
                    calls.append(1)
                    if len(calls) == failure:
                        raise OSError('injected checkpoint interruption')
                    save(settings, ledger)

                with patch.object(f.main, 'save_removal_ledger', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        f.main.run()
                self.assertEqual(f.store.load_intent(settings), old)
                self.assertEqual(len(f.sonarr.mutations), 1 if failure == 4 else 0)
                f.sonarr.expect('GET', 'episode', [episode_payload(monitored=failure != 4)], query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                if failure != 4:
                    f.sonarr.expect('PUT', 'episode/monitor',
                        body={'episodeIds': [101], 'monitored': False})
                result = f.main.run()
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(result['freed_bytes'], 0)
                self.assertEqual(f.store.load_settings()['rules'], [])
                ledger = f.store.load_removal_ledger(settings)
                self.assertEqual(len(ledger['batches']), 1)
                self.assertEqual(ledger['batches'][0]['operations'][0]['status'], 'done')
                self.assertEqual(len(f.sonarr.mutations), 1)
                # Repeated retry neither repeats completion reports nor external work.
                again = f.main.run()
                self.assertEqual(again['removals'], [])
                f.sonarr.assert_finished()

    def test_orphan_acknowledged_operation_finalizes_without_dispatch(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f, status='done', records=False)
            f.sonarr.expect('GET', 'series', [SERIES])
            result = f.main.run()
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(f.store.load_settings()['rules'], [])
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()

    def test_missing_or_corrupt_handed_off_ledger_prevents_restage(self):
        for content in (None, '{', '{}', '{"version":1,"batches":[null]}'):
            with self.subTest(content=content), IsolatedWorker() as f:
                settings, old = self.seed_mixed(f)
                f.store.save_intent(settings, dict(old, separate_removals=True,
                    operations=[], removals=[]))
                if content is not None:
                    (f.root / 'state' / 'removal-ledger.json').write_text(content)
                f.sonarr.expect('GET', 'series', [SERIES])
                with self.assertRaises((f.main.Rejected, ValueError)):
                    f.main.run()
                self.assertEqual(f.sonarr.mutations, [])
                f.sonarr.assert_finished()

    def test_changed_acknowledged_target_reports_unfinished_cleanup(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f, status='done')
            settings['rules'][0].update(series_id=2, path='/tv/Second', series_title='Second')
            f.store.save_settings(settings)
            series = [dict(SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)]
            f.sonarr.expect('GET', 'series', series)
            f.sonarr.expect('GET', 'series', series)
            result = f.main.run()
            self.assertEqual(result['status'], 'incomplete')
            self.assertIn('target changed', result['errors'][0])
            self.assertEqual(len(f.store.load_settings()['rules']), 1)
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()

    def test_replacement_rule_cannot_act_on_unresolved_target(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f)
            settings['rules'][0]['id'] = 'replacement'
            settings['rules'][0]['queue'] = {}
            f.store.save_settings(settings)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'series', [SERIES])
            result = f.main.run()
            self.assertEqual(result['status'], 'incomplete')
            self.assertTrue(any('Target held' in error for error in result['errors']))
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()

    def test_failed_replacement_intent_does_not_repeat_acknowledged_removal(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            f.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [101], 'monitored': False})
            with patch.object(f.main, 'save_intent', side_effect=OSError('replacement interrupted')):
                with self.assertRaises(OSError):
                    f.main.run()
            self.assertEqual(f.store.load_intent(settings), old)
            self.assertEqual(f.store.load_settings()['rules'], [])
            self.assertEqual(f.store.load_removal_ledger(settings)['batches'][0]['operations'][0]['status'], 'done')
            result = f.main.run()
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['removals'], [])
            self.assertEqual(len(f.sonarr.mutations), 1)
            f.sonarr.assert_finished()

    def test_overlapping_request_owners_fail_closed(self):
        with IsolatedWorker() as f:
            settings, old = self.seed_mixed(f)
            batch = {key: old[key] for key in ('id', 'operations', 'removals')}
            f.store.save_removal_ledger(settings, {'version': 1,
                'batches': [batch, dict(batch, id='duplicate-owner')]})
            f.sonarr.expect('GET', 'series', [SERIES])
            with self.assertRaisesRegex(f.main.Rejected, 'overlapping'):
                f.main.run()
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()


if __name__ == '__main__':
    unittest.main()
