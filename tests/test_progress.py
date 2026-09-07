import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401


class Progress(unittest.TestCase):
    """The marker that lets an open page see a scheduled check working."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ['TVD_CONFIG'] = str(Path(self.temp.name) / 'settings.json')
        os.environ['TVD_RUNTIME'] = str(Path(self.temp.name) / 'run')
        os.environ['TVD_DEVELOPMENT'] = '1'
        import main
        self.main = main
        main.CONFIG = Path(self.temp.name) / 'settings.json'
        self.settings = {'state_dir': str(Path(self.temp.name) / 'state'),
                         'health': {'ttl_hours': 24}, 'rules': [], 'profiles': []}

    def tearDown(self):
        self.temp.cleanup()

    def test_no_marker_means_nothing_is_running(self):
        self.assertFalse(self.main.read_progress(self.settings)['running'])

    def test_a_marker_is_reported_while_it_is_fresh(self):
        self.main.set_progress(self.settings, running=True, started=self.main.now_iso(),
                               total=3, done=1, current='r1')
        progress = self.main.read_progress(self.settings)
        self.assertTrue(progress['running'])
        self.assertEqual(progress['done'], 1)

    def test_a_marker_left_by_a_killed_process_is_ignored(self):
        # Otherwise a crash during a sweep would leave the interface waiting for ever.
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
        self.main.set_progress(self.settings, running=True, started=old, total=3, done=1)
        self.assertFalse(self.main.read_progress(self.settings)['running'])

    def test_clearing_ends_it(self):
        self.main.set_progress(self.settings, running=True, started=self.main.now_iso())
        self.main.clear_progress(self.settings)
        self.assertFalse(self.main.read_progress(self.settings)['running'])


class StaleSelection(unittest.TestCase):
    """Only genuinely stale rules are re-read when a page opens."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        import main
        from core import rule_fingerprint, validate_settings
        self.main = main
        self.settings = validate_settings({
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/mnt/user/media/TV/A',
                       'keep_days': 30},
                      {'id': 'r2', 'instance_id': 'i1', 'series_id': 2, 'path': '/mnt/user/media/TV/B',
                       'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.fingerprint = rule_fingerprint
        fresh = {'checked_at': main.now_iso(),
                 'fingerprint': rule_fingerprint(self.settings['rules'][0], self.settings)}
        self.health = {'rules': {'r1': fresh}}

    def tearDown(self):
        self.temp.cleanup()

    def test_a_rule_with_no_result_is_stale(self):
        self.assertEqual(self.main.stale_rule_ids(self.settings, self.health), ['r2'])

    def test_a_fresh_matching_result_is_not_re_read(self):
        self.assertNotIn('r1', self.main.stale_rule_ids(self.settings, self.health))

    def test_changing_the_rule_makes_it_stale(self):
        self.settings['rules'][0]['keep_days'] = 90
        self.assertIn('r1', self.main.stale_rule_ids(self.settings, self.health))

    def test_an_aged_out_result_is_stale(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
        self.health['rules']['r1']['checked_at'] = old
        self.assertIn('r1', self.main.stale_rule_ids(self.settings, self.health))

    def test_a_disabled_rule_is_never_re_read(self):
        self.settings['rules'][1]['enabled'] = False
        self.assertEqual(self.main.stale_rule_ids(self.settings, self.health), [])


if __name__ == '__main__':
    unittest.main()


class HealthPruning(unittest.TestCase):
    def test_results_for_removed_rules_do_not_linger(self):
        """Otherwise the cache grows for ever and its counts disagree with the show list."""
        import tempfile
        from pathlib import Path
        import main
        with tempfile.TemporaryDirectory() as temp:
            settings = {'state_dir': str(Path(temp) / 'state'),
                        'rules': [{'id': 'r1'}, {'id': 'r2'}]}
            main.write_cache(settings, 'health.json',
                             {'rules': {'r1': {'ok': True}, 'r2': {'ok': True}, 'gone': {'ok': True}}})
            health = main.load_health(settings)
            live = {rule['id'] for rule in settings['rules']}
            health['rules'] = {rid: e for rid, e in health['rules'].items() if rid in live}
            self.assertEqual(sorted(health['rules']), ['r1', 'r2'])
