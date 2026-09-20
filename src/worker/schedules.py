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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

FREQUENCIES = ['hourly', 'daily', 'weekly', 'monthly', 'custom']
MONTHLY_MODES = ['day', 'first', 'last']
# 0 is Sunday, matching the weekday order people read in a dropdown.
WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
CRON_FIELD = re.compile(r'^[0-9*/,\-]+$')
# Supported custom schedules may be sparse, including an annual date. The search is by
# civil date rather than minute, so a decade is still cheap and gives a bounded contract.
SEARCH_LIMIT_DAYS = 3660
DEFAULT_TIMEZONE = 'Etc/UTC'


class ScheduleError(ValueError):
    """An unusable schedule. The message is written for the person reading the UI."""


def sunday_index(moment: dt.datetime) -> int:
    """Weekday with Sunday as 0. Python counts from Monday; dropdowns start at Sunday."""
    return (moment.weekday() + 1) % 7


def _field_values(field: str, low: int, high: int) -> set:
    """Expand one cron field into the values it matches."""
    values = set()
    if not field or any(part == '' for part in field.split(',')):
        raise ScheduleError(f'Unsupported cron field "{field}"')
    for part in field.split(','):
        step = 1
        if '/' in part:
            part, _, raw_step = part.partition('/')
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ScheduleError(f'Unsupported step in "{field}"')
            step = int(raw_step)
        if part == '*':
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


def _parse_cron(expression: str):
    """Validate and expand every cron field before evaluating any match."""
    fields = str(expression or '').split()
    if len(fields) != 5:
        raise ScheduleError('A cron expression has five fields, for example "0 4 * * *"')
    if any(not CRON_FIELD.match(field) for field in fields):
        raise ScheduleError(f'Unsupported cron field "{next(field for field in fields if not CRON_FIELD.match(field))}"')
    minute, hour, day, month, weekday = fields
    return {
        'minute': _field_values(minute, 0, 59),
        'hour': _field_values(hour, 0, 23),
        'day': _field_values(day, 1, 31),
        'month': _field_values(month, 1, 12),
        'weekday': _field_values(weekday, 0, 7),
        'day_restricted': day != '*',
        'weekday_restricted': weekday != '*',
    }


def zone_for(schedule: dict, fallback=None):
    """Return the configured IANA timezone, refusing a missing zone database entry."""
    if not schedule.get('timezone') and fallback is not None:
        return fallback
    name = str(schedule.get('timezone') or DEFAULT_TIMEZONE).strip()
    if name in ('UTC', 'Etc/UTC', 'GMT'):
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ScheduleError(f'Unknown IANA timezone "{name}"') from error


def available_zones() -> list[str]:
    """Every IANA zone this image can actually resolve, for the interface to offer.

    Read from the running zone database rather than a list kept here, because the two
    can disagree: the container installs `tzdata` and carries America/Detroit, the host
    that runs the test gate carries a trimmed set and does not. A hard-coded list would
    eventually offer a zone `zone_for` then refuses, which is the failure a free-text
    field already had — you find out at save time, or never, because the name looked
    right.

    Region-prefixed names only. `Factory` and `localtime` are not places, and bare `UTC`
    and `GMT` are the same choice as `Etc/UTC` spelled differently.

    The default is always present even when the database omits it. A trimmed zone set
    can lack `Etc/UTC` — the gate host's does — but `zone_for` special-cases the name
    and never looks it up, so it is always a usable choice and a list that offered
    everything except the default would be a strange thing to hand somebody.
    """
    zones = {name for name in available_timezones() if '/' in name}
    zones.add(DEFAULT_TIMEZONE)
    return sorted(zones)


def _aware(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment


def occurrence_id(moment: dt.datetime) -> str:
    """Stable identity for one absolute scheduled instant, including fall-back folds."""
    return _aware(moment).astimezone(dt.timezone.utc).isoformat(timespec='seconds')


def check_cron(expression: str) -> None:
    """Raise ScheduleError unless this is a five-field cron expression we can read.

    Validation only. This was once `cron_matches`, a third copy of "does this fire at
    this minute" that its only caller invoked against a hard-coded date purely to make
    it raise — a parse check wearing a matcher's name, and one more place for the firing
    rules to drift.
    """
    _parse_cron(expression)


def _cron_date_matches(parsed: dict, moment: dt.datetime) -> bool:
    if moment.month not in parsed['month']:
        return False
    day_hit = moment.day in parsed['day']
    weekday = sunday_index(moment)
    weekday_hit = weekday in parsed['weekday'] or (weekday == 0 and 7 in parsed['weekday'])
    if parsed['day_restricted'] and parsed['weekday_restricted']:
        return day_hit or weekday_hit
    if parsed['day_restricted']:
        return day_hit
    if parsed['weekday_restricted']:
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


def _local_candidates(local: dt.datetime, zone) -> list:
    """Convert a civil time to zero, one, or two real instants across DST transitions."""
    found = {}
    naive = local.replace(tzinfo=None)
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        utc = candidate.astimezone(dt.timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            found[occurrence_id(utc)] = utc
    return sorted(found.values())


def day_matches(schedule: dict, local_date: dt.date) -> tuple[set, set]:
    """The hours and minutes this schedule fires at on one local date.

    The single source of frequency semantics. Both readers go through it: `occurs_at`
    asks about one minute, `_latest_on_date` asks about a whole day while searching
    backwards. They used to be two separate implementations of the same five rules, and
    only one of them was tested — the untested one being the one the scheduler actually
    ran. They had already drifted: a frequency this code does not recognise raised in
    `occurs_at` and silently fired as daily here, which is a typo in a stored document
    quietly deleting things a day at a time.

    Day-of-month and day-of-week are OR'd when both are restricted, which is what cron
    itself does. Only the custom option reaches that; the named frequencies do not.
    """
    frequency = schedule.get('frequency', 'daily')
    local = dt.datetime.combine(local_date, dt.time())
    if frequency == 'custom':
        parsed = _parse_cron(schedule.get('cron', ''))
        if not _cron_date_matches(parsed, local):
            return set(), set()
        return parsed['hour'], parsed['minute']
    if frequency == 'hourly':
        return set(range(24)), {int(schedule.get('minute', 0))}
    if frequency not in ('daily', 'weekly', 'monthly'):
        raise ScheduleError(f'Unknown frequency "{frequency}"')
    if frequency == 'weekly' and sunday_index(local) != int(schedule.get('weekday', 0)):
        return set(), set()
    if frequency == 'monthly' and local.day != monthly_day(schedule, local.year, local.month):
        return set(), set()
    return {int(schedule.get('hour', 0))}, {int(schedule.get('minute', 0))}


def occurs_at(schedule: dict, moment: dt.datetime) -> bool:
    """Whether this schedule fires at this exact minute.

    A thin reading of `day_matches`, so the frequency rules this asserts are the same
    ones the scheduler runs.
    """
    zone = zone_for(schedule, _aware(moment).tzinfo)
    local = _aware(moment).astimezone(zone)
    hours, minutes = day_matches(schedule, local.date())
    return local.hour in hours and local.minute in minutes


def _latest_on_date(schedule: dict, local_date: dt.date, zone, upper: dt.datetime):
    hours, minutes = day_matches(schedule, local_date)
    latest = None
    for hour in sorted(hours):
        for minute in sorted(minutes):
            local = dt.datetime.combine(local_date, dt.time(hour, minute), tzinfo=zone)
            for candidate in _local_candidates(local, zone):
                if candidate <= upper and (latest is None or candidate > latest):
                    latest = candidate
    return latest


def last_occurrence(schedule: dict, now: dt.datetime, limit_days: int = SEARCH_LIMIT_DAYS):
    """The most recent minute at or before `now` when this schedule fired.

    Searching backwards a minute at a time is uninteresting but exactly right: it needs no
    special case per frequency, and the longest gap between occurrences is a month, so the
    walk is bounded. A daily schedule finds its answer within a day of minutes.
    """
    current = _aware(now)
    zone = zone_for(schedule, current.tzinfo)
    upper = current.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    local_date = upper.astimezone(zone).date()
    for offset in range(limit_days + 1):
        candidate = _latest_on_date(schedule, local_date - dt.timedelta(days=offset), zone, upper)
        if candidate is not None:
            return candidate.astimezone(current.tzinfo or dt.timezone.utc)
    return None


def due_occurrence(schedule: dict, now: dt.datetime, last_seen):
    """Return the latest missed occurrence, or None when that occurrence was answered."""
    occurrence = last_occurrence(schedule, now)
    if occurrence is None:
        return None
    if last_seen is None:
        return occurrence
    if isinstance(last_seen, str):
        try:
            last_seen = dt.datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
        except ValueError:
            return occurrence
    last_seen = _aware(last_seen)
    return occurrence if last_seen.astimezone(dt.timezone.utc) < \
        occurrence.astimezone(dt.timezone.utc) else None


def is_due(schedule: dict, now: dt.datetime, last_run) -> bool:
    """Whether a job should run now, given when it last ran.

    True when an occurrence has passed that the job has not yet answered. A server that was
    off at the scheduled minute therefore catches up on its next tick rather than skipping
    the run entirely, which is the behaviour a crontab silently fails to provide.
    """
    if not schedule.get('enabled'):
        return False
    return due_occurrence(schedule, now, last_run) is not None


def describe(schedule: dict) -> str:
    """A plain sentence for the interface, so the stored fields are never shown raw."""
    if not schedule.get('enabled'):
        return 'Off'
    frequency = schedule.get('frequency', 'daily')
    minute = int(schedule.get('minute', 0))
    hour = int(schedule.get('hour', 0))
    clock = f'{hour:02d}:{minute:02d}'
    if frequency == 'custom':
        text = f'Custom: {schedule.get("cron", "")}'
    elif frequency == 'hourly':
        text = f'Every hour at {minute:02d} minutes past'
    elif frequency == 'daily':
        text = f'Every day at {clock}'
    elif frequency == 'weekly':
        text = f'Every {WEEKDAY_NAMES[int(schedule.get("weekday", 0))]} at {clock}'
    elif frequency == 'monthly':
        mode = schedule.get('monthly_mode', 'day')
        weekday = schedule.get('monthly_weekday')
        if mode == 'day':
            text = f'Day {int(schedule.get("monthly_day", 1))} of each month at {clock}'
        else:
            which = 'first' if mode == 'first' else 'last'
            if weekday in (None, ''):
                text = f'The {which} day of each month at {clock}'
            else:
                text = f'The {which} {WEEKDAY_NAMES[int(weekday)]} of each month at {clock}'
    else:
        return 'Off'
    timezone = schedule.get('timezone') or DEFAULT_TIMEZONE
    return text if timezone == DEFAULT_TIMEZONE else f'{text} ({timezone})'
