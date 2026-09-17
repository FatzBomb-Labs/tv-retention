"""Ordinary retention starts fresh after failure; old operations are audit, not authority."""
import datetime as dt
from io import BytesIO
import json
import unittest
import urllib.parse
import urllib.error
from unittest.mock import patch

import context  # noqa: F401
from fake_sonarr import IsolatedWorker, SERIES, episode_payload


class RetentionRetry(unittest.TestCase):
    def test_changed_permission_does_not_replay_failed_deletion(self):
        for change in ('window', 'exclusion', 'disabled', 'subset', 'file-gone'):
            with self.subTest(change=change):
                self.check_fresh_retry(change)

    def check_fresh_retry(self, change):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['rules'][0]['queue'] = {}
            fixture.store.save_settings(settings)
            row = episode_payload()
            row['airDateUtc'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).isoformat()
            fixture.sonarr.expect('GET', 'series', [SERIES])
            fixture.sonarr.expect('GET', 'episode', [row], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [101], 'monitored': False})
            fixture.sonarr.expect('DELETE', 'episodefile/99',
                urllib.error.URLError('fixture acknowledgement lost'))
            first = fixture.main.run()
            self.assertEqual(first['status'], 'incomplete')
            old_intent = fixture.store.load_intent(settings)
            current = fixture.store.load_settings()
            if change == 'window':
                current['rules'][0]['keep_days'] = 90
            elif change == 'exclusion':
                current['rules'][0]['exclusions'] = [{'season': 1, 'episode': 1}]
            elif change == 'disabled':
                current['rules'][0]['enabled'] = False
            fixture.store.save_settings(current)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            if change not in ('disabled', 'subset'):
                fresh = dict(row, monitored=False)
                if change == 'file-gone':
                    fresh.update(hasFile=False, episodeFileId=0, episodeFile=None)
                fixture.sonarr.expect('GET', 'episode', [fresh], query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            second = fixture.main.run(rule_ids=['other'] if change == 'subset' else None,
                                      scheduled=True)
            fixture.sonarr.assert_finished()
            self.assertEqual(len(fixture.sonarr.mutations), 2)
            self.assertEqual(second['status'], 'complete')
            self.assertEqual(second['planned'], 0)
            self.assertNotEqual(second['id'], first['id'])
            self.assertEqual(fixture.store.load_intent(settings)['id'], second['id'])
            archives = list((fixture.root / 'state' / 'run-history').glob('*.json'))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text()), old_intent)
            self.assertEqual(second['deleted'], 0)
            self.assertEqual(second['freed_bytes'], 0)

    def test_new_exclusion_prevents_stale_delete_independent_of_read_order(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['rules'][0]['queue'] = {}
            fixture.store.save_settings(settings)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [101], 'monitored': False})
            fixture.sonarr.expect('DELETE', 'episodefile/99', urllib.error.URLError('lost reply'))
            fixture.main.run()
            fixture.sonarr.assert_finished()
            current = fixture.store.load_settings()
            current['rules'][0]['exclusions'] = [{'season': 1, 'episode': 1}]
            fixture.store.save_settings(current)
            writes = []

            def respond(request, **kwargs):
                parsed = urllib.parse.urlsplit(request.full_url)
                self.assertEqual(f'{parsed.scheme}://{parsed.netloc}', fixture.sonarr.url)
                method = request.get_method()
                if method != 'GET':
                    writes.append((method, parsed.path))
                    return BytesIO(b'')
                payloads = {'/api/v3/series': [SERIES],
                            '/api/v3/episode': [episode_payload(monitored=False)]}
                self.assertIn(parsed.path, payloads)
                return BytesIO(json.dumps(payloads[parsed.path]).encode())

            # Accept either read order; assert the safety outcome, not implementation order.
            with patch('urllib.request.urlopen', side_effect=respond):
                fixture.main.run(scheduled=True)
            self.assertEqual(writes, [], 'new exclusion must forbid the old deletion')

    def test_completed_removal_does_not_authorize_stale_ordinary_delete(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                path='/tv/Second', series_title='Second', queue={}))
            fixture.store.save_settings(settings)
            series = [SERIES, dict(SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)]
            row = dict(episode_payload(102, file_id=199), seriesId=2)
            fixture.sonarr.expect('GET', 'series', series)
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('GET', 'episode', [row], query={
                'seriesId': ['2'], 'includeEpisodeFile': ['true']})
            for episode_id in (101, 102):
                fixture.sonarr.expect('PUT', 'episode/monitor',
                    body={'episodeIds': [episode_id], 'monitored': False})
            fixture.sonarr.expect('DELETE', 'episodefile/199', urllib.error.URLError('lost reply'))
            first = fixture.main.run()
            fixture.sonarr.assert_finished()
            self.assertEqual(first['status'], 'incomplete')
            old = fixture.store.load_intent(settings)
            current = fixture.store.load_settings()
            self.assertEqual([r['id'] for r in current['rules']], ['r2'])
            current['rules'][0]['exclusions'] = [{'season': 1, 'episode': 1}]
            fixture.store.save_settings(current)
            writes = []

            def respond(request, **kwargs):
                parsed = urllib.parse.urlsplit(request.full_url)
                self.assertEqual(f'{parsed.scheme}://{parsed.netloc}', fixture.sonarr.url)
                if request.get_method() != 'GET':
                    writes.append((request.get_method(), parsed.path))
                    return BytesIO(b'')
                payloads = {'/api/v3/series': series,
                            '/api/v3/episode': [dict(row, monitored=False)]}
                self.assertIn(parsed.path, payloads)
                return BytesIO(json.dumps(payloads[parsed.path]).encode())

            with patch('urllib.request.urlopen', side_effect=respond):
                second = fixture.main.run(scheduled=True)
            self.assertEqual(writes, [], 'completed removal must not preserve old deletion permission')
            self.assertNotEqual(first['id'], second['id'])
            self.assertEqual(second['deleted'], 0)
            self.assertEqual(second['freed_bytes'], 0)
            archives = list((fixture.root / 'state' / 'run-history').glob('*.json'))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text()), old)

    def test_completed_removal_awaiting_local_finalization_is_not_repeated(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [101], 'monitored': False})
            with patch.object(fixture.main, '_finish_removals', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    fixture.main.run()
            fixture.sonarr.assert_finished()
            old = fixture.store.load_intent(settings)
            self.assertEqual(old['operations'][0]['status'], 'done')
            # No further external read/write is needed to finalize acknowledged work.
            second = fixture.main.run()
            self.assertNotEqual(second['id'], old['id'])
            self.assertEqual(second['status'], 'complete')
            ledger = fixture.store.load_removal_ledger(settings)
            self.assertEqual(ledger['batches'][0]['operations'], old['operations'])
            archives = list((fixture.root / 'state' / 'run-history').glob('*.json'))
            self.assertIn(old, [json.loads(path.read_text()) for path in archives])
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            self.assertEqual(fixture.store.load_settings()['rules'], [])
            fixture.sonarr.assert_finished()

    def test_archive_failure_preserves_intent_and_prevents_writes(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['rules'][0]['queue'] = {}
            fixture.store.save_settings(settings)
            old = {'id': 'failed-run', 'status': 'incomplete', 'operations': [],
                   'rules': [], 'removals': []}
            fixture.store.save_intent(settings, old)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            with patch.object(fixture.main, 'archive_intent', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    fixture.main.run()
            self.assertEqual(fixture.store.load_intent(settings), old)
            self.assertEqual(fixture.sonarr.mutations, [])
            fixture.sonarr.assert_finished()


if __name__ == '__main__':
    unittest.main()
