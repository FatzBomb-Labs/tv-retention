"""Upgrading a settings document written by an earlier release.

Settings outlive any release, so a change of shape has to bring the existing document
with it. One of these runs against a redacted copy of a real in-use configuration — 36
rules and 4 presets — because a migration that only ever sees hand-written fixtures is a
migration nobody has tested.

There is a floor at `MINIMUM_VERSION`, so the other half of the contract is what happens
below it: a refusal that names the version, not a silent reshape. When the floor rises,
re-base the fixture with the step that is about to be deleted, then delete it.
"""
import json
import unittest
from pathlib import Path

import context  # noqa: F401
from core import validate_settings
from migrate import MINIMUM_VERSION, SETTINGS_VERSION, UnsupportedVersion, migrate

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / f'settings-v{MINIMUM_VERSION}-live.json'


class TheFloor(unittest.TestCase):
    """Below the floor is a named refusal, because the steps no longer exist."""

    def test_a_document_older_than_the_floor_is_refused_by_version(self):
        for old in (1, 6, 7, MINIMUM_VERSION - 1):
            with self.assertRaises(UnsupportedVersion) as caught:
                migrate({'settings_version': old, 'rules': []})
            self.assertIn(str(old), str(caught.exception))
            self.assertIn(str(MINIMUM_VERSION), str(caught.exception))

    def test_the_floor_itself_is_accepted(self):
        document = migrate({'settings_version': MINIMUM_VERSION, 'rules': []})
        self.assertEqual(document['settings_version'], SETTINGS_VERSION)

    def test_an_empty_document_is_treated_as_fresh_rather_than_ancient(self):
        """A settings file holding `{}` is a new install, not a v1 document.

        The old code read a missing version as 1 and replayed twelve steps over nothing.
        With a floor that would be a refusal on first run, which is the wrong answer to
        an empty file.
        """
        self.assertEqual(migrate({})['settings_version'], SETTINGS_VERSION)
        self.assertEqual(migrate(None)['settings_version'], SETTINGS_VERSION)
        self.assertEqual(migrate([])['settings_version'], SETTINGS_VERSION)

    def test_the_refusal_reaches_the_loader_as_a_settings_error(self):
        """store turns it into the same Rejected every other broken-settings path uses,
        so the interface still loads and Status can name the problem."""
        import tempfile
        import store
        from core import Rejected
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            path.write_text(json.dumps({'settings_version': 7, 'rules': []}))
            original = store.CONFIG
            store.CONFIG = path
            try:
                with self.assertRaises(Rejected) as caught:
                    store.load_settings_strict()
                self.assertIn('version 7', str(caught.exception))
                self.assertIn(str(MINIMUM_VERSION), str(caught.exception))
            finally:
                store.CONFIG = original


class ToTheCurrentVersion(unittest.TestCase):
    """The one remaining step, and the scrub that runs at the current version."""

    def test_a_schedule_without_a_zone_gains_utc(self):
        document = migrate({'settings_version': MINIMUM_VERSION,
                            'schedule': {'enabled': True, 'hour': 4}})
        self.assertEqual(document['schedule']['timezone'], 'Etc/UTC')
        self.assertEqual(document['schedule']['hour'], 4, 'the rest of the schedule survives')

    def test_an_existing_zone_is_never_overwritten(self):
        document = migrate({'settings_version': MINIMUM_VERSION,
                            'schedule': {'timezone': 'America/New_York'}})
        self.assertEqual(document['schedule']['timezone'], 'America/New_York')

    def test_a_missing_schedule_still_gets_one(self):
        self.assertEqual(migrate({'settings_version': MINIMUM_VERSION})['schedule']['timezone'],
                         'Etc/UTC')

    def test_a_current_document_is_still_scrubbed_of_notifications(self):
        """A hand-edited current document can carry the retired outbound matrix.

        Silently retaining it would leave a credential-shaped value in a feature this
        release cannot use.
        """
        document = migrate({'settings_version': SETTINGS_VERSION,
                            'notifications': {'errors': True}, 'schedule': {}})
        self.assertNotIn('notifications', document)
        self.assertEqual(document['schedule']['timezone'], 'Etc/UTC')

    def test_migration_is_idempotent(self):
        once = migrate({'settings_version': MINIMUM_VERSION, 'schedule': {'hour': 3}})
        self.assertEqual(migrate(dict(once)), once)

    def test_a_forward_version_document_is_left_alone(self):
        # A newer release may have written keys this one does not know; keep them.
        ahead = {'settings_version': SETTINGS_VERSION + 5, 'something_new': 42}
        self.assertEqual(migrate(ahead), ahead)

    def test_an_unknown_key_is_never_dropped(self):
        document = migrate({'settings_version': MINIMUM_VERSION, 'mystery': {'a': 1}})
        self.assertEqual(document['mystery'], {'a': 1})


class RealConfiguration(unittest.TestCase):
    """A redacted copy of a configuration that was actually in use."""

    def setUp(self):
        self.old = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.new = migrate(self.old)

    def test_the_fixture_is_at_the_floor(self):
        # If this fails the fixture was not re-based when the floor moved.
        self.assertEqual(self.old['settings_version'], MINIMUM_VERSION)

    def test_every_rule_survives(self):
        self.assertEqual(len(self.new['rules']), len(self.old['rules']))
        self.assertEqual(len(self.new['rules']), 36)

    def test_every_preset_survives(self):
        self.assertEqual(len(self.new['profiles']), len(self.old['profiles']))
        self.assertEqual(len(self.new['profiles']), 4)

    def test_rules_keep_their_series_binding(self):
        for before, after in zip(self.old['rules'], self.new['rules']):
            self.assertEqual(before['series_id'], after['series_id'])
            self.assertEqual(before['path'], after['path'])

    def test_the_migrated_document_validates(self):
        settings = validate_settings(self.new)
        self.assertEqual(len(settings['rules']), 36)
        self.assertEqual(len(settings['profiles']), 4)

    def test_validation_preserves_every_rule_binding(self):
        # The real risk of a migration is silent loss, so this checks identity, not counts.
        settings = validate_settings(self.new)
        self.assertEqual(sorted(r['id'] for r in settings['rules']),
                         sorted(r['id'] for r in self.old['rules']))


class TheVersionNumber(unittest.TestCase):
    def test_validation_stamps_the_version_the_migration_reaches(self):
        """They were two constants, and only one got bumped.

        `migrate` raised the document and `validate_settings` stamped it back, so every
        load migrated it again and the file on disk never moved.
        """
        import core
        self.assertEqual(core.SETTINGS_VERSION, SETTINGS_VERSION)

    def test_a_migrated_document_survives_validation_at_its_new_version(self):
        document = migrate({'settings_version': MINIMUM_VERSION, 'instances': [], 'rules': []})
        self.assertEqual(validate_settings(document)['settings_version'], SETTINGS_VERSION)

    def test_the_floor_is_below_the_current_version(self):
        self.assertLessEqual(MINIMUM_VERSION, SETTINGS_VERSION)
