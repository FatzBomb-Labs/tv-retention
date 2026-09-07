#!/usr/bin/env python3
"""When a job is due.

The plugin does not hand a cron expression to the system and hope. Cron fires this worker
on a fixed tick and the worker decides whether a scheduled moment has passed since the job
last ran. That buys three things a generated crontab cannot:

* "first Monday of the month" and similar have no cron form at all — day-of-month and
  day-of-week are OR'd when both are restricted, so `0 4 1-7 * MON` means "the 1st to the
  7th, *or* any Monday". Every product that offers this feature works around it.
* A run missed because the server was down can be noticed and caught up.
* A job blocked by something transient (Sonarr unreachable) can wait and run on recovery,
  because the worker is already awake and deciding.

Everything here is pure: it takes a schedule and a moment and answers questions about them.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re

FREQUENCIES = ['hourly', 'daily', 'weekly', 'monthly', 'custom']
MONTHLY_MODES = ['day', 'first', 'last']
# 0 is Sunday, matching the weekday order people read in a dropdown.
WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
CRON_FIELD = re.compile(r'^[0-9*/,\-]+$')
# A month is the longest gap between occurrences, so a search never needs to look further.
SEARCH_LIMIT_DAYS = 40


class ScheduleError(ValueError):
    """An unusable schedule. The message is written for the person reading the UI."""


def sunday_index(moment: dt.datetime) -> int:
    """Weekday with Sunday as 0. Python counts from Monday; dropdowns start at Sunday."""
    return (moment.weekday() + 1) % 7


def _field_values(field: str, low: int, high: int) -> set:
    """Expand one cron field into the values it matches."""
    values = set()
    for part in field.split(','):
        step = 1
        if '/' in part:
            part, _, raw_step = part.partition('/')
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ScheduleError(f'Unsupported step in "{field}"')
            step = int(raw_step)
        if part in ('*', ''):
            start, end = low, high
        elif '-' in part:
            first, _, last = part.partition('-')
            if not (first.isdigit() and last.isdigit()):
                raise ScheduleError(f'Unsupported range in "{field}"')
            start, end = int(first), int(last)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ScheduleError(f'Unsupported cron field "{field}"')
        if start < low or end > high or start > end:
            raise ScheduleError(f'Cron field "{field}" is outside {low}-{high}')
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expression: str, moment: dt.datetime) -> bool:
    """Whether a five-field cron expression fires at this minute.

    Day-of-month and day-of-week are OR'd when both are restricted, which is what cron
    itself does. Only the custom option reaches this; the named frequencies do not go
    anywhere near it.
    """
    fields = str(expression or '').split()
    if len(fields) != 5:
        raise ScheduleError('A cron expression has five fields, for example "0 4 * * *"')
    for field in fields:
        if not CRON_FIELD.match(field):
            raise ScheduleError(f'Unsupported cron field "{field}"')
    minute, hour, day, month, weekday = fields
    if moment.minute not in _field_values(minute, 0, 59):
        return False
    if moment.hour not in _field_values(hour, 0, 23):
        return False
    if moment.month not in _field_values(month, 1, 12):
        return False
    day_restricted = day.strip() != '*'
    weekday_restricted = weekday.strip() != '*'
    day_hit = moment.day in _field_values(day, 1, 31)
    weekday_hit = sunday_index(moment) in _field_values(weekday, 0, 7) or \
        (sunday_index(moment) == 0 and 7 in _field_values(weekday, 0, 7))
    if day_restricted and weekday_restricted:
        return day_hit or weekday_hit
    if day_restricted:
        return day_hit
    if weekday_restricted:
        return weekday_hit
    return True


def nth_weekday(year: int, month: int, weekday: int, last: bool = False) -> int:
    """Day of the month for the first or last given weekday. Sunday is 0."""
    days = calendar.monthrange(year, month)[1]
    candidates = [day for day in range(1, days + 1)
                  if sunday_index(dt.datetime(year, month, day)) == weekday]
    return candidates[-1] if last else candidates[0]


def monthly_day(schedule: dict, year: int, month: int) -> int:
    """The day of a given month this schedule targets."""
    mode = schedule.get('monthly_mode', 'day')
    if mode == 'day':
        # Capped at 28 in validation, so every month has the day.
        return int(schedule.get('monthly_day', 1))
    weekday = schedule.get('monthly_weekday')
    if weekday in (None, ''):
        # "First" or "Last" day of the month, with no weekday chosen.
        return 1 if mode == 'first' else calendar.monthrange(year, month)[1]
    return nth_weekday(year, month, int(weekday), last=(mode == 'last'))


def occurs_at(schedule: dict, moment: dt.datetime) -> bool:
    """Whether this schedule fires at this exact minute."""
    frequency = schedule.get('frequency', 'daily')
    if frequency == 'custom':
        return cron_matches(schedule.get('cron', ''), moment)
    if moment.minute != int(schedule.get('minute', 0)):
        return False
    if frequency == 'hourly':
        return True
    if moment.hour != int(schedule.get('hour', 0)):
        return False
    if frequency == 'daily':
        return True
    if frequency == 'weekly':
        return sunday_index(moment) == int(schedule.get('weekday', 0))
    if frequency == 'monthly':
        return moment.day == monthly_day(schedule, moment.year, moment.month)
    raise ScheduleError(f'Unknown frequency "{frequency}"')


def last_occurrence(schedule: dict, now: dt.datetime, limit_days: int = SEARCH_LIMIT_DAYS):
    """The most recent minute at or before `now` when this schedule fired.

    Searching backwards a minute at a time is uninteresting but exactly right: it needs no
    special case per frequency, and the longest gap between occurrences is a month, so the
    walk is bounded. A daily schedule finds its answer within a day of minutes.
    """
    moment = now.replace(second=0, microsecond=0)
    for _ in range(limit_days * 24 * 60):
        if occurs_at(schedule, moment):
            return moment
        moment -= dt.timedelta(minutes=1)
    return None


def is_due(schedule: dict, now: dt.datetime, last_run) -> bool:
    """Whether a job should run now, given when it last ran.

    True when an occurrence has passed that the job has not yet answered. A server that was
    off at the scheduled minute therefore catches up on its next tick rather than skipping
    the run entirely, which is the behaviour a crontab silently fails to provide.
    """
    if not schedule.get('enabled'):
        return False
    occurrence = last_occurrence(schedule, now)
    if occurrence is None:
        return False
    if last_run is None:
        return True
    if isinstance(last_run, str):
        try:
            last_run = dt.datetime.fromisoformat(last_run)
        except ValueError:
            return True
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=dt.timezone.utc)
    if occurrence.tzinfo is None:
        occurrence = occurrence.replace(tzinfo=dt.timezone.utc)
    return last_run < occurrence


def describe(schedule: dict) -> str:
    """A plain sentence for the interface, so the stored fields are never shown raw."""
    if not schedule.get('enabled'):
        return 'Off'
    frequency = schedule.get('frequency', 'daily')
    minute = int(schedule.get('minute', 0))
    hour = int(schedule.get('hour', 0))
    clock = f'{hour:02d}:{minute:02d}'
    if frequency == 'custom':
        return f'Custom: {schedule.get("cron", "")}'
    if frequency == 'hourly':
        return f'Every hour at {minute:02d} minutes past'
    if frequency == 'daily':
        return f'Every day at {clock}'
    if frequency == 'weekly':
        return f'Every {WEEKDAY_NAMES[int(schedule.get("weekday", 0))]} at {clock}'
    if frequency == 'monthly':
        mode = schedule.get('monthly_mode', 'day')
        weekday = schedule.get('monthly_weekday')
        if mode == 'day':
            return f'Day {int(schedule.get("monthly_day", 1))} of each month at {clock}'
        which = 'first' if mode == 'first' else 'last'
        if weekday in (None, ''):
            return f'The {which} day of each month at {clock}'
        return f'The {which} {WEEKDAY_NAMES[int(weekday)]} of each month at {clock}'
    return 'Off'
