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
        # `include_specials: True` meant "do not exclude them", and v10 says that the
        # other way round in the section where every automatic decision now lives.
        self.assertFalse(self.new['automation']['exclude_specials'])
        self.assertNotIn('include_specials', self.new['retention'])
        self.assertNotIn('auto_unmonitor', self.new['retention'])
        self.assertNotIn('auto_monitor', self.new['retention'])

    def test_the_separate_specials_monitoring_flag_is_gone(self):
        self.assertNotIn('monitor_specials', self.new['retention'])

    def test_a_tmdb_key_survives_without_its_enabled_flag(self):
        self.assertEqual(self.new['tmdb'], {'api_key': 'b' * 32})

    def test_old_notification_settings_are_removed(self):
        self.assertNotIn('notifications', self.new)
        self.assertEqual(self.new['connections']['tmdb']['api_key'], 'b' * 32)

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


class TheVersionNumber(unittest.TestCase):
    def test_validation_stamps_the_version_the_migration_reaches(self):
        """They were two constants, and only one got bumped.

        `migrate` raised the document to 12 and `validate_settings` stamped it back to 11,
        so every load migrated it again and the file on disk never moved.
        """
        import core
        self.assertEqual(core.SETTINGS_VERSION, SETTINGS_VERSION)

    def test_a_migrated_document_survives_validation_at_its_new_version(self):
        document = migrate({'settings_version': 11, 'instances': [], 'rules': []})
        self.assertEqual(validate_settings(document)['settings_version'], SETTINGS_VERSION)


class ToVersionTwelve(unittest.TestCase):
    """Full sync goes, and with it two settings groups nothing ever read."""

    def setUp(self):
        self.document = migrate({
            'settings_version': 11,
            'retention': {'monitoring': 'full-sync', 'allow_estimated_dates': True},
            'automation': {'monitoring': {'in_scope_unmonitored': 'monitor'},
                           'persistence': {'monitored_out_scope': 'unmonitor'},
                           'search_after_monitor': True, 'exclude_specials': False,
                           'exclude_seasons': [3]},
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'monitoring': 'full-sync', 'keep_days': 30},
                      {'id': 'r2', 'instance_id': 'i1', 'series_id': 2, 'path': '/tv/B',
                       'monitoring': '', 'keep_days': 60}],
        })

    def test_the_mode_is_gone_from_every_rule(self):
        for rule in self.document['rules']:
            self.assertNotIn('monitoring', rule)

    def test_a_series_opted_into_full_sync_lands_on_the_safe_behaviour(self):
        # There is nowhere left to record the choice, and nothing left that would act on
        # it: a run no longer monitors anything at all.
        self.assertNotIn('monitoring', self.document['retention'])

    def test_the_two_unread_groups_are_removed(self):
        self.assertNotIn('monitoring', self.document['automation'])
        self.assertNotIn('persistence', self.document['automation'])

    def test_everything_that_was_wired_survives(self):
        automation = self.document['automation']
        self.assertTrue(automation['search_after_monitor'])
        self.assertFalse(automation['exclude_specials'])
        self.assertEqual(automation['exclude_seasons'], [3])
        self.assertTrue(self.document['retention']['allow_estimated_dates'])

    def test_keep_values_are_untouched(self):
        self.assertEqual([r['keep_days'] for r in self.document['rules']], [30, 60])

    def test_migrating_twice_changes_nothing_further(self):
        self.assertEqual(migrate(self.document), self.document)

    def test_the_migrated_document_validates(self):
        settings = validate_settings(self.document)
        self.assertNotIn('monitoring', settings['retention'])
        self.assertNotIn('monitoring', settings['automation'])
        self.assertEqual(len(settings['rules']), 2)


if __name__ == '__main__':
    unittest.main()


class ToVersionFive(unittest.TestCase):
    """One monitoring mode replaced a per-series flag; v12 then removed the mode itself."""

    def test_everyone_lands_on_the_safe_mode(self):
        document = migrate({'settings_version': 4, 'retention': {'include_specials': True},
                            'rules': [{'id': 'r1', 'monitor_missing': True}]})
        self.assertEqual(document['settings_version'], SETTINGS_VERSION)
        self.assertNotIn('monitoring', document['retention'], 'v12 removed the mode entirely')
        self.assertFalse(document['automation']['exclude_specials'], 'other settings survive')

    def test_a_series_opted_into_downloads_is_not_carried_over(self):
        """An upgrade is the wrong moment to start hundreds of downloads."""
        document = migrate({'settings_version': 4, 'rules': [{'id': 'r1', 'monitor_missing': True}]})
        self.assertNotIn('monitor_missing', document['rules'][0])
        self.assertNotIn('monitoring', document['rules'][0])

    def test_migrating_twice_changes_nothing_further(self):
        once = migrate({'settings_version': 4, 'rules': [{'id': 'r1', 'monitor_missing': True}]})
        self.assertEqual(migrate(once), once)

    def test_the_whole_chain_still_arrives(self):
        document = migrate({'dry_run': True, 'schedule': {'cron': '0 4 * * *', 'enabled': True}})
        self.assertEqual(document['settings_version'], SETTINGS_VERSION)
        self.assertNotIn('monitoring', document['retention'])


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


class ToV9(unittest.TestCase):
    """The volume is not a setting.

    `state_dir` existed because the plugin had to be told where on somebody else's system
    to put its working data. A container is given one volume, and a stored path pointing at
    the host either fails inside the container or — worse — succeeds against the image's own
    writable layer and loses everything on the next restart.
    """

    def test_a_host_path_does_not_survive_the_move(self):
        moved = migrate({'settings_version': 8, 'state_dir': '/mnt/user/appdata/tv-retention'})
        self.assertNotIn('state_dir', moved)
        self.assertEqual(moved['settings_version'], SETTINGS_VERSION)

    def test_everything_else_is_left_alone(self):
        before = {'settings_version': 8, 'state_dir': '/mnt/user/appdata/tv-retention',
                  'rules': [{'id': 'r1', 'keep_days': 30}],
                  'notifications': {'errors': False, 'webhook_url': 'https://example.invalid/hook'}}
        after = migrate(before)
        self.assertEqual(after['rules'], [dict(before['rules'][0], combine='any')])
        self.assertNotIn('notifications', after)

    def test_nothing_asks_the_filesystem_anything_any_more(self):
        """The folder picker was the last thing that read a directory.

        With the volume no longer configurable there is nothing to pick, and *the plugin
        touches no filesystem at all* is true without qualification for the first time.
        """
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / 'src'
        # Across every module: "gone" means gone from the graph, not moved out of the entry.
        for path in sorted((root / 'assets').glob('*.js')):
            self.assertNotIn('browseFolder', path.read_text(encoding='utf-8'), path.name)
        self.assertNotIn('action_browse', (root / 'worker' / 'actions.py').read_text())


class ToV11(unittest.TestCase):
    """Condition names describe the keep decision rather than timeline arithmetic."""

    def test_both_old_safe_modes_become_any(self):
        for old in ('earliest', 'latest'):
            document = migrate({
                'settings_version': 10,
                'rules': [{'id': 'r1', 'combine': old}],
                'profiles': [{'id': 'p1', 'combine': old}],
            })
            self.assertEqual(document['rules'][0]['combine'], 'any')
            self.assertEqual(document['profiles'][0]['combine'], 'any')

    def test_the_old_aggressive_any_mode_becomes_all(self):
        document = migrate({
            'settings_version': 10,
            'rules': [{'id': 'r1', 'combine': 'any'}],
            'profiles': [{'id': 'p1', 'combine': 'any'}],
        })
        self.assertEqual(document['rules'][0]['combine'], 'all')
        self.assertEqual(document['profiles'][0]['combine'], 'all')

    def test_the_migration_is_idempotent(self):
        once = migrate({'settings_version': 10, 'rules': [{'combine': 'earliest'}]})
        self.assertEqual(migrate(once), once)
