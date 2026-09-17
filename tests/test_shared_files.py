"""Shared files expire with their latest episode, through the real Sonarr mapping."""
import datetime as dt
import unittest
import urllib.error

import context  # noqa: F401
from fake_sonarr import IsolatedWorker, SERIES, episode_payload


class SharedFiles(unittest.TestCase):
    def check_run(self, *, recent=False, excluded=False, failed_monitor=False):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            rule = settings['rules'][0]
            rule.update(queue={}, combine='all')
            if excluded:
                rule['exclusions'] = [{'season': 1, 'episode': 1}]
            fixture.store.save_settings(settings)
            rows = [episode_payload(101, number=1), episode_payload(102, number=2)]
            if recent:
                rows[1]['airDateUtc'] = (dt.datetime.now(dt.timezone.utc)
                                       - dt.timedelta(days=1)).isoformat()
            fixture.sonarr.expect('GET', 'series', [SERIES])
            fixture.sonarr.expect('GET', 'episode', rows, query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            if not recent:
                ids = [102] if excluded else [101, 102]
                fixture.sonarr.expect('PUT', 'episode/monitor',
                    urllib.error.URLError('monitor failed') if failed_monitor else None,
                    body={'episodeIds': ids, 'monitored': False})
                if not excluded and not failed_monitor:
                    fixture.sonarr.expect('DELETE', 'episodefile/99')
            result = fixture.main.run()
            fixture.sonarr.assert_finished()
            self.assertEqual(result['deleted'], int(not (recent or excluded or failed_monitor)))
            self.assertEqual(result['freed_bytes'], 1000 if result['deleted'] else 0)
            self.assertEqual(result['status'], 'incomplete' if failed_monitor else 'complete')

    def test_latest_episode_keeps_shared_file_under_all(self):
        self.check_run(recent=True)

    def test_expired_shared_file_deleted_and_counted_once(self):
        self.check_run()

    def test_excluded_sibling_protects_shared_file(self):
        self.check_run(excluded=True)

    def test_failed_unmonitor_prevents_shared_delete(self):
        self.check_run(failed_monitor=True)

    def test_unknown_or_future_sibling_is_not_permission_to_delete(self):
        from core import evaluate
        from test_retention import NOW, episode, settings
        for future in (False, True):
            with self.subTest(future=future):
                first = episode(1, 1, days_ago=100)
                second = episode(1, 2, days_ago=-1, air_date=future)
                second.update(file_id=first['file_id'], path=first['path'])
                result = evaluate([first, second], {'keep_days': 30, 'combine': 'all'},
                                  settings(retention={'allow_estimated_dates': False}), now=NOW)
                self.assertEqual(result['delete'], [])

    def test_episode_count_counts_episodes_not_files(self):
        from core import evaluate
        from test_retention import NOW, episode, settings
        rows = [episode(1, n, days_ago=10 - n) for n in range(1, 4)]
        rows[1].update(file_id=rows[2]['file_id'], path=rows[2]['path'])
        for combine in ('any', 'all'):
            with self.subTest(combine=combine):
                result = evaluate(rows, {'keep_episodes': 2, 'combine': combine}, settings(), now=NOW)
                self.assertEqual([r['episode'] for r in result['delete']], [1])
                self.assertEqual([r['episode'] for r in result['keep']], [2, 3])
