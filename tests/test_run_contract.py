"""Linux public-run contracts: real persistence and Sonarr mapping, scripted transport."""
import unittest
import urllib.error
import urllib.request

import context  # noqa: F401
from fake_sonarr import FakeSonarr, IsolatedWorker, SERIES, episode_payload
from sonarr import Sonarr, SonarrError


class TransportContract(unittest.TestCase):
    def test_unexpected_target_fails_without_network(self):
        fake = FakeSonarr()
        with self.assertRaisesRegex(AssertionError, 'Unexpected outbound target'):
            fake.urlopen(urllib.request.Request('http://unexpected.invalid/api/v3/series'))

    def test_changed_shared_and_fileless_payloads_use_real_mapping(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            client = Sonarr(settings['instances'][0])
            query = {'seriesId': ['1'], 'includeEpisodeFile': ['true']}
            fixture.sonarr.expect('GET', 'episode', [episode_payload(),
                episode_payload(102, number=2), episode_payload(103, number=3, file_id=None)],
                query=query)
            fixture.sonarr.expect('GET', 'episode', [episode_payload(file_id=100)], query=query)
            first = client.episodes(1, files_only=False)
            second = client.episodes(1, files_only=False)
            self.assertEqual([row['file_id'] for row in first], [99, 99, None])
            self.assertFalse(first[2]['has_file'])
            self.assertEqual(second[0]['file_id'], 100)
            fixture.sonarr.assert_finished()

    def test_lost_acknowledgement_is_recorded_as_attempt_not_success(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            fixture.sonarr.expect('PUT', 'episode/monitor',
                urllib.error.URLError('fixture acknowledgement lost'),
                body={'episodeIds': [101], 'monitored': False})
            with self.assertRaises(SonarrError):
                Sonarr(settings['instances'][0]).set_monitored([101], False)
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            fixture.sonarr.assert_finished()


class PublicRunContract(unittest.TestCase):
    def test_test_mode_manual_and_scheduled_runs_never_write_or_save_intent(self):
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled), IsolatedWorker() as fixture:
                settings = fixture.settings()
                before = fixture.store.CONFIG.read_bytes()
                fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                result = fixture.main.run(scheduled=scheduled)
                self.assertTrue(result['test_mode'])
                self.assertEqual(result['scheduled'], scheduled)
                self.assertEqual(fixture.sonarr.mutations, [])
                self.assertEqual(fixture.store.CONFIG.read_bytes(), before)
                self.assertFalse((fixture.root / 'state').exists())
                fixture.sonarr.assert_finished()

    def test_live_manual_and_scheduled_runs_dispatch_staged_monitoring(self):
        # Ordinary run, real persistence and mapping: a successful summary alone must
        # never hide a missing external request or an unfinished operation checkpoint.
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled), IsolatedWorker() as fixture:
                settings = fixture.settings(test_mode=False)
                fixture.sonarr.expect('GET', 'series', [SERIES])
                fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                fixture.sonarr.expect('PUT', 'episode/monitor',
                    body={'episodeIds': [101], 'monitored': False})
                fixture.main.run(scheduled=scheduled)
                fixture.sonarr.assert_finished()
                self.assertEqual(fixture.sonarr.mutations, [
                    ('PUT', '/api/v3/episode/monitor', {},
                     {'episodeIds': [101], 'monitored': False})])
                intent = fixture.store.load_intent(settings)
                self.assertEqual(intent['status'], 'complete')
                self.assertEqual(intent['operations'][0]['status'], 'done')
                self.assertEqual(fixture.store.load_settings()['rules'], [])

    def test_failed_first_write_stops_next_operation_and_reports_incomplete(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                                          path='/tv/Second', series_title='Second'))
            fixture.store.save_settings(settings)
            fixture.sonarr.expect('GET', 'series', [SERIES, dict(
                SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)])
            for series_id in (1, 2):
                fixture.sonarr.expect('GET', 'episode', [episode_payload(100 + series_id)],
                    query={'seriesId': [str(series_id)], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                urllib.error.URLError('fixture write failed'),
                body={'episodeIds': [101], 'monitored': False})
            result = fixture.main.run()
            fixture.sonarr.assert_finished()
            self.assertEqual(len(fixture.sonarr.mutations), 1)
            intent = fixture.store.load_intent(settings)
            self.assertEqual(intent['status'], 'incomplete')
            self.assertEqual([op['status'] for op in intent['operations']], ['failed', 'pending'])
            self.assertEqual([op['attempts'] for op in intent['operations']], [1, 0])
            self.assertEqual(len(fixture.store.load_settings()['rules']), 2)
            self.assertTrue(result['errors'])
            self.assertFalse(result['removals'][0]['ok'])
            self.assertFalse(result['removals'][1]['ok'])
            self.assertEqual(result['status'], 'incomplete')

    def test_restart_after_failed_write_retries_only_the_failed_operation(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            settings['rules'].append(dict(settings['rules'][0], id='r2', series_id=2,
                                          path='/tv/Second', series_title='Second'))
            fixture.store.save_settings(settings)
            fixture.sonarr.expect('GET', 'series', [SERIES, dict(
                SERIES, id=2, title='Second', path='/tv/Second', tvdbId=20)])
            for series_id in (1, 2):
                fixture.sonarr.expect('GET', 'episode', [episode_payload(100 + series_id)],
                    query={'seriesId': [str(series_id)], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                urllib.error.URLError('fixture write failed'),
                body={'episodeIds': [101], 'monitored': False})
            fixture.main.run()
            # Restart: recovery re-reads both unfinished series (read-only), then the
            # loop retries only the failed write and runs the never-attempted one once.
            fixture.sonarr.expect('GET', 'episode', [episode_payload()],
                query={'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('GET', 'episode', [episode_payload(102)],
                query={'seriesId': ['2'], 'includeEpisodeFile': ['true']})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [101], 'monitored': False})
            fixture.sonarr.expect('PUT', 'episode/monitor',
                body={'episodeIds': [102], 'monitored': False})
            result = fixture.main.run()
            fixture.sonarr.assert_finished()
            self.assertEqual(result['status'], 'complete')
            intent = fixture.store.load_intent(settings)
            self.assertEqual(intent['status'], 'complete')
            self.assertEqual([op['status'] for op in intent['operations']], ['done', 'done'])
            self.assertEqual([op['attempts'] for op in intent['operations']], [2, 1])
            self.assertEqual(fixture.store.load_settings()['rules'], [])


if __name__ == '__main__':
    unittest.main()
