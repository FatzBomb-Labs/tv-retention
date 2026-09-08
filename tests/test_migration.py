"""Upgrading a settings document written by an earlier release.

Settings live on the flash device and outlive any release, so a change of shape has to
bring the existing document with it. One of these runs against a redacted copy of a real
in-use configuration — 36 rules and 4 presets — because a migration that only ever sees
hand-written fixtures is a migration nobody has tested.
"""
import json
import unittest
from pathlib import Path

import context  # noqa: F401
from core import validate_settings
from migrate import SETTINGS_VERSION, migrate, schedule_from_cron

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'settings-v1-live.json'


class CronRecovery(unittest.TestCase):
    """The old release stored a cron line; the schedule it meant has to survive."""

    def test_a_daily_line(self):
        schedule = schedule_from_cron('0 4 * * *', True)
        self.assertEqual((schedule['frequency'], schedule['hour'], schedule['minute']), ('daily', 4, 0))

    def test_a_weekly_line(self):
        schedule = schedule_from_cron('30 5 * * 0', True)
        self.assertEqual((schedule['frequency'], schedule['weekday']), ('weekly', 0))

    def test_a_monthly_line(self):
        schedule = schedule_from_cron('0 4 1 * *', True)
        self.assertEqual((schedule['frequency'], schedule['monthly_day']), ('monthly', 1))

    def test_a_day_beyond_28_is_capped_so_no_month_is_skipped(self):
        self.assertEqual(schedule_from_cron('0 4 31 * *', True)['monthly_day'], 28)

    def test_an_expression_with_no_structured_form_stays_custom(self):
        schedule = schedule_from_cron('*/10 * * * *', True)
        self.assertEqual(schedule['frequency'], 'custom')
        self.assertEqual(schedule['cron'], '*/10 * * * *')

    def test_the_enabled_state_carries_over(self):
        self.assertFalse(schedule_from_cron('0 4 * * *', False)['enabled'])


class Upgrade(unittest.TestCase):
    def setUp(self):
        self.old = {
            'settings_version': 1,
            'dry_run': False,
            'schedule': {'enabled': True, 'cron': '0 4 * * *'},
            'health': {'enabled': True, 'cron': '0 5 * * *', 'ttl_hours': 12, 'notify_ok': True},
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32,
                           'path_maps': [{'from': '/tv', 'to': '/mnt/user/media/TV'}]}],
            'guards': {'max_deletes_per_run': 300, 'max_percent_per_rule': 40, 'min_file_age_hours': 2},
            'retention': {'include_specials': True, 'unmonitor_deleted': False,
                          'remonitor_widened': True, 'monitor_specials': True},
            'tmdb': {'enabled': False, 'api_key': 'b' * 32},
            'notify': False,
            'allow_series_deletion': True,
            'rules': [], 'profiles': [],
        }
        self.new = migrate(self.old)

    def test_the_version_advances(self):
        self.assertEqual(self.new['settings_version'], SETTINGS_VERSION)

    def test_dry_run_becomes_schedule_test_mode(self):
        # v1 dry_run -> v2 preview -> v3 schedule.test_mode, in one hop.
        self.assertFalse(self.new['schedule']['test_mode'])
        self.assertNotIn('dry_run', self.new)
        self.assertNotIn('preview', self.new)

    def test_the_schedule_becomes_structured(self):
        self.assertEqual(self.new['schedule']['frequency'], 'daily')
        self.assertTrue(self.new['schedule']['enabled'])

    def test_path_mapping_is_gone_entirely(self):
        # Sonarr owns the filesystem, so an instance is only a connection.
        instance = self.new['instances'][0]
        self.assertNotIn('path_maps', instance)
        self.assertNotIn('roots', instance)


    def test_retention_keeps_what_still_means_something(self):
        # Unmonitoring what we delete is an invariant now, so its flag is gone; monitoring
        # missing episodes became a per-series decision.
        self.assertTrue(self.new['retention']['include_specials'])
        self.assertNotIn('auto_unmonitor', self.new['retention'])
        self.assertNotIn('auto_monitor', self.new['retention'])

    def test_the_separate_specials_monitoring_flag_is_gone(self):
        self.assertNotIn('monitor_specials', self.new['retention'])

    def test_a_tmdb_key_survives_without_its_enabled_flag(self):
        self.assertEqual(self.new['tmdb'], {'api_key': 'b' * 32})

    def test_the_notify_flag_becomes_a_matrix(self):
        self.assertFalse(self.new['notifications']['run_completed'])
        self.assertTrue(self.new['notifications']['health_ok'])

    def test_the_deletion_gate_is_dropped_for_a_typed_confirmation(self):
        self.assertNotIn('allow_series_deletion', self.new)

    def test_running_it_twice_changes_nothing(self):
        self.assertEqual(migrate(self.new), self.new)

    def test_the_result_passes_validation(self):
        validate_settings(self.new)


class RealConfiguration(unittest.TestCase):
    """A redacted copy of a configuration that was actually in use."""

    def setUp(self):
        self.old = json.loads(FIXTURE.read_text())
        self.new = migrate(self.old)

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

    def test_the_instance_keeps_only_its_connection(self):
        self.assertNotIn('roots', self.new['instances'][0])
        self.assertEqual(self.new['instances'][0]['url'], 'http://sonarr.example:8989')

    def test_the_migrated_document_validates(self):
        settings = validate_settings(self.new)
        self.assertEqual(len(settings['rules']), 36)
        self.assertEqual(len(settings['profiles']), 4)

    def test_validation_preserves_every_rule_binding(self):
        # The real risk of a migration is silent loss, so this checks identity, not counts.
        settings = validate_settings(self.new)
        self.assertEqual(sorted(r['id'] for r in settings['rules']),
                         sorted(r['id'] for r in self.old['rules']))


if __name__ == '__main__':
    unittest.main()


class ToVersionFive(unittest.TestCase):
    """One monitoring mode replaces a per-series flag and an unwritten rule."""

    def test_everyone_lands_on_the_safe_mode(self):
        document = migrate({'settings_version': 4, 'retention': {'include_specials': True},
                            'rules': [{'id': 'r1', 'monitor_missing': True}]})
        self.assertEqual(document['settings_version'], SETTINGS_VERSION)
        self.assertEqual(document['retention']['monitoring'], 'unmonitor-only')
        self.assertTrue(document['retention']['include_specials'], 'other settings survive')

    def test_a_series_opted_into_downloads_is_not_carried_over(self):
        """An upgrade is the wrong moment to start hundreds of downloads."""
        document = migrate({'settings_version': 4, 'rules': [{'id': 'r1', 'monitor_missing': True}]})
        self.assertNotIn('monitor_missing', document['rules'][0])
        self.assertEqual(document['rules'][0]['monitoring'], '', 'inherits the global mode')

    def test_migrating_twice_changes_nothing_further(self):
        once = migrate({'settings_version': 4, 'rules': [{'id': 'r1', 'monitor_missing': True}]})
        self.assertEqual(migrate(once), once)

    def test_the_whole_chain_still_arrives(self):
        document = migrate({'dry_run': True, 'schedule': {'cron': '0 4 * * *', 'enabled': True}})
        self.assertEqual(document['settings_version'], SETTINGS_VERSION)
        self.assertEqual(document['retention']['monitoring'], 'unmonitor-only')


class ToVersionSix(unittest.TestCase):
    """A log that only speaks when something breaks is an empty file."""

    def test_the_old_default_becomes_the_new_one(self):
        document = migrate({'settings_version': 5, 'logging': {'level': 'warning'}})
        self.assertEqual(document['logging']['level'], 'info')

    def test_a_level_chosen_on_purpose_is_left_alone(self):
        for chosen in ('minimal', 'error', 'verbose'):
            document = migrate({'settings_version': 5, 'logging': {'level': chosen}})
            self.assertEqual(document['logging']['level'], chosen)

    def test_ordinary_activity_is_visible_at_the_default_level(self):
        # The bug in one assertion: at the old default, everything routine ranked above
        # the threshold and was dropped, so weeks of checks wrote nothing.
        from store import LOG_RANK
        from core import DEFAULTS
        self.assertLessEqual(LOG_RANK['info'], LOG_RANK[DEFAULTS['logging']['level']])
