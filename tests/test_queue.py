"""The queue: nothing a card offers happens until a run applies it."""
import unittest

import context  # noqa: F401
from core import REMOVAL_ACTIONS, REMOVAL_CONFIRMATIONS, Rejected, validate_settings


def base(**rule_extra):
    rule = {'id': 'r1', 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
            'path': '/mnt/user/media/TV/Show', 'keep_days': 30}
    rule.update(rule_extra)
    return {'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [rule]}


class Removals(unittest.TestCase):
    def test_the_default_action_leaves_sonarr_alone(self):
        self.assertEqual(list(REMOVAL_ACTIONS)[0], 'remove')

    def test_only_the_two_destructive_actions_need_a_typed_word(self):
        self.assertEqual(sorted(REMOVAL_CONFIRMATIONS), ['delete-series', 'delete-series-files'])
        self.assertEqual(REMOVAL_CONFIRMATIONS['delete-series'], 'DELETE')
        self.assertEqual(REMOVAL_CONFIRMATIONS['delete-series-files'], 'DELETE ALL')

    def test_a_queued_removal_survives_validation(self):
        settings = validate_settings(base(queue={'removal': {'action': 'delete-series-files',
                                                             'created_at': '2026-09-07T10:00:00+00:00'}}))
        self.assertEqual(settings['rules'][0]['queue']['removal']['action'], 'delete-series-files')

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(Rejected):
            validate_settings(base(queue={'removal': {'action': 'nuke-everything'}}))

    def test_no_queue_is_the_normal_state(self):
        self.assertIsNone(validate_settings(base())['rules'][0]['queue']['removal'])


class Fixes(unittest.TestCase):
    def test_a_queued_fix_survives_validation(self):
        settings = validate_settings(base(queue={'fixes': [{'kind': 'unmonitor-out-frame'}]}))
        self.assertEqual([f['kind'] for f in settings['rules'][0]['queue']['fixes']], ['unmonitor-out-frame'])

    def test_the_same_fix_is_not_queued_twice(self):
        settings = validate_settings(base(queue={'fixes': [{'kind': 'monitor-in-frame'},
                                                           {'kind': 'monitor-in-frame'}]}))
        self.assertEqual(len(settings['rules'][0]['queue']['fixes']), 1)

    def test_an_unknown_fix_is_refused(self):
        with self.assertRaises(Rejected):
            validate_settings(base(queue={'fixes': [{'kind': 'delete-everything'}]}))


class Overrides(unittest.TestCase):
    """Three global rules a series may disagree with."""

    def test_each_override_is_three_state(self):
        settings = validate_settings(base(include_specials='yes', auto_unmonitor='no', auto_monitor=''))
        rule = settings['rules'][0]
        self.assertIs(rule['include_specials'], True)
        self.assertIs(rule['auto_unmonitor'], False)
        self.assertIsNone(rule['auto_monitor'])

    def test_unset_means_inherit(self):
        rule = validate_settings(base())['rules'][0]
        for name in ('include_specials', 'auto_unmonitor', 'auto_monitor'):
            self.assertIsNone(rule[name])

    def test_nonsense_is_refused(self):
        with self.assertRaises(Rejected):
            validate_settings(base(auto_monitor='sometimes'))


class SafetyMoved(unittest.TestCase):
    def test_the_import_date_fallback_is_a_guard_not_a_global_rule(self):
        settings = validate_settings({})
        self.assertIn('allow_import_date_fallback', settings['guards'])
        self.assertNotIn('allow_mtime_fallback', settings['retention'])

    def test_searching_after_monitoring_is_off_by_default(self):
        # Monitoring many episodes at once means downloading many at once.
        self.assertFalse(validate_settings({})['retention']['search_after_monitor'])


if __name__ == '__main__':
    unittest.main()


class UnmonitorOnDelete(unittest.TestCase):
    def test_deleting_a_file_always_unmonitors_it(self):
        """Not a setting: a deleted file left monitored is a fetch-and-delete loop.

        Auto unmonitor governs the episodes *around* the deletion — the ones outside the
        window that have no file — never the deletion itself.
        """
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'src' / 'tv-delete' / 'worker' / 'main.py').read_text()
        block = source.split('if deleted_ids and not dry_run:')[1].split('if (settings.get(')[0]
        self.assertIn('client.unmonitor(deleted_ids)', block)
        self.assertNotIn('auto_unmonitor', block)
        self.assertNotIn("rule.get('unmonitor')", block)
