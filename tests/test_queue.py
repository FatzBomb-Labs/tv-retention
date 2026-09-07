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

    def test_specials_override_is_three_state(self):
        rule = validate_settings(base(include_specials='yes'))['rules'][0]
        self.assertIs(rule['include_specials'], True)
        self.assertIs(validate_settings(base(include_specials='no'))['rules'][0]['include_specials'], False)
        self.assertIsNone(validate_settings(base())['rules'][0]['include_specials'])

    def test_monitoring_missing_episodes_is_off_unless_chosen(self):
        # Monitoring an episode with no file starts a download, so it is never a default.
        self.assertFalse(validate_settings(base())['rules'][0]['monitor_missing'])
        self.assertTrue(validate_settings(base(monitor_missing=True))['rules'][0]['monitor_missing'])

    def test_nonsense_is_refused(self):
        with self.assertRaises(Rejected):
            validate_settings(base(include_specials='sometimes'))


class SafetyMoved(unittest.TestCase):
    def test_the_safety_section_is_gone_with_the_filesystem(self):
        # Sonarr owns the filesystem, so the guards that protected against a bad path
        # mapping have nothing left to protect against.
        settings = validate_settings({})
        self.assertNotIn('guards', settings)
        self.assertTrue(settings['retention']['allow_estimated_dates'])

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


class LoadNormalises(unittest.TestCase):
    def test_a_rule_written_before_a_field_existed_still_gets_it(self):
        """Validation runs on load, not only on save.

        Merging a stored document over the defaults leaves rules exactly as last written,
        so a field added since is simply absent and the interface has nowhere to put it.
        That is how queued removals silently failed to persist.
        """
        import json
        import tempfile
        from pathlib import Path
        import main
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            path.write_text(json.dumps({
                'settings_version': 4,
                'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
                # A rule as an older release would have written it: no queue, no overrides.
                'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                           'keep_days': 30}],
            }))
            original = main.CONFIG
            main.CONFIG = path
            try:
                rule = main.load_settings()['rules'][0]
            finally:
                main.CONFIG = original
            self.assertIn('queue', rule)
            self.assertEqual(rule['queue'], {'removal': None, 'fixes': []})
            self.assertIn('monitor_missing', rule)
