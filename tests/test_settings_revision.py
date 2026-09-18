"""Whole-document settings saves reject stale revisions."""
import copy
import json
import unittest

import context  # noqa: F401
import actions
from core import Rejected
from fake_sonarr import IsolatedWorker


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
