"""Submission-time target authority through the real action/run boundary."""
import copy
from io import BytesIO
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import context  # noqa: F401
import actions
from fake_sonarr import IsolatedWorker, SERIES, episode_payload


class RemovalTarget(unittest.TestCase):
    def queue(self, f, action='unmonitor-all'):
        settings = f.settings(test_mode=False, removal=None)
        settings['rules'][0].update(tvdb_id=10, match_status='matched')
        f.store.save_settings(settings)
        draft = copy.deepcopy(settings)
        draft['rules'][0]['queue']['removal'] = {'action': action, 'target': {
            'instance_id': 'evil', 'url': 'http://evil.invalid', 'series_id': 999,
            'tvdb_id': 999, 'path': '/wrong'}}
        actions.action_settings(settings, {'settings': draft})
        return f.store.load_settings()

    def test_submission_freezes_server_metadata_and_echo_cannot_edit_it(self):
        with IsolatedWorker() as f:
            settings = self.queue(f)
            expected = {'instance_id': 'fake', 'url': f.sonarr.url, 'series_id': 1,
                        'tvdb_id': 10, 'path': '/tv/Fixture'}
            self.assertEqual(settings['rules'][0]['queue']['removal'].get('target'), expected)
            draft = copy.deepcopy(settings)
            draft['rules'][0]['queue']['removal']['target']['tvdb_id'] = 999
            draft['rules'][0]['tvdb_id'] = 999
            actions.action_settings(settings, {'settings': draft})
            self.assertEqual(f.store.load_settings()['rules'][0]['queue']['removal']['target'], expected)
            self.assertEqual(f.sonarr.requests, [])

    def test_changed_target_refused_before_first_stage_retry_and_dispatch(self):
        for phase in ('stage', 'retry', 'restage', 'dispatch'):
            for change in ('url', 'tvdb', 'path', 'numeric-id'):
                with self.subTest(phase=phase, change=change), IsolatedWorker() as f:
                    settings = self.queue(f)
                    replacement = dict(SERIES)
                    changed, writes, reads = [], [], []

                    def change_target():
                        changed.append(True)
                        if change == 'url':
                            current = f.store.load_settings()
                            current['instances'][0]['url'] = 'http://replacement.invalid'
                            f.store.save_settings(current)
                        else:
                            replacement.update({'tvdbId': 99} if change == 'tvdb' else
                                               {'path': '/tv/Replacement'} if change == 'path' else {'id': 2})

                    def respond(request, **kwargs):
                        parsed = urllib.parse.urlsplit(request.full_url)
                        method = request.get_method()
                        if method != 'GET':
                            writes.append((method, request.full_url))
                            if phase == 'retry' and not changed:
                                raise urllib.error.URLError('lost acknowledgement')
                            return BytesIO(b'')
                        reads.append(parsed.path)
                        if parsed.path == '/api/v3/series':
                            return BytesIO(json.dumps([replacement]).encode())
                        if parsed.path == '/api/v3/series/1':
                            self.assertEqual(parsed.netloc, 'sonarr.invalid')
                            return BytesIO(json.dumps(replacement).encode())
                        self.assertEqual(parsed.path, '/api/v3/episode')
                        if phase == 'restage' and not changed:
                            raise urllib.error.URLError('staging unavailable')
                        return BytesIO(json.dumps([episode_payload()]).encode())

                    save = f.main.save_intent
                    def checkpoint(current, intent):
                        save(current, intent)
                        if phase == 'dispatch' and not changed and intent.get('operations'):
                            change_target()

                    with patch('urllib.request.urlopen', side_effect=respond), patch.object(
                            f.main, 'save_intent', side_effect=checkpoint):
                        if phase == 'stage':
                            change_target()
                        if phase in ('retry', 'restage'):
                            f.main.run()
                            self.assertEqual(len(writes), 1 if phase == 'retry' else 0)
                            change_target()
                        before = len(writes)
                        result = f.main.run()
                    self.assertEqual(len(writes), before, result)
                    self.assertEqual(result['status'], 'incomplete')
                    self.assertTrue(any('target' in error.lower() for error in result['errors']), result)
                    self.assertTrue(f.store.load_settings()['rules'][0]['queue']['removal'])
                    persisted = (f.store.load_removal_ledger(settings) if phase in ('retry', 'restage')
                                 else f.store.load_intent(settings))
                    self.assertIn('http://sonarr.invalid', json.dumps(persisted))

    def test_uncertain_delete_404_never_claims_completion(self):
        with IsolatedWorker() as f:
            settings = self.queue(f, 'delete-series')
            writes = []
            absent = []
            def respond(request, **kwargs):
                path = urllib.parse.urlsplit(request.full_url).path
                if request.get_method() == 'DELETE':
                    writes.append(path)
                    raise urllib.error.URLError('lost delete reply')
                if path == '/api/v3/series':
                    return BytesIO(json.dumps([SERIES]).encode())
                self.assertEqual(path, '/api/v3/series/1')
                if absent:
                    raise urllib.error.HTTPError(request.full_url, 404, 'proxy not found', {}, None)
                return BytesIO(json.dumps(SERIES).encode())
            with patch('urllib.request.urlopen', side_effect=respond):
                f.main.run()
                absent.append(True)
                result = f.main.run()
            self.assertEqual(len(writes), 1)
            self.assertEqual(result['status'], 'incomplete')
            self.assertIn('cannot verify absence', '; '.join(result['errors']))
            self.assertEqual(f.store.load_removal_ledger(settings)['batches'][0]['operations'][0]['status'], 'failed')
            self.assertEqual(len(f.store.load_settings()['rules']), 1)

    def test_legacy_queue_holds_external_work_but_local_removal_stays_local(self):
        for action in ('unmonitor-all', 'remove'):
            with self.subTest(action=action), IsolatedWorker() as f:
                settings = f.settings(test_mode=False, removal=action)
                settings['rules'][0]['queue']['removal'].pop('target', None)
                f.store.save_settings(settings)
                f.sonarr.expect('GET', 'series', [SERIES])
                result = f.main.run()
                f.sonarr.assert_finished()
                self.assertEqual(f.sonarr.mutations, [])
                self.assertEqual(result['status'], 'complete' if action == 'remove' else 'incomplete')
                self.assertEqual(len(f.store.load_settings()['rules']), 0 if action == 'remove' else 1)

    def test_legacy_operation_with_request_id_but_no_target_is_held(self):
        with IsolatedWorker() as f:
            settings = self.queue(f)
            operation = f.main._operation('set-monitored', settings['rules'][0],
                removal_action='unmonitor-all', episode_ids=[101], monitored=False)
            operation.pop('target')
            operation.update(status='failed', attempts=1)
            intent = {'id': 'legacy-target', 'status': 'incomplete', 'operations': [operation]}
            f.store.save_intent(settings, intent)
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'series', [SERIES])
            result = f.main.run()
            self.assertEqual(result['status'], 'incomplete')
            self.assertIn('snapshot', '; '.join(result['errors']))
            self.assertEqual(f.store.load_removal_ledger(settings)['batches'][0]['operations'], [operation])
            self.assertEqual(f.sonarr.mutations, [])
            f.sonarr.assert_finished()

    def test_successful_delete_finalizes_without_reading_deleted_series(self):
        with IsolatedWorker() as f:
            settings = self.queue(f, 'delete-series-files')
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'series/1', SERIES)
            f.sonarr.expect('GET', 'series/1', SERIES)
            f.sonarr.expect('DELETE', 'series/1', query={
                'deleteFiles': ['true'], 'addImportListExclusion': ['false']})
            result = f.main.run()
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(f.store.load_settings()['rules'], [])
            self.assertEqual(len(f.sonarr.mutations), 1)
            f.sonarr.assert_finished()

    def test_dispatch_uses_current_credentials_not_run_client(self):
        with IsolatedWorker() as f:
            settings = self.queue(f)
            save = f.main.save_intent
            changed = []
            def checkpoint(current, intent):
                save(current, intent)
                if not changed and intent.get('operations'):
                    changed.append(True)
                    saved = f.store.load_settings()
                    saved['instances'][0]['api_key'] = 'newFixtureCredential0000000000'
                    f.store.save_settings(saved)
                    f.sonarr.api_key = saved['instances'][0]['api_key']
            f.sonarr.expect('GET', 'series', [SERIES])
            f.sonarr.expect('GET', 'series/1', SERIES)
            f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            f.sonarr.expect('GET', 'series/1', SERIES)
            f.sonarr.expect('PUT', 'episode/monitor', body={'episodeIds': [101], 'monitored': False})
            with patch.object(f.main, 'save_intent', side_effect=checkpoint):
                result = f.main.run()
            self.assertEqual(result['status'], 'complete')
            self.assertNotEqual(settings['instances'][0]['api_key'], f.sonarr.api_key)
            f.sonarr.assert_finished()

    def test_requeue_record_only_uses_new_request_but_not_new_target(self):
        for change_target in (False, True):
            with self.subTest(change_target=change_target), IsolatedWorker() as f:
                settings = self.queue(f)
                original = copy.deepcopy(settings['rules'][0]['queue']['removal'])
                f.sonarr.expect('GET', 'series', [SERIES])
                f.sonarr.expect('GET', 'series/1', SERIES)
                f.sonarr.expect('GET', 'episode', urllib.error.URLError('staging failed'), query={
                    'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                f.main.run()
                current = f.store.load_settings()
                if change_target:
                    current['rules'][0]['tvdb_id'] = 99
                    f.store.save_settings(current)
                draft = copy.deepcopy(current)
                draft['rules'][0]['queue']['removal'] = {'action': 'monitor-all'}
                actions.action_settings(current, {'settings': draft})
                new_request = f.store.load_settings()['rules'][0]['queue']['removal']['request_id']
                self.assertNotEqual(original['request_id'], new_request)
                identity = dict(SERIES, tvdbId=99) if change_target else SERIES
                f.sonarr.expect('GET', 'series', [identity])
                if change_target:
                    f.sonarr.expect('GET', 'series', [identity])
                else:
                    f.sonarr.expect('GET', 'series/1', SERIES)
                    f.sonarr.expect('GET', 'episode', [episode_payload()], query={
                        'seriesId': ['1'], 'includeEpisodeFile': ['true']})
                    f.sonarr.expect('GET', 'series/1', SERIES)
                    f.sonarr.expect('PUT', 'episode/monitor', body={'episodeIds': [101], 'monitored': True})
                result = f.main.run()
                self.assertEqual(result['status'], 'incomplete' if change_target else 'complete')
                batch = f.store.load_removal_ledger(settings)['batches'][0]
                if change_target:
                    self.assertEqual(batch['operations'], [])
                    self.assertEqual(batch['removals'][0]['target'], original['target'])
                    self.assertEqual(f.sonarr.mutations, [])
                else:
                    self.assertEqual(batch['operations'][0]['request_id'], new_request)
                    self.assertEqual(f.store.load_settings()['rules'], [])
                f.sonarr.assert_finished()


if __name__ == '__main__':
    unittest.main()
