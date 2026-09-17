"""Focused executor regressions using the ordinary Linux worker import."""
import unittest
from unittest.mock import Mock, patch

import context  # noqa: F401
import main
from sonarr import SonarrError


class RunExecutorReview(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.operation = {'kind': 'set-monitored', 'instance_id': 'fake',
                          'series_id': 1, 'episode_ids': [101], 'monitored': False,
                          'status': 'pending', 'attempts': 0, 'error': ''}
        self.intent = {'operations': [self.operation]}
        self.checkpoints = []
        self.lookup = self.enter_patch('client_for', return_value=self.client)
        self.save = self.enter_patch('save_intent', side_effect=self.checkpoint)

    def enter_patch(self, name, **kwargs):
        patcher = patch.object(main, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def checkpoint(self, settings, current):
        self.assertIs(current, self.intent)
        self.checkpoints.append(self.operation['status'])

    def test_staged_monitoring_operation_is_executed_and_completed(self):
        main._execute_operation({}, self.intent, self.operation)
        self.client.set_monitored.assert_called_once_with([101], False)
        self.assertEqual(self.operation['status'], 'done')
        self.assertEqual(self.operation['attempts'], 1)
        self.assertEqual(self.checkpoints, ['in-progress', 'done'])

    def test_failed_before_checkpoint_prevents_external_call(self):
        self.save.side_effect = OSError('fixture disk full')
        with self.assertRaises(OSError):
            main._execute_operation({}, self.intent, self.operation)
        self.lookup.assert_not_called()
        self.assertEqual(self.operation['status'], 'in-progress')

    def test_failed_sonarr_write_is_checkpointed_not_completed(self):
        self.client.set_monitored.side_effect = SonarrError('fixture unavailable')
        main._execute_operation({}, self.intent, self.operation)
        self.assertEqual(self.operation['status'], 'failed')
        self.assertEqual(self.checkpoints, ['in-progress', 'failed'])

    def test_failed_after_checkpoint_propagates_uncertainty(self):
        self.save.side_effect = [None, OSError('fixture disk full')]
        with self.assertRaises(OSError):
            main._execute_operation({}, self.intent, self.operation)
        self.client.set_monitored.assert_called_once_with([101], False)

    def test_remove_rule_does_not_require_sonarr(self):
        self.operation['kind'] = 'remove-rule'
        main._execute_operation({}, self.intent, self.operation)
        self.lookup.assert_not_called()
        self.assertEqual(self.operation['status'], 'done')

    def test_unknown_operation_is_not_reported_as_success(self):
        self.operation['kind'] = 'unknown'
        main._execute_operation({}, self.intent, self.operation)
        self.assertEqual(self.operation['status'], 'failed')
        self.assertEqual(self.client.mock_calls, [])

    def test_recovery_only_reads_unfinished_monitoring(self):
        self.client.episodes.return_value = [{'episode_id': 101, 'monitored': True}]
        main._resume_intent({}, self.intent)
        self.assertEqual(self.client.mock_calls, [
            unittest.mock.call.episodes(1, files_only=False)])
        self.assertEqual(self.operation['status'], 'pending')

    def test_empty_recovery_does_not_dispatch_a_last_operation(self):
        self.intent['operations'] = []
        main._resume_intent({}, self.intent)
        self.lookup.assert_not_called()
        self.save.assert_called_once_with({}, self.intent)


if __name__ == '__main__':
    unittest.main()
