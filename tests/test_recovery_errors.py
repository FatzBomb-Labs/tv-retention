"""Recovery must distinguish an absent target from an uncertain remote reading."""
import unittest
import urllib.error
from unittest.mock import patch

import context  # noqa: F401
from fake_sonarr import IsolatedWorker, SERIES, episode_payload
from sonarr import Sonarr, SonarrError


class RecoveryErrors(unittest.TestCase):
    def test_transport_preserves_http_status_without_parsing_message(self):
        for status in (401, 403, 404, 500, 503):
            with self.subTest(status=status), IsolatedWorker() as fixture:
                settings = fixture.settings()
                error = urllib.error.HTTPError(
                    fixture.sonarr.url + '/api/v3/series/1', status,
                    'fixture response', {}, None)
                with patch('urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(SonarrError) as caught:
                        Sonarr(settings['instances'][0]).series_one(1)
                self.assertEqual(caught.exception.status_code, status)

    def test_series_recovery_completes_only_on_http_404(self):
        for status in (401, 403, 404, 500, 503):
            with self.subTest(status=status), IsolatedWorker() as fixture:
                settings = fixture.settings(test_mode=False)
                operation = {'kind': 'delete-series', 'instance_id': 'fake',
                             'series_id': 1, 'status': 'in-progress', 'attempts': 1}
                intent = {'id': 'fixture-intent', 'status': 'incomplete',
                          'operations': [operation]}
                fixture.store.save_intent(settings, intent)
                error = urllib.error.HTTPError(
                    fixture.sonarr.url + '/api/v3/series/1', status,
                    'fixture response', {}, None)
                with patch('urllib.request.urlopen', side_effect=error) as request:
                    if status == 404:
                        fixture.main._resume_intent(settings, intent)
                        self.assertEqual(operation['status'], 'done')
                    else:
                        with self.assertRaises(SonarrError):
                            fixture.main._resume_intent(settings, intent)
                        self.assertEqual(operation['status'], 'in-progress')
                    request.assert_called_once()
                    self.assertEqual(request.call_args.args[0].get_method(), 'GET')

    def test_not_found_in_connection_message_does_not_complete_series(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False)
            operation = {'kind': 'delete-series', 'instance_id': 'fake',
                         'series_id': 1, 'status': 'in-progress', 'attempts': 1}
            intent = {'id': 'fixture-intent', 'status': 'incomplete',
                      'operations': [operation]}
            fixture.store.save_intent(settings, intent)
            fixture.sonarr.expect('GET', 'series/1',
                urllib.error.URLError('host not found'))
            with self.assertRaises(SonarrError):
                fixture.main._resume_intent(settings, intent)
            fixture.sonarr.assert_finished()
            self.assertEqual(operation['status'], 'in-progress')
            self.assertEqual(fixture.store.load_intent(settings), intent)
            self.assertEqual(fixture.sonarr.mutations, [])

    def test_public_retry_stops_on_unavailable_or_malformed_recovery_read(self):
        for response in (urllib.error.URLError('host not found'), {'unexpected': 'payload'}):
            with self.subTest(response=str(response)), IsolatedWorker() as fixture:
                settings = fixture.settings(test_mode=False)
                fixture.sonarr.expect('GET', 'series', [SERIES])
                fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                fixture.sonarr.expect('PUT', 'episode/monitor',
                    urllib.error.URLError('fixture acknowledgement lost'),
                    body={'episodeIds': [101], 'monitored': False})
                fixture.main.run()
                before = fixture.store.load_intent(settings)
                fixture.sonarr.expect('GET', 'episode', response, query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                with self.assertRaises(SonarrError):
                    fixture.main.run()
                fixture.sonarr.assert_finished()
                self.assertEqual(len(fixture.sonarr.mutations), 1)
                self.assertEqual(fixture.store.load_intent(settings), before)
                self.assertEqual(len(fixture.store.load_settings()['rules']), 1)


if __name__ == '__main__':
    unittest.main()
