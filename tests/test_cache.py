import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, Rejected, evaluate, rule_fingerprint, validate_settings

NOW = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)


def base(**overrides):
    document = {
        'instances': [{'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32}],
        'profiles': [{'id': 'p1', 'name': 'Keep 30', 'keep_days': 30}],
        'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
                   'path': '/mnt/user/media/TV/Show', 'profile_id': 'p1'}],
    }
    document.update(overrides)
    return validate_settings(document)


def fingerprint(settings, index=0):
    return rule_fingerprint(settings['rules'][index], settings)


class Fingerprint(unittest.TestCase):
    def test_identical_settings_agree(self):
        self.assertEqual(fingerprint(base()), fingerprint(base()))

    def test_widening_the_preset_invalidates_the_rule(self):
        wider = base()
        wider['profiles'][0]['keep_days'] = 90
        self.assertNotEqual(fingerprint(base()), fingerprint(wider))

    def test_changing_the_combine_mode_invalidates(self):
        other = base()
        other['profiles'][0]['combine'] = 'any'
        self.assertNotEqual(fingerprint(base()), fingerprint(other))

    def test_the_global_specials_setting_invalidates(self):
        self.assertNotEqual(fingerprint(base()),
                            fingerprint(base(retention={'include_specials': True})))

    def test_a_per_rule_specials_override_invalidates(self):
        document = base()
        override = base()
        override['rules'][0]['include_specials'] = True
        self.assertNotEqual(fingerprint(document), fingerprint(override))

    def test_an_unrelated_setting_does_not_invalidate(self):
        # Notifications cannot move the keep frame, so cached results stay valid.
        self.assertEqual(fingerprint(base()), fingerprint(base(notify=False)))

    def test_a_missing_preset_is_refused_rather_than_hashed(self):
        settings = base()
        settings['profiles'] = []
        with self.assertRaises(Rejected):
            rule_fingerprint(settings['rules'][0], settings)


class Specials(unittest.TestCase):
    """The per-show override, which is the point of putting it on the rule."""

    def setUp(self):
        self.episodes = [{
            'episode_id': 1, 'season': 0, 'episode': 1, 'title': 'Special',
            'path': '/mnt/user/media/TV/Show/Specials/S00E01.mkv',
            'air_date': '2015-01-01', 'air_source': 'sonarr',
            'mtime': dt.datetime(2015, 1, 1, tzinfo=dt.timezone.utc).timestamp(), 'size': 10,
        }]
        self.settings = {'retention': dict(DEFAULTS['retention']),
                         'guards': {'max_percent_per_rule': 100, 'min_file_age_hours': 0}}

    def evaluate(self, rule, include_globally=False):
        settings = dict(self.settings, retention=dict(self.settings['retention'],
                                                      include_specials=include_globally))
        return evaluate(self.episodes, dict(rule, keep_days=30, combine='earliest'), settings, now=NOW)

    def test_inherits_the_global_exclusion(self):
        self.assertEqual(self.evaluate({'include_specials': None})['delete'], [])

    def test_inherits_the_global_inclusion(self):
        self.assertEqual(len(self.evaluate({'include_specials': None}, include_globally=True)['delete']), 1)

    def test_a_show_can_include_specials_the_global_setting_excludes(self):
        self.assertEqual(len(self.evaluate({'include_specials': True})['delete']), 1)

    def test_a_show_can_protect_specials_the_global_setting_includes(self):
        result = self.evaluate({'include_specials': False}, include_globally=True)
        self.assertEqual(result['delete'], [])
        self.assertIn('Specials', result['protected'][0]['reason'])


class SpecialsSettings(unittest.TestCase):
    def test_the_override_survives_validation(self):
        document = {
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/mnt/user/media/TV/A',
                       'keep_days': 30, 'include_specials': 'yes'}],
        }
        self.assertIs(validate_settings(document)['rules'][0]['include_specials'], True)

    def test_inherit_is_stored_as_no_opinion(self):
        document = {
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/mnt/user/media/TV/A',
                       'keep_days': 30, 'include_specials': ''}],
        }
        self.assertIsNone(validate_settings(document)['rules'][0]['include_specials'])

    def test_nonsense_is_rejected(self):
        document = {
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/mnt/user/media/TV/A',
                       'keep_days': 30, 'include_specials': 'sometimes'}],
        }
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_the_series_match_schedule_is_validated(self):
        with self.assertRaises(Rejected):
            validate_settings({'health': {'series_match': {'enabled': True, 'frequency': 'never'}}})

    def test_the_series_match_check_cannot_be_switched_off(self):
        # A rule that no longer resolves to a series must not act, so the check that
        # notices is not optional. Only its cadence is.
        self.assertTrue(validate_settings({})['health']['series_match']['enabled'])
        off = validate_settings({'health': {'series_match': {'enabled': False, 'frequency': 'weekly'}}})
        self.assertTrue(off['health']['series_match']['enabled'])
        self.assertEqual(off['health']['series_match']['frequency'], 'weekly')


if __name__ == '__main__':
    unittest.main()
