"""Whole-document settings saves reject stale revisions."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

import context  # noqa: F401
import actions
from core import Rejected
from fake_sonarr import IsolatedWorker, SERIES, episode_payload


class SettingsRevision(unittest.TestCase):
    def test_stale_whole_document_save_cannot_overwrite_newer_settings(self):
        with IsolatedWorker() as fixture:
            current = fixture.settings(test_mode=True, removal=None)
            fixture.store.save_settings(current)
            stale = copy.deepcopy(current)
            newer = copy.deepcopy(current)
            newer['rules'][0]['keep_days'] = 90
            saved = actions.action_settings(current, {'settings': newer})
            self.assertEqual(saved['settings']['settings_revision'], current['settings_revision'] + 1)
            with self.assertRaisesRegex(Rejected, 'revision|reload'):
                actions.action_settings(fixture.store.load_settings(), {'settings': stale})
            actual = fixture.store.load_settings()
            self.assertEqual(actual['rules'][0]['keep_days'], 90)
            self.assertEqual(actual['settings_revision'], saved['settings']['settings_revision'])

    def test_old_documents_start_at_a_revision_and_echo_it(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            settings.pop('settings_revision', None)
            fixture.store.CONFIG.write_text(json.dumps(settings))
            loaded = fixture.store.load_settings()
            self.assertEqual(loaded['settings_revision'], 0)
            draft = copy.deepcopy(loaded)
            result = actions.action_settings(loaded, {'settings': draft})
            self.assertEqual(result['settings']['settings_revision'], 1)

    def test_concurrent_document_saves_allow_one_revision_and_reject_the_other(self):
        with IsolatedWorker() as fixture:
            current = fixture.settings(test_mode=True, removal=None)
            fixture.store.save_settings(current)
            first = copy.deepcopy(current)
            second = copy.deepcopy(current)
            first['rules'][0]['keep_days'] = 60
            second['rules'][0]['keep_days'] = 90
            results, errors = [], []

            def save(document):
                try:
                    results.append(actions.action_settings(current, {'settings': document}))
                except Rejected as error:
                    errors.append(error)

            threads = [threading.Thread(target=save, args=(document,))
                       for document in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn(fixture.store.load_settings()['rules'][0]['keep_days'], (60, 90))

    def test_background_binding_merges_rule_fields_into_newer_settings(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            settings['rules'][0]['queue'] = {}
            fixture.store.save_settings(settings)
            fixture.sonarr.expect('GET', 'series', [SERIES])
            original_match = fixture.main.match_rule

            def match_and_edit(rule, series):
                current = fixture.store.load_settings()
                current['rules'][0]['keep_days'] = 90
                fixture.store.save_settings(current)
                return original_match(rule, series)

            with patch.object(fixture.main, 'match_rule', side_effect=match_and_edit):
                fixture.main.bind_rules(settings, force=True)
            actual = fixture.store.load_settings()
            self.assertEqual(actual['rules'][0]['keep_days'], 90)
            self.assertEqual(actual['rules'][0]['match_status'], 'matched')
            fixture.sonarr.assert_finished()

    def test_sync_reenable_merges_owned_fields_into_newer_settings(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal=None)
            settings['rules'][0]['queue'] = {}
            fixture.store.save_settings(settings)
            fixture.sonarr.expect('GET', 'series', [dict(SERIES, ended=True)])
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            fixture.main.sync_from_sonarr(settings)
            current = fixture.store.load_settings()
            current['rules'][0].update(enabled=False, auto_reenable=True, auto_reenable_after='')
            fixture.store.save_settings(current)
            settings = fixture.store.load_settings()
            fixture.sonarr.expect('GET', 'series', [dict(SERIES, ended=False, status='continuing')])
            fixture.sonarr.expect('GET', 'episode', [episode_payload()], query={
                'seriesId': ['1'], 'includeEpisodeFile': ['true']})
            original = fixture.sonarr.urlopen

            def change_during_read(request, **kwargs):
                response = original(request, **kwargs)
                if request.get_method() == 'GET' and '/api/v3/series' in request.full_url:
                    newer = fixture.store.load_settings()
                    newer['rules'][0]['keep_days'] = 90
                    fixture.store.save_settings(newer)
                return response

            with patch('urllib.request.urlopen', side_effect=change_during_read):
                fixture.main.sync_from_sonarr(settings)
            actual = fixture.store.load_settings()
            self.assertEqual(actual['rules'][0]['keep_days'], 90)
            self.assertTrue(actual['rules'][0]['enabled'])
            self.assertFalse(actual['rules'][0]['auto_reenable'])
            fixture.sonarr.assert_finished()

    def test_backup_status_commit_serializes_with_a_settings_save(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            fixture.store.save_settings(settings)
            def create_backup(current):
                newer = fixture.store.load_settings()
                newer['rules'][0]['keep_days'] = 90
                actions.action_settings(newer, {'settings': newer})
                return {'created_at': 'backup-stamp'}

            with patch.object(actions.backup, 'create', side_effect=create_backup), \
                    patch.object(actions.backup, 'list_backups', return_value=[]):
                result = actions.action_backup(settings, {'operation': 'create'})

            self.assertEqual(result['result']['created_at'], 'backup-stamp')
            actual = fixture.store.load_settings()
            self.assertEqual(actual['rules'][0]['keep_days'], 90)
            self.assertEqual(actual['backup']['last'], 'backup-stamp')

    def test_stale_health_writer_preserves_operator_alert_decisions(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            alert = fixture.main.alerts.make('ended-expired', rule_id='r1')
            recycle = fixture.main.alerts.make('no-recycle-bin', instance_id='fake')
            fixture.main.write_cache(settings, 'health.json', {
                'alerts': [alert, recycle], 'rules': {}, 'instances': {}})
            stale = fixture.store.load_health(settings)

            self.assertTrue(actions.action_acknowledge(settings, {'key': alert['key']})['alerts'])
            actions.action_suppress_alert(settings, {'key': recycle['key']})
            stale['instances']['fake'] = {'ok': True, 'reachable': True}
            fixture.main.write_cache(settings, 'health.json', stale)

            actual = fixture.store.load_health(settings)
            self.assertIn(alert['key'], actual['acknowledged'])
            self.assertIn(recycle['key'], actual['suppressed'])
            self.assertEqual(actual['instances']['fake']['ok'], True)

    def test_invalid_saved_settings_are_visible_but_mutations_fail_closed(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal=None)
            invalid = copy.deepcopy(settings)
            invalid['rules'][0]['keep_days'] = 'not-a-duration'
            fixture.store.CONFIG.write_text(json.dumps(invalid))

            status = actions.dispatch({'action': 'status'})
            self.assertTrue(status['ok'], status)
            self.assertTrue(any(alert['kind'] == 'settings-invalid'
                                for alert in status['alerts']))

            result = actions.dispatch({'action': 'run'})
            self.assertFalse(result['ok'])
            self.assertIn('Saved settings are invalid', result['error'])
            self.assertEqual(fixture.sonarr.mutations, [])

    def test_settings_save_can_repair_an_invalid_document(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            invalid = copy.deepcopy(settings)
            invalid['rules'][0]['keep_days'] = 'not-a-duration'
            fixture.store.CONFIG.write_text(json.dumps(invalid))

            result = actions.dispatch({'action': 'settings', 'settings': settings})
            self.assertTrue(result['ok'], result)
            self.assertEqual(fixture.store.load_settings_strict()['rules'][0]['keep_days'], 30)

    def test_invalid_saved_intent_is_visible_and_blocks_live_runs(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal=None)
            fixture.store.save_settings(settings)
            intent_path = fixture.store.state_dir(settings) / 'run-intent.json'
            intent_path.write_text('{not-json')

            status = actions.dispatch({'action': 'status'})
            self.assertTrue(status['ok'], status)
            self.assertTrue(any(alert['kind'] == 'intent-invalid'
                                for alert in status['alerts']))

            result = actions.dispatch({'action': 'run'})
            self.assertFalse(result['ok'])
            self.assertIn('Saved run intent is unreadable', result['error'])
            self.assertEqual(fixture.sonarr.mutations, [])

    def test_invalid_saved_operation_is_visible_and_blocks_live_runs(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal=None)
            fixture.store.save_settings(settings)
            intent_path = fixture.store.state_dir(settings) / 'run-intent.json'
            intent_path.write_text(json.dumps({
                'id': 'bad-operation', 'status': 'incomplete',
                'operations': [{'kind': 'delete-episode-file', 'status': 'pending'}],
            }))

            status = actions.dispatch({'action': 'status'})
            self.assertTrue(status['ok'], status)
            self.assertTrue(any(alert['kind'] == 'intent-invalid'
                                for alert in status['alerts']))

            result = actions.dispatch({'action': 'run'})
            self.assertFalse(result['ok'])
            self.assertIn('Saved run intent is invalid', result['error'])
            self.assertEqual(fixture.sonarr.mutations, [])

    def test_invalid_saved_state_is_visible_and_blocks_history_mutation(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            fixture.store.save_settings(settings)
            state_path = fixture.store.state_dir(settings) / 'state.json'
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text('{not-json')

            status = actions.dispatch({'action': 'status'})
            self.assertTrue(status['ok'], status)
            self.assertTrue(any(alert['kind'] == 'state-invalid'
                                for alert in status['alerts']))

            result = actions.dispatch({'action': 'clear-history'})
            self.assertFalse(result['ok'])
            self.assertIn('Saved run history is unreadable', result['error'])
            self.assertEqual(state_path.read_text(), '{not-json')

    def test_invalid_saved_state_blocks_live_runs_before_sonarr_mutation(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=False, removal=None)
            fixture.store.save_settings(settings)
            state_path = fixture.store.state_dir(settings) / 'state.json'
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text('{not-json')

            result = actions.dispatch({'action': 'run'})
            self.assertFalse(result['ok'])
            self.assertIn('Saved run history is unreadable', result['error'])
            self.assertEqual(fixture.sonarr.mutations, [])

    def test_invalid_scheduler_state_is_visible_in_status(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            fixture.store.save_settings(settings)
            jobs_path = fixture.store.state_dir(settings) / 'jobs.json'
            jobs_path.parent.mkdir(parents=True, exist_ok=True)
            jobs_path.write_text('{not-json')

            status = actions.dispatch({'action': 'status'})
            self.assertTrue(status['ok'], status)
            self.assertTrue(any(alert['kind'] == 'jobs-invalid'
                                for alert in status['alerts']))

    def test_invalid_health_cache_is_visible_in_status(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings(test_mode=True, removal=None)
            fixture.store.save_settings(settings)
            health_path = fixture.store.state_dir(settings) / 'health.json'
            health_path.parent.mkdir(parents=True, exist_ok=True)
            health_path.write_text('{not-json')

            status = actions.dispatch({'action': 'status'})
            self.assertTrue(status['ok'], status)
            self.assertTrue(any(alert['kind'] == 'health-invalid'
                                for alert in status['alerts']))
