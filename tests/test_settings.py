import unittest

import context  # noqa: F401
from core import DEFAULTS, Rejected, redact, validate_cron, validate_settings


def base(**overrides):
    document = {
        'instances': [{'id': 'i1', 'name': 'Series', 'url': 'http://sonarr:8989',
                       'api_key': 'a' * 32, 'path_maps': [{'from': '/tv', 'to': '/mnt/user/media/TV'}]}],
        'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 5, 'series_title': 'Show',
                   'tvdb_id': 99, 'path': '/mnt/user/media/TV/Show', 'keep_days': 30}],
    }
    document.update(overrides)
    return document


class Cron(unittest.TestCase):
    def test_accepts_five_fields(self):
        self.assertEqual(validate_cron(' 0  4 * * * '), '0 4 * * *')

    def test_rejects_wrong_field_count(self):
        with self.assertRaises(Rejected):
            validate_cron('0 4 * *')

    def test_rejects_shell_injection(self):
        with self.assertRaises(Rejected):
            validate_cron('0 4 * * * ; rm -rf /')


class Settings(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(validate_settings(DEFAULTS)['preview'], True)

    def test_preview_defaults_on(self):
        # A fresh install must never be able to delete before anyone has looked at it.
        self.assertTrue(validate_settings({})['preview'])

    def test_rule_requires_a_condition(self):
        document = base()
        document['rules'][0].pop('keep_days')
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_rule_requires_a_known_instance(self):
        document = base()
        document['rules'][0]['instance_id'] = 'missing'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_rule_without_series_is_unmatched(self):
        document = base()
        document['rules'][0].pop('series_id')
        self.assertEqual(validate_settings(document)['rules'][0]['match_status'], 'unmatched')

    def test_duplicate_folders_are_rejected(self):
        document = base()
        second = dict(document['rules'][0], id='r2')
        document['rules'].append(second)
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_library_paths_must_be_deep_enough(self):
        document = base()
        document['rules'][0]['path'] = '/mnt/user'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_traversal_is_rejected(self):
        document = base()
        document['rules'][0]['path'] = '/mnt/user/media/../../etc/passwd'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_masked_key_keeps_the_stored_secret(self):
        stored = validate_settings(base())
        document = base()
        document['instances'][0]['api_key'] = '********'
        self.assertEqual(validate_settings(document, previous=stored)['instances'][0]['api_key'], 'a' * 32)

    def test_masked_key_without_a_stored_secret_is_rejected(self):
        document = base()
        document['instances'][0]['api_key'] = '********'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_redaction_hides_keys(self):
        self.assertEqual(redact(validate_settings(base()))['instances'][0]['api_key'], '********')

    def test_video_extension_cannot_be_a_sidecar(self):
        with self.assertRaises(Rejected):
            validate_settings(base(sidecars={'enabled': True, 'extensions': ['mkv']}))

    def test_plugin_recycle_requires_a_folder(self):
        with self.assertRaises(Rejected):
            validate_settings(base(recycle={'mode': 'plugin', 'path': ''}))

    def test_guard_bounds_are_enforced(self):
        with self.assertRaises(Rejected):
            validate_settings(base(guards={'max_percent_per_rule': {'enabled': True, 'value': 500}}))

    def test_a_guard_can_be_switched_off_without_losing_its_number(self):
        settings = validate_settings(base(guards={'max_deletes_per_run': {'enabled': False, 'value': 200}}))
        self.assertFalse(settings['guards']['max_deletes_per_run']['enabled'])
        self.assertEqual(settings['guards']['max_deletes_per_run']['value'], 200)

    def test_combine_mode_is_checked(self):
        document = base()
        document['rules'][0]['combine'] = 'whenever'
        with self.assertRaises(Rejected):
            validate_settings(document)

    def test_url_must_be_http(self):
        document = base()
        document['instances'][0]['url'] = 'ftp://sonarr'
        with self.assertRaises(Rejected):
            validate_settings(document)


if __name__ == '__main__':
    unittest.main()
