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


class OfferedZones(unittest.TestCase):
    """The interface offers zones from the running database, not a list kept in code.

    The two disagree: the container installs tzdata and carries America/Detroit, the
    host running this gate carries a trimmed set and does not. A hard-coded list would
    eventually offer a name `zone_for` refuses, which is the failure the free-text field
    had — you found out at save time.
    """

    def test_every_zone_offered_is_one_the_schedule_can_use(self):
        offered = schedules.available_zones()
        self.assertTrue(offered, 'the zone database produced nothing at all')
        for name in offered:
            schedules.zone_for({'timezone': name})   # raises ScheduleError if unusable

    def test_the_default_is_offered(self):
        self.assertIn(schedules.DEFAULT_TIMEZONE, schedules.available_zones())

    def test_only_places_are_offered(self):
        # Factory and localtime are not places, and bare UTC/GMT are Etc/UTC spelled
        # differently. Every entry has a region, which is what the grouping relies on.
        offered = schedules.available_zones()
        self.assertTrue(all('/' in name for name in offered))
        for junk in ('Factory', 'localtime', 'UTC', 'GMT'):
            self.assertNotIn(junk, offered)

    def test_the_list_is_sorted_and_free_of_duplicates(self):
        offered = schedules.available_zones()
        self.assertEqual(offered, sorted(offered))
        self.assertEqual(len(offered), len(set(offered)))


class CommonZones(unittest.TestCase):
    """One dropdown entry per distinct offset behaviour, not one per IANA name.

    A release carries roughly fifteen America/* zones that have kept identical civil
    time since the US unified its 2007 DST rule -- Detroit, four Indiana zones,
    Louisville, Toronto and more are all, today and for as far ahead as scheduling
    matters, indistinguishable from New York. Verified against the actual database
    rather than a fixture, because the whole point is not drifting from what
    `zone_for` can resolve.
    """

    def test_a_cluster_of_identical_zones_collapses_to_one_entry(self):
        # America/Detroit and America/New_York have shared every DST transition since
        # 2007; both zones existing separately in tzdata is a historical artifact this
        # picker has no reason to expose.
        if 'America/Detroit' not in schedules.available_zones() or \
                'America/New_York' not in schedules.available_zones():
            self.skipTest('this host does not carry both reference zones')
        names = [z['name'] for z in schedules.common_zones()]
        self.assertIn('America/New_York', names)
        self.assertNotIn('America/Detroit', names)

    def test_new_york_is_the_preferred_name_for_its_cluster(self):
        # The example the feature was asked for by name: picking the well-known city
        # over whichever cluster member happens to sort first alphabetically
        # (America/Detroit precedes America/New_York).
        if 'America/New_York' not in schedules.available_zones():
            self.skipTest('this host does not carry the reference zone')
        names = [z['name'] for z in schedules.common_zones()]
        self.assertIn('America/New_York', names)

    def test_zones_with_different_dst_behaviour_are_never_merged(self):
        # Same standard offset, opposite DST behaviour: merging any of these pairs
        # would be a wrong answer wearing a tidy list's clothes.
        pairs = (('America/Phoenix', 'America/Denver'),      # Arizona never changes clocks
                 ('Australia/Darwin', 'Australia/Adelaide'),  # NT never changes clocks
                 ('Australia/Brisbane', 'Australia/Sydney'))  # QLD never changes clocks
        available = set(schedules.available_zones())
        names = {z['name'] for z in schedules.common_zones()}
        for still, moves in pairs:
            if not ({still, moves} <= available):
                continue
            self.assertIn(still, names, f'{still} was merged away')
            self.assertIn(moves, names, f'{moves} was merged away')

    def test_posix_offset_zones_are_not_offered(self):
        # Etc/GMT+12 is UTC-12: the sign is inverted from every reader's expectation,
        # and it names an offset rather than a place. Etc/UTC is the one exception.
        names = [z['name'] for z in schedules.common_zones()]
        self.assertNotIn('Etc/GMT+12', names)
        self.assertTrue(all(not n.startswith('Etc/') or n == 'Etc/UTC' for n in names))

    def test_the_current_zone_is_appended_when_it_is_not_the_cluster_pick(self):
        # A stored value that clusters with something else, but is not the cluster's
        # own preferred name, must still be selectable -- changing nothing must never
        # change what is shown.
        available = schedules.available_zones()
        candidates = [n for n in available
                     if n != 'America/New_York' and n not in schedules.PREFERRED_ZONE_NAMES]
        target = next((n for n in candidates
                       if schedules.common_zones(current=n) != schedules.common_zones()), None)
        if target is None:
            self.skipTest('no non-canonical cluster member available on this host')
        with_current = [z['name'] for z in schedules.common_zones(current=target)]
        self.assertIn(target, with_current)

    def test_a_current_value_already_offered_is_not_duplicated(self):
        zones = schedules.common_zones(current=schedules.DEFAULT_TIMEZONE)
        names = [z['name'] for z in zones]
        self.assertEqual(names.count(schedules.DEFAULT_TIMEZONE), 1)

    def test_an_unresolvable_current_value_does_not_raise(self):
        # A corrupted settings document must not take the whole snapshot down over a
        # cosmetic list; the real error surfaces through zone_for at the point of use.
        zones = schedules.common_zones(current='Not/AZone')
        self.assertNotIn('Not/AZone', [z['name'] for z in zones])

    def test_every_offered_zone_still_resolves(self):
        for entry in schedules.common_zones():
            schedules.zone_for({'timezone': entry['name']})

    def test_entries_are_sorted_by_offset_then_label(self):
        zones = schedules.common_zones()
        keys = [(z['offset_minutes'], z['label']) for z in zones]
        self.assertEqual(keys, sorted(keys))

    def test_half_and_quarter_hour_offsets_are_labelled_correctly(self):
        if 'Asia/Kolkata' not in schedules.available_zones():
            self.skipTest('this host does not carry the reference zone')
        kolkata = next(z for z in schedules.common_zones() if z['name'] == 'Asia/Kolkata')
        self.assertEqual(kolkata['offset_minutes'], 330)

    def test_the_label_is_the_last_path_segment_with_underscores_as_spaces(self):
        if 'America/New_York' not in schedules.available_zones():
            self.skipTest('this host does not carry the reference zone')
        entry = next(z for z in schedules.common_zones() if z['name'] == 'America/New_York')
        self.assertEqual(entry['label'], 'New York')


class StandardOffset(unittest.TestCase):
    """The label offset never changes with the season, even in the hemisphere where
    DST falls across the calendar year boundary."""

    def test_a_southern_hemisphere_zone_reports_its_non_dst_offset(self):
        # Sydney observes DST across the southern summer (roughly Oct-Apr), so a naive
        # "read January" would report the DST offset, not standard time.
        if 'Australia/Sydney' not in schedules.available_zones():
            self.skipTest('this host does not carry the reference zone')
        zone = schedules._resolved('Australia/Sydney')
        self.assertEqual(schedules._standard_offset_minutes(zone, 2026), 600)  # +10:00

    def test_a_northern_hemisphere_zone_reports_its_non_dst_offset(self):
        if 'America/New_York' not in schedules.available_zones():
            self.skipTest('this host does not carry the reference zone')
        zone = schedules._resolved('America/New_York')
        self.assertEqual(schedules._standard_offset_minutes(zone, 2026), -300)  # -05:00

    def test_a_zone_with_no_dst_is_unaffected_by_which_probe_answers(self):
        if 'America/Phoenix' not in schedules.available_zones():
            self.skipTest('this host does not carry the reference zone')
        zone = schedules._resolved('America/Phoenix')
        self.assertEqual(schedules._standard_offset_minutes(zone, 2026), -420)  # -07:00
