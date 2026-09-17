"""One-time removal requests cannot borrow permission from a later queue entry."""
import copy
import unittest
import urllib.error
from unittest.mock import patch

import context  # noqa: F401
from fake_sonarr import IsolatedWorker, SERIES, episode_payload
from core import Rejected
import actions


class RemovalIdentity(unittest.TestCase):
    def test_request_identity_survives_load_and_unrelated_save(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            request_id = settings['rules'][0]['queue']['removal'].get('request_id')
            self.assertTrue(request_id)
            loaded = f.store.load_settings()
            self.assertEqual(loaded['rules'][0]['queue']['removal']['request_id'], request_id)
            draft = copy.deepcopy(loaded)
            draft['rules'][0]['keep_days'] = 90
            actions.action_settings(loaded, {'settings': draft})
            self.assertEqual(f.store.load_settings()['rules'][0]['queue']['removal']['request_id'], request_id)
            self.assertEqual(f.sonarr.mutations, [])

    def test_cancel_requeue_same_action_cannot_resume_old_operation(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            f.sonarr.expect('PUT', 'episode/monitor', urllib.error.URLError('lost reply'),
                body={'episodeIds': [101], 'monitored': False})
            f.main.run()
            old = f.store.load_intent(settings)
            current = f.store.load_settings()
            canceled = copy.deepcopy(current)
            canceled['rules'][0]['queue']['removal'] = None
            actions.action_settings(current, {'settings': canceled})
            current = f.store.load_settings()
            requeued = copy.deepcopy(current)
            requeued['rules'][0]['queue']['removal'] = {'action': 'unmonitor-all'}
            actions.action_settings(current, {'settings': requeued})
            new_request = f.store.load_settings()['rules'][0]['queue']['removal']['request_id']
            f.sonarr.expect('GET', 'series', [SERIES])
            result = f.main.run()
            self.assertEqual(result['status'], 'incomplete')
            self.assertRegex('; '.join(result['errors']), 'removal.*changed|removal.*canceled')
            f.sonarr.assert_finished()
            self.assertEqual(len(f.sonarr.mutations), 1)
            ledger = f.store.load_removal_ledger(settings)
            self.assertEqual(ledger['batches'][0]['operations'], old['operations'])
            self.assertEqual(f.store.load_settings()['rules'][0]['queue']['removal']['request_id'], new_request)

    def test_stale_settings_cannot_restore_canceled_request_id(self):
        with IsolatedWorker() as f:
            current = f.settings()
            stale = copy.deepcopy(current)
            canceled = copy.deepcopy(current)
            canceled['rules'][0]['queue']['removal'] = None
            actions.action_settings(current, {'settings': canceled})
            current = f.store.load_settings()
            with self.assertRaisesRegex(Rejected, 'removal.*changed|removal.*canceled'):
                actions.action_settings(current, {'settings': stale})
            self.assertIsNone(f.store.load_settings()['rules'][0]['queue']['removal'])
            self.assertEqual(f.sonarr.mutations, [])

    def test_legacy_saved_operation_without_identity_is_not_authorized(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            operation = {'kind': 'set-monitored', 'rule_id': 'r1', 'instance_id': 'fake',
                         'series_id': 1, 'status': 'failed', 'removal_action': 'unmonitor-all',
                         'episode_ids': [101], 'monitored': False}
            intent = {'id': 'legacy', 'operations': [operation], 'status': 'incomplete'}
            f.store.save_intent(settings, intent)
            with self.assertRaisesRegex(Rejected, 'lacks request identity'):
                f.main._resume_intent(settings, intent)
            self.assertEqual(f.store.load_intent(settings), intent)
            self.assertEqual(f.sonarr.requests, [])

    def test_cancel_after_staging_prevents_dispatch(self):
        with IsolatedWorker() as f:
            settings = f.settings(test_mode=False)
            operation = f.main._operation('set-monitored', settings['rules'][0],
                removal_action='unmonitor-all', episode_ids=[101], monitored=False)
            intent = {'id': 'staged', 'operations': [operation]}
            canceled = copy.deepcopy(settings)
            canceled['rules'][0]['queue']['removal'] = None
            actions.action_settings(settings, {'settings': canceled})
            f.main._execute_operation(settings, intent, operation)
            self.assertEqual(operation['status'], 'failed')
            self.assertIn('canceled', operation['error'])
            self.assertEqual(f.sonarr.requests, [])
            self.assertEqual(f.store.load_intent(settings), intent)

    def test_finalization_preserves_newer_settings_and_removal_request(self):
        for change in ('unrelated', 'canceled', 'requeued', 'target'):
            with self.subTest(change=change), IsolatedWorker() as f:
                settings = f.settings(test_mode=False)
                settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                    path='/tv/Second', series_title='Second', queue={}, enabled=False))
                f.store.save_settings(settings)
                f.sonarr.expect('GET', 'series', [SERIES, dict(
                    SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)])
                f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                f.sonarr.expect('PUT', 'episode/monitor',
                    body={'episodeIds': [101], 'monitored': False})
                finish = f.main._finish_removals
                expected = {}

                def edit_then_finish(stale, intent):
                    current = f.store.load_settings()
                    draft = copy.deepcopy(current)
                    draft['rules'][1]['keep_days'] = 123
                    if change in ('canceled', 'requeued'):
                        draft['rules'][0]['queue']['removal'] = None
                        actions.action_settings(current, {'settings': draft})
                        current = f.store.load_settings()
                        draft = copy.deepcopy(current)
                        if change == 'requeued':
                            draft['rules'][0]['queue']['removal'] = {'action': 'unmonitor-all'}
                    elif change == 'target':
                        draft['rules'][0]['series_id'] = 3
                    actions.action_settings(current, {'settings': draft})
                    expected.update(f.store.load_settings())
                    finish(stale, intent)

                with patch.object(f.main, '_finish_removals', side_effect=edit_then_finish):
                    f.main.run()
                actual = f.store.load_settings()
                if change == 'unrelated':
                    expected['rules'] = [r for r in expected['rules'] if r['id'] != 'r1']
                self.assertEqual(actual, expected)
                self.assertEqual(len(f.sonarr.mutations), 1)
                f.sonarr.assert_finished()


if __name__ == '__main__':
    unittest.main()
