import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import context  # noqa: F401
import schedules
from schedules import (ScheduleError, describe, due_occurrence, is_due, last_occurrence,
                        occurrence_id, occurs_at)


def at(year, month, day, hour=0, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)


def schedule(**fields):
    base = {'enabled': True, 'frequency': 'daily', 'minute': 0, 'hour': 4, 'weekday': 0,
            'monthly_mode': 'day', 'monthly_day': 1, 'monthly_weekday': '', 'cron': '0 4 * * *'}
    base.update(fields)
    return base


class Frequencies(unittest.TestCase):
    def test_hourly_fires_on_its_minute(self):
        s = schedule(frequency='hourly', minute=7)
        self.assertTrue(occurs_at(s, at(2026, 9, 7, 13, 7)))
        self.assertFalse(occurs_at(s, at(2026, 9, 7, 13, 8)))

    def test_daily_fires_once(self):
        s = schedule(frequency='daily', hour=4, minute=30)
        self.assertTrue(occurs_at(s, at(2026, 9, 7, 4, 30)))
        self.assertFalse(occurs_at(s, at(2026, 9, 7, 5, 30)))

    def test_daily_schedule_uses_the_offset_of_the_clock_it_is_given(self):
        eastern = dt.timezone(dt.timedelta(hours=-4), 'EDT')
        moment = dt.datetime(2026, 9, 7, 1, 0, tzinfo=eastern)
        self.assertTrue(occurs_at(schedule(frequency='daily', hour=1), moment))

    def test_weekly_uses_sunday_as_zero(self):
        # 2026-09-07 is a Monday.
        s = schedule(frequency='weekly', weekday=1, hour=4)
        self.assertTrue(occurs_at(s, at(2026, 9, 7, 4)))
        self.assertFalse(occurs_at(s, at(2026, 9, 8, 4)))

    def test_monthly_on_a_numbered_day(self):
        s = schedule(frequency='monthly', monthly_mode='day', monthly_day=15, hour=4)
        self.assertTrue(occurs_at(s, at(2026, 9, 15, 4)))
        self.assertFalse(occurs_at(s, at(2026, 9, 16, 4)))


class MonthlyWeekdays(unittest.TestCase):
    """The case with no cron equivalent, which is why the worker owns scheduling."""

    def test_first_monday(self):
        s = schedule(frequency='monthly', monthly_mode='first', monthly_weekday=1, hour=4)
        self.assertTrue(occurs_at(s, at(2026, 9, 7, 4)))    # first Monday of September
        self.assertFalse(occurs_at(s, at(2026, 9, 14, 4)))  # second Monday

    def test_last_friday(self):
        s = schedule(frequency='monthly', monthly_mode='last', monthly_weekday=5, hour=4)
        self.assertTrue(occurs_at(s, at(2026, 9, 25, 4)))
        self.assertFalse(occurs_at(s, at(2026, 9, 18, 4)))

    def test_first_day_of_the_month(self):
        s = schedule(frequency='monthly', monthly_mode='first', monthly_weekday='', hour=4)
        self.assertTrue(occurs_at(s, at(2026, 9, 1, 4)))

    def test_last_day_of_the_month_tracks_month_length(self):
        s = schedule(frequency='monthly', monthly_mode='last', monthly_weekday='', hour=4)
        self.assertTrue(occurs_at(s, at(2026, 9, 30, 4)))
        self.assertTrue(occurs_at(s, at(2026, 2, 28, 4)))
        self.assertFalse(occurs_at(s, at(2026, 9, 29, 4)))

    def test_february_in_a_leap_year(self):
        s = schedule(frequency='monthly', monthly_mode='last', monthly_weekday='', hour=4)
        self.assertTrue(occurs_at(s, at(2028, 2, 29, 4)))


class Custom(unittest.TestCase):
    def test_a_plain_expression(self):
        s = schedule(frequency='custom', cron='30 2 * * *')
        self.assertTrue(occurs_at(s, at(2026, 9, 7, 2, 30)))

    def test_steps_and_lists(self):
        s = schedule(frequency='custom', cron='*/15 1,2 * * *')
        self.assertTrue(occurs_at(s, at(2026, 9, 7, 2, 45)))
        self.assertFalse(occurs_at(s, at(2026, 9, 7, 3, 45)))

    def test_day_and_weekday_are_ored_as_cron_does(self):
        # This is exactly the trap that makes "first Monday" impossible in cron.
        s = schedule(frequency='custom', cron='0 4 1-7 * 1')
        self.assertTrue(occurs_at(s, at(2026, 9, 3, 4)))   # in 1-7, a Thursday
        self.assertTrue(occurs_at(s, at(2026, 9, 14, 4)))  # a Monday, outside 1-7

    def test_a_malformed_expression_is_refused(self):
        with self.assertRaises(ScheduleError):
            occurs_at(schedule(frequency='custom', cron='nightly'), at(2026, 9, 7))

    def test_an_out_of_range_field_is_refused(self):
        with self.assertRaises(ScheduleError):
            occurs_at(schedule(frequency='custom', cron='0 99 * * *'), at(2026, 9, 7))

    def test_a_later_invalid_field_is_refused_even_when_an_earlier_field_misses(self):
        with self.assertRaises(ScheduleError):
            occurs_at(schedule(frequency='custom', cron='1 99 * * *'), at(2026, 9, 7))

    def test_an_annual_custom_schedule_catches_up_after_forty_days(self):
        s = schedule(frequency='custom', cron='0 4 1 1 *')
        self.assertTrue(is_due(s, at(2027, 1, 2, 9), '2026-01-01T04:00:00+00:00'))


class LastOccurrence(unittest.TestCase):
    def test_finds_todays_run(self):
        s = schedule(frequency='daily', hour=4)
        # The result keeps the timezone of the moment it was asked about.
        self.assertEqual(last_occurrence(s, at(2026, 9, 7, 12)), at(2026, 9, 7, 4))

    def test_falls_back_to_yesterday_before_the_hour(self):
        s = schedule(frequency='daily', hour=4)
        self.assertEqual(last_occurrence(s, at(2026, 9, 7, 3)), at(2026, 9, 6, 4))

    def test_monthly_reaches_back_into_the_previous_month(self):
        s = schedule(frequency='monthly', monthly_mode='day', monthly_day=1, hour=4)
        self.assertEqual(last_occurrence(s, at(2026, 9, 20, 12)).month, 9)
        self.assertEqual(last_occurrence(s, at(2026, 9, 1, 3)).month, 8)

    def test_schedule_uses_its_iana_timezone(self):
        s = schedule(frequency='daily', hour=1, minute=30, timezone='America/New_York')
        found = last_occurrence(s, dt.datetime(2026, 9, 7, 6, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(found, dt.datetime(2026, 9, 7, 5, 30, tzinfo=dt.timezone.utc))

    def test_a_nonexistent_spring_forward_time_is_skipped(self):
        s = schedule(frequency='daily', hour=2, minute=30, timezone='America/New_York')
        found = last_occurrence(s, dt.datetime(2026, 3, 8, 7, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(found, dt.datetime(2026, 3, 7, 7, 30, tzinfo=dt.timezone.utc))

    def test_fall_back_occurrences_have_distinct_durable_identities(self):
        s = schedule(frequency='daily', hour=1, minute=30, timezone='America/New_York')
        first = dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc)
        second = dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc)
        self.assertNotEqual(occurrence_id(first), occurrence_id(second))
        self.assertEqual(due_occurrence(s, second + dt.timedelta(minutes=5), occurrence_id(first)), second)
        self.assertIsNone(due_occurrence(s, second + dt.timedelta(minutes=5), occurrence_id(second)))


class Due(unittest.TestCase):
    def test_a_disabled_schedule_is_never_due(self):
        self.assertFalse(is_due(schedule(enabled=False), at(2026, 9, 7, 12), None))

    def test_a_job_that_has_never_run_is_due(self):
        self.assertTrue(is_due(schedule(), at(2026, 9, 7, 12), None))

    def test_a_job_that_ran_after_the_occurrence_is_not_due(self):
        self.assertFalse(is_due(schedule(hour=4), at(2026, 9, 7, 12), at(2026, 9, 7, 5).isoformat()))

    def test_a_job_that_ran_before_the_occurrence_is_due(self):
        self.assertTrue(is_due(schedule(hour=4), at(2026, 9, 7, 12), at(2026, 9, 6, 23).isoformat()))

    def test_a_run_missed_while_the_server_was_off_is_caught_up(self):
        # The behaviour a crontab cannot offer: 04:00 passed while powered down, and the
        # first tick after boot at 09:00 still runs it.
        self.assertTrue(is_due(schedule(hour=4), at(2026, 9, 7, 9), at(2026, 9, 6, 4).isoformat()))

    def test_a_naive_timestamp_is_treated_as_utc_not_rejected(self):
        self.assertFalse(is_due(schedule(hour=4), at(2026, 9, 7, 12), '2026-09-07T05:00:00'))

    def test_an_unreadable_timestamp_runs_rather_than_stalling_for_ever(self):
        self.assertTrue(is_due(schedule(hour=4), at(2026, 9, 7, 12), 'not a date'))


class Description(unittest.TestCase):
    def test_each_frequency_reads_as_a_sentence(self):
        self.assertEqual(describe(schedule(enabled=False)), 'Off')
        self.assertEqual(describe(schedule(frequency='hourly', minute=5)),
                         'Every hour at 05 minutes past')
        self.assertEqual(describe(schedule(frequency='daily', hour=4, minute=30)),
                         'Every day at 04:30')
        self.assertEqual(describe(schedule(frequency='weekly', weekday=0, hour=4)),
                         'Every Sunday at 04:00')
        self.assertEqual(describe(schedule(frequency='monthly', monthly_mode='last',
                                           monthly_weekday=5, hour=4)),
                         'The last Friday of each month at 04:00')
        self.assertEqual(describe(schedule(frequency='monthly', monthly_mode='first',
                                           monthly_weekday='', hour=4)),
                         'The first day of each month at 04:00')

    def test_weekday_names_start_at_sunday(self):
        self.assertEqual(schedules.WEEKDAY_NAMES[0], 'Sunday')
        self.assertEqual(schedules.WEEKDAY_NAMES[6], 'Saturday')


class OneMatcher(unittest.TestCase):
    """Every check in this file reads `day_matches`, which is what the scheduler runs.

    It was two implementations: `occurs_at`, which every frequency test above exercises,
    and the day-at-a-time reader behind `last_occurrence`, which nothing tested and the
    worker actually used. They had drifted — an unrecognised frequency raised in the
    tested one and silently fired as daily in the live one.
    """

    def test_both_readers_refuse_a_frequency_neither_understands(self):
        broken = schedule(frequency='Daily')      # a capitalised typo in a stored document
        with self.assertRaises(ScheduleError):
            occurs_at(broken, at(2026, 9, 20, 4))
        with self.assertRaises(ScheduleError):
            due_occurrence(broken, at(2026, 9, 20, 12), None)
        with self.assertRaises(ScheduleError):
            last_occurrence(broken, at(2026, 9, 20, 12))

    def test_the_predicate_agrees_with_the_day_reader_everywhere(self):
        """`occurs_at` must stay a thin reading of `day_matches` rather than a rival."""
        for fields in ({'frequency': 'hourly'},
                       {'frequency': 'daily'},
                       {'frequency': 'weekly', 'weekday': 1},
                       {'frequency': 'monthly', 'monthly_mode': 'day', 'monthly_day': 15},
                       {'frequency': 'custom', 'cron': '30 4 * * 1'}):
            current = schedule(minute=30, hour=4, **fields)
            for day in range(1, 29):
                for hour in (0, 4, 13, 23):
                    moment = at(2026, 9, day, hour, 30)
                    hours, minutes = schedules.day_matches(current, moment.date())
                    self.assertEqual(occurs_at(current, moment),
                                     moment.hour in hours and moment.minute in minutes,
                                     f'{fields} disagreed at {moment}')

    def test_a_bad_cron_expression_is_refused_by_the_validator(self):
        # core calls this to validate a stored expression; it used to call the matcher
        # against a hard-coded date purely to make it raise.
        schedules.check_cron('0 4 * * *')
        for bad in ('nightly', '0 99 * * *', '0 4 * *'):
            with self.assertRaises(ScheduleError):
                schedules.check_cron(bad)


if __name__ == '__main__':
    unittest.main()
