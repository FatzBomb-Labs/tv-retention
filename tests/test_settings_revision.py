"""Whole-document settings saves reject stale revisions."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

import context  # noqa: F401
import actions
from core import Rejected
from fake_sonarr import IsolatedWorker, SERIES


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
