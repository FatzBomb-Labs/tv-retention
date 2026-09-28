"""Calendar projections use only cached Sonarr data and the retention evaluator."""
import datetime as dt
import unittest
from unittest.mock import patch

import context  # noqa: F401
from fake_sonarr import IsolatedWorker
from test_mode_boundary import cached_rule


FIXED_NOW = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


class Calendar(unittest.TestCase):
    def call(self, fixture, settings):
        import actions
        with patch.object(actions.dt, 'datetime', FixedDateTime), \
                patch.object(actions, 'load_settings', return_value=settings):
            return actions.dispatch({'action': 'calendar'})

    def test_airings_use_cached_catalogue_including_unmanaged_series(self):
        from store import SCHEMA, write_cache
        import actions
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            settings['rules'][0]['queue'] = {'removal': None, 'fixes': []}
            fixture.store.save_settings(settings)
            write_cache(settings, 'catalogue.json', {
                'fake': {'schema': SCHEMA, 'fetched_at': '2026-09-01T08:00:00+00:00',
                         'series': [
                             {'series_id': 1, 'title': 'Managed', 'next_airing': '2026-09-05T20:00:00Z'},
                             {'series_id': 2, 'title': 'Unmanaged', 'next_airing': '2026-09-06T20:00:00Z'},
                         ]}
            })
            fixture.store.store_episodes(settings, settings['rules'][0], [{
                'episode_id': 101, 'file_id': None, 'has_file': False, 'series_id': 1,
                'season': 2, 'episode': 3, 'title': 'Managed episode',
                'air_date': '2026-09-05', 'air_source': 'sonarr', 'monitored': True,
                'path': 'sonarr:episode:101', 'size': 0,
            }], {})

            with patch.object(actions, 'load_settings_strict', side_effect=AssertionError(
                    'calendar dispatch must use read-only settings loading')):
                result = self.call(fixture, settings)

            self.assertTrue(result['ok'], result)
            events = result['events']
            self.assertEqual([(event['date'], event['title']) for event in events if event['kind'] == 'airing'], [
                ('2026-09-05', 'Fixture'), ('2026-09-05', 'Managed'),
                ('2026-09-06', 'Unmanaged'),
            ])
            managed_detail = next(event for event in events if event.get('episode'))
            self.assertEqual(managed_detail['episode'], {
                'season': 2, 'number': 3, 'title': 'Managed episode'})
            self.assertEqual(fixture.sonarr.requests, [])
            fixture.sonarr.assert_finished()

    def test_scheduled_estimates_apply_core_exclusions_and_never_promise_deletion(self):
        import actions
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            rule = settings['rules'][0]
            rule.update(queue={'removal': None, 'fixes': []}, keep_days=2, combine='any', enabled=True,
                        keep_episodes=None, keep_seasons=None,
                        exclusions=[{'season': 1, 'episode': 2}])
            settings['schedule'].update(enabled=True, frequency='daily', hour=12, minute=0,
                                        timezone='Etc/UTC', test_mode=True)
            settings['rules'][0]['queue']['removal'] = None
            settings['rules'][0]['enabled'] = True
            fixture.store.save_settings(settings)
            episodes = [
                {'episode_id': 101, 'file_id': 50, 'has_file': True, 'series_id': 1,
                 'season': 1, 'episode': 1, 'title': 'Will age out',
                 'air_date': '2026-08-02', 'air_source': 'sonarr', 'monitored': True,
                 'path': '/tv/Fixture/shared.mkv', 'size': 1000},
                {'episode_id': 102, 'file_id': 50, 'has_file': True, 'series_id': 1,
                 'season': 1, 'episode': 2, 'title': 'Hand excluded',
                 'air_date': '2026-08-01', 'air_source': 'sonarr', 'monitored': True,
                 'path': '/tv/Fixture/shared.mkv', 'size': 1000},
                {'episode_id': 103, 'file_id': 51, 'has_file': True, 'series_id': 1,
                 'season': 1, 'episode': 3, 'title': 'Unprotected old episode',
                 'air_date': '2026-08-01', 'air_source': 'sonarr', 'monitored': True,
                 'path': '/tv/Fixture/old.mkv', 'size': 1000},
            ]
            fixture.main.interpolate_air_dates(episodes)
            fixture.store.store_episodes(settings, rule, episodes, {})
            self.assertIsNotNone(fixture.store.episode_cache(settings, rule)[0])

            result = self.call(fixture, settings)

            self.assertTrue(result['ok'], result)
            estimates = [event for event in result['events'] if event['kind'] == 'estimate']
            self.assertEqual([(event['date'], event['episode']['title']) for event in estimates], [
                ('2026-09-01', 'Unprotected old episode'),
            ])
            self.assertIn('Possible retention deletion', estimates[0]['detail'])
            self.assertIn('may change', estimates[0]['detail'])
            self.assertEqual(fixture.sonarr.requests, [])

    def test_schedule_forecast_uses_local_wall_time_and_does_not_include_ambiguous_fallback_run(self):
        import actions
        now = dt.datetime(2026, 11, 1, 4, 0, tzinfo=dt.timezone.utc)
        schedule = {'enabled': True, 'frequency': 'daily', 'hour': 1, 'minute': 30,
                    'timezone': 'America/New_York'}

        runs = actions._calendar_runs(schedule, now, horizon_days=1)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][0], dt.date(2026, 11, 1))
        # The repeated 01:30 wall time has two possible instants. Use its first
        # occurrence, rather than reporting two runs or an arbitrary noon UTC time.
        self.assertEqual(runs[0][1], dt.datetime(2026, 11, 1, 5, 30,
                                                  tzinfo=dt.timezone.utc))

    def test_expired_files_are_forecast_once_not_re_evaluated_on_every_schedule_day(self):
        import actions
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            rule = settings['rules'][0]
            rule.update(queue={'removal': None, 'fixes': []}, keep_days=2, combine='any', enabled=True,
                        keep_episodes=None, keep_seasons=None)
            settings['schedule'].update(enabled=True, frequency='daily', hour=12, minute=0,
                                        timezone='Etc/UTC', test_mode=True)
            fixture.store.save_settings(settings)
            episodes = [{
                'episode_id': 101, 'file_id': 50, 'has_file': True, 'series_id': 1,
                'season': 1, 'episode': 1, 'title': 'Old episode',
                'air_date': '2026-08-01', 'air_source': 'sonarr', 'monitored': False,
                'path': '/tv/Fixture/old.mkv', 'size': 1000,
            }]
            fixture.store.store_episodes(settings, rule, episodes, {})

            with patch.object(actions, 'evaluate', wraps=actions.evaluate) as evaluate_mock:
                result = self.call(fixture, settings)

            self.assertTrue(result['ok'], result)
            forecasts = [event for event in result['events'] if event['kind'] == 'estimate']
            self.assertEqual(len(forecasts), 1)
            self.assertEqual(evaluate_mock.call_count, 1)
            fixture.sonarr.assert_finished()

    def test_queued_actions_follow_the_schedule_but_off_schedules_project_nothing(self):
        with IsolatedWorker() as fixture:
            settings = fixture.settings()
            settings['rules'][0]['queue'] = {'removal': {'action': 'unmonitor-all', 'created_at': '2026-09-01T08:00:00Z'}, 'fixes': []}
            settings['schedule'].update(enabled=True, frequency='daily', hour=12, minute=0,
                                        timezone='Etc/UTC', test_mode=True)
            fixture.store.save_settings(settings)

            result = self.call(fixture, settings)

            self.assertTrue(result['ok'], result)
            queued = [event for event in result['events'] if event['kind'] == 'queued']
            self.assertEqual([event['date'] for event in queued], ['2026-09-01'])
            self.assertIn('Test Mode', queued[0]['detail'])
            self.assertEqual(fixture.sonarr.requests, [])

            settings['schedule']['enabled'] = False
            fixture.store.save_settings(settings)
            result = self.call(fixture, settings)
            self.assertFalse(any(event['kind'] in ('queued', 'estimate')
                                 for event in result['events']))
            fixture.sonarr.assert_finished()


if __name__ == '__main__':
    unittest.main()
