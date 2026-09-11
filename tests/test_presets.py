import unittest

import context  # noqa: F401
from core import Rejected, effective_rule, validate_settings


def base(**overrides):
    document = {
        'instances': [{'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989', 'api_key': 'a' * 32}],
        'profiles': [{'id': 'p1', 'name': 'Keep 30 days', 'keep_days': 30, 'combine': 'any'}],
        'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
                   'path': '/mnt/user/media/TV/Show', 'profile_id': 'p1'}],
    }
    document.update(overrides)
    return document


class Presets(unittest.TestCase):
    def test_a_rule_may_carry_only_a_preset(self):
        settings = validate_settings(base())
        self.assertEqual(settings['rules'][0]['profile_id'], 'p1')
        self.assertIsNone(settings['rules'][0]['keep_days'])

    def test_a_preset_needs_a_condition(self):
        document = base()
        document['profiles'][0].pop('keep_days')
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_a_preset_needs_a_name(self):
        document = base()
        document['profiles'][0]['name'] = ''
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_preset_names_are_unique(self):
        document = base()
        document['profiles'].append({'id': 'p2', 'name': 'keep 30 DAYS', 'keep_days': 60})
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_an_unknown_preset_is_rejected(self):
        document = base()
        document['rules'][0]['profile_id'] = 'gone'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_a_rule_without_a_preset_still_needs_its_own_values(self):
        document = base()
        document['rules'][0]['profile_id'] = ''
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_preset_values_win_over_stale_rule_values(self):
        document = base()
        document['rules'][0]['keep_days'] = 9999
        settings = validate_settings(document)
        resolved = effective_rule(settings['rules'][0], settings['profiles'])
        self.assertEqual(resolved['keep_days'], 30)

    def test_widening_a_preset_widens_every_rule_using_it(self):
        document = base()
        document['rules'].append({'id': 'r2', 'instance_id': 'i1', 'series_id': 6, 'series_title': 'Other',
                                  'path': '/mnt/user/media/TV/Other', 'profile_id': 'p1'})
        document['profiles'][0]['keep_days'] = 90
        settings = validate_settings(document)
        for rule in settings['rules']:
            self.assertEqual(effective_rule(rule, settings['profiles'])['keep_days'], 90)

    def test_a_custom_rule_is_untouched_by_presets(self):
        document = base()
        document['rules'][0] = {'id': 'r1', 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
                                'path': '/mnt/user/media/TV/Show', 'keep_episodes': 12, 'combine': 'any'}
        settings = validate_settings(document)
        resolved = effective_rule(settings['rules'][0], settings['profiles'])
        self.assertEqual(resolved['keep_episodes'], 12)
        self.assertEqual(resolved['combine'], 'any')

    def test_a_missing_preset_at_run_time_is_refused_not_guessed(self):
        with self.assertRaises(Rejected):
            effective_rule({'profile_id': 'p9', 'path': '/mnt/user/media/TV/Show'}, [])


if __name__ == '__main__':
    unittest.main()
