"""Operator resolution retires uncertain removal intent without claiming success."""
import copy
import unittest
import urllib.error
from unittest.mock import patch

import context  # noqa: F401
import actions
from fake_sonarr import IsolatedWorker, SERIES


class RemovalResolution(unittest.TestCase):
    def test_cancel_reviewed_removal_preserves_uncertainty_and_prevents_retry(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal='delete-series')
            settings['rules'][0]['enabled'] = False
            fixture.store.save_settings(settings)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            fixture.sonarr.expect('GET', 'series/1', SERIES)
            fixture.sonarr.expect('GET', 'series/1', SERIES)
            fixture.sonarr.expect('DELETE', 'series/1', urllib.error.URLError('lost delete reply'),
                                  query={'deleteFiles': ['false'], 'addImportListExclusion': ['false']})
            first = fixture.main.run()
            self.assertEqual(first['status'], 'incomplete')
            queued = fixture.store.load_settings()['rules'][0]['queue']['removal']
            response = actions.dispatch({'action': 'resolve-removal', 'rule_id': 'r1',
                                         'request_id': queued['request_id'],
                                         'removal_action': queued['action']})
            self.assertTrue(response['ok'], response)
            self.assertIsNone(fixture.store.load_settings()['rules'][0]['queue']['removal'])
            intent = fixture.store.load_intent(settings)
            self.assertEqual(intent['operations'][0]['status'], 'resolved')
            self.assertIn('operator', intent['operations'][0]['error'].lower())
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            fixture.sonarr.assert_finished()

            fixture.sonarr.expect('GET', 'series', [SERIES])
            second = fixture.main.run()
            self.assertEqual(second['status'], 'complete')
            self.assertEqual(fixture.sonarr.mutations, [fixture.sonarr.mutations[0]])
            fixture.sonarr.assert_finished()

    def test_stale_resolution_cannot_cancel_a_requeued_request(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal='delete-series')
            old = copy.deepcopy(settings['rules'][0]['queue']['removal'])
            current = copy.deepcopy(settings)
            current['rules'][0]['queue']['removal'] = {'action': 'delete-series'}
            actions.action_settings(settings, {'settings': current})
            response = actions.dispatch({'action': 'resolve-removal', 'rule_id': 'r1',
                                         'request_id': old['request_id'],
                                         'removal_action': old['action']})
            self.assertFalse(response['ok'], response)
            self.assertIsNotNone(fixture.store.load_settings()['rules'][0]['queue']['removal'])
            self.assertEqual(fixture.sonarr.requests, [])

    def test_settings_failure_leaves_queue_for_retry_after_review_is_saved(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal='delete-series')
            current = fixture.store.load_settings()
            queued = current['rules'][0]['queue']['removal']
            operation = fixture.main._operation('delete-series', current['rules'][0],
                removal_action='delete-series', delete_files=False)
            operation.update(status='failed', error='cannot verify absence')
            intent = {'id': 'resolution-save-failure', 'status': 'incomplete',
                      'operations': [operation], 'removals': [{
                          'rule_id': 'r1', 'action': 'delete-series', 'request_id': queued['request_id'],
                          'ok': False, 'error': 'cannot verify absence'}]}
            fixture.store.save_intent(settings, intent)
            with patch.object(fixture.main, 'save_settings', side_effect=OSError('settings unavailable')):
                response = actions.dispatch({'action': 'resolve-removal', 'rule_id': 'r1',
                                             'request_id': queued['request_id'],
                                             'removal_action': 'delete-series'})
            self.assertFalse(response['ok'], response)
            self.assertIsNotNone(fixture.store.load_settings()['rules'][0]['queue']['removal'])
            saved = fixture.store.load_intent(settings)
            self.assertEqual(saved['operations'][0]['status'], 'resolved')
            self.assertEqual(saved['removals'][0]['resolution'], 'operator-canceled')


if __name__ == '__main__':
    unittest.main()
