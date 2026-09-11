#!/usr/bin/env python3
"""Bring a stored settings document up to the current shape.

Settings live on the flash device and outlive any release, so every change of shape needs
a path forward for what is already there. Migration runs on load, is idempotent, and never
discards a value it does not recognise: an unknown key is left in place rather than
dropped, so downgrading and re-upgrading cannot lose a setting.
"""
from __future__ import annotations

import re

SETTINGS_VERSION = 11

# The five-field cron subset the old release generated, mapped back to the structured form
# so an existing schedule keeps firing at the same time after the upgrade.
SIMPLE_CRON = re.compile(r'^(\d+) (\d+) (\*|\d+) \* (\*|\d+)$')


def schedule_from_cron(expression: str, enabled: bool) -> dict:
    """Recover a structured schedule from a cron line this plugin previously wrote."""
    schedule = {'enabled': bool(enabled), 'frequency': 'custom', 'minute': 0, 'hour': 4,
                'weekday': 0, 'monthly_mode': 'day', 'monthly_day': 1, 'monthly_weekday': '',
                'cron': str(expression or '0 4 * * *')}
    found = SIMPLE_CRON.match(str(expression or '').strip())
    if not found:
        return schedule
    minute, hour, day, weekday = found.groups()
    schedule['minute'] = int(minute)
    schedule['hour'] = int(hour)
    if day == '*' and weekday == '*':
        schedule['frequency'] = 'daily'
    elif day == '*':
        schedule['frequency'] = 'weekly'
        schedule['weekday'] = int(weekday) % 7
    elif weekday == '*':
        schedule['frequency'] = 'monthly'
        schedule['monthly_mode'] = 'day'
        schedule['monthly_day'] = min(28, max(1, int(day)))
    else:
        return schedule
    schedule.pop('cron', None)
    schedule['cron'] = str(expression)
    return schedule


def _guard(value, fallback: int) -> dict:
    """A guard was a bare number; it is now a number that can be switched off."""
    if isinstance(value, dict):
        return {'enabled': bool(value.get('enabled', True)), 'value': int(value.get('value', fallback))}
    try:
        number = int(value)
    except (TypeError, ValueError):
        return {'enabled': True, 'value': fallback}
    return {'enabled': True, 'value': number}


def migrate(raw: dict) -> dict:
    """Upgrade a settings document in place-safe fashion, returning the new one."""
    if not isinstance(raw, dict):
        return {'settings_version': SETTINGS_VERSION}
    document = dict(raw)
    version = int(document.get('settings_version') or 1)
    if version >= SETTINGS_VERSION:
        return document

    if version < 2:
        document.update(_to_v2(document))
    if version < 3:
        document.update(_to_v3(document))
    if version < 4:
        document.update(_to_v4(document))
    if version < 5:
        document.update(_to_v5(document))
    if version < 6:
        document.update(_to_v6(document))
    if version < 7:
        document.update(_to_v7(document))
    if version < 8:
        document.update(_to_v8(document))
    if version < 9:
        document.pop('state_dir', None)
    if version < 10:
        document.update(_to_v10(document))
    if version < 11:
        document.update(_to_v11(document))
    document['settings_version'] = SETTINGS_VERSION
    return document


def _to_v11(document: dict) -> dict:
    """Replace two identical timeline names with the decision they actually express.

    `earliest` and `latest` both deleted only when every condition voted to delete, so
    both become the safe `any` mode: any keep vote keeps the episode. The old `any` mode
    deleted on one delete vote, so it becomes the aggressive `all` mode: every keep
    condition must agree. `all` deliberately improves the old mode's handling of missing
    facts: an unknown can no longer authorize deletion, so migration may retain more but
    can never delete more than the stored rule did.
    """
    def migrate_conditions(entry):
        moved = dict(entry)
        old = moved.get('combine', 'earliest')
        moved['combine'] = 'all' if old == 'any' else 'any'
        return moved

    return {
        'profiles': [migrate_conditions(entry) for entry in document.get('profiles') or []],
        'rules': [migrate_conditions(entry) for entry in document.get('rules') or []],
    }


def _to_v10(document: dict) -> dict:
    """Specials stop being a setting of their own and become an exclusion.

    They were decided in two places: `excluded_episodes`, then a `season == 0` branch a few
    lines further down in both `evaluate` and `keep_frame`. Two gates answering the same
    kind of question is how "what will this run skip" ends up with two answers, and the
    second one was invisible — nothing on screen ever said that fifteen episodes had been
    set aside.

    `include_specials: False` meant "exclude them", so it becomes `exclude_specials: True`.
    The per-series tristate is untouched and keeps its meaning: a series that says it
    includes specials is opting out of the safety.

    `search_after_monitor` moves from `retention` to `automation` because it is an
    automatic action rather than a retention condition, and this is the release where
    everything automatic went to one page.
    """
    retention = dict(document.get('retention') or {})
    automation = dict(document.get('automation') or {})
    automation.setdefault('exclude_specials', not retention.pop('include_specials', False))
    automation.setdefault('search_after_monitor', retention.pop('search_after_monitor', False))
    # The one pattern list became two, because a season folder and an episode are two
    # questions. A phrase written when there was one box could have meant either, so it is
    # kept as both rather than guessed at: the alternative is silently dropping an
    # exclusion somebody is relying on.
    old = automation.pop('exclude_matching', None)
    if old:
        automation.setdefault('exclude_folders', list(old))
        automation.setdefault('exclude_episodes', list(old))
    return {'retention': retention, 'automation': automation}


# v9 is a removal rather than a function: `state_dir` was a setting because the plugin had
# to be told where on somebody else's system to put its working data. A container is given
# one volume, and a stored path pointing at the host — `/mnt/user/appdata/tv-retention`, say
# — either fails inside the container or, worse, succeeds against the image's own writable
# layer and loses everything on the next restart. Dropping the key lets it re-default.


def _to_v8(document: dict) -> dict:
    """Notifications go outward now, rather than into the host.

    The plugin shelled out to Unraid's `notify` script, so there was nowhere to configure:
    the destination was the machine it was installed on. A container has to be told, and
    empty means nowhere — the flags that say *which* events matter are unchanged, and keep
    whatever they were set to.
    """
    notifications = dict(document.get('notifications') or {})
    notifications.setdefault('webhook_url', '')
    return {'notifications': notifications}


def _to_v7(document: dict) -> dict:
    """Two schedules described work the plugin now does on its own.

    The series-match check verified that every rule still resolved to one Sonarr series —
    which every check already does — and it was born comparing this server's mount paths
    against Sonarr's, back when the plugin had mount paths. The connectivity interval
    described a question the tick asks anyway, and asks again before anything that needs
    the answer. Both were controls for work that happens regardless, which is two ways to
    configure something into being wrong. Only the reading lifetime survives.
    """
    health = {'ttl_hours': int((document.get('health') or {}).get('ttl_hours', 24) or 24)}
    return {'health': health}


def _to_v6(document: dict) -> dict:
    """The log had a level for warnings and none for work.

    Everything routine was logged at `verbose` and the default was `warning`, so a healthy
    plugin wrote nothing at all: the Live Log stayed empty through weeks of checks, sweeps
    and edits, which teaches nobody anything about a thing that is working. `info` is the
    new default, and anyone left on the old default is moved onto it — the intent was
    "ordinary activity", not "silence until something breaks".
    """
    logging = dict(document.get('logging') or {})
    if logging.get('level', 'warning') == 'warning':
        logging['level'] = 'info'
    return {'logging': logging}


def _to_v5(document: dict) -> dict:
    """One monitoring mode replaces a per-series flag and an unwritten rule.

    Monitoring used to be two decisions nobody could see together: episodes on disk inside
    the window were always monitored, and missing ones were opted into per series. It took
    a measurement against a real library — 28 on disk against 262 missing — to see that
    those halves were nothing alike, which is a sign the model was wrong rather than that
    the numbers were surprising.

    Now there is one mode. Everyone lands on the safe one, including anyone who had opted
    a series into fetching its missing episodes: an upgrade is the wrong moment to start
    hundreds of downloads, and turning full sync on is one setting away.
    """
    retention = dict(document.get('retention') or {})
    retention['monitoring'] = 'unmonitor-only'
    rules = []
    for rule in document.get('rules') or []:
        rule = {key: value for key, value in rule.items() if key != 'monitor_missing'}
        rule['monitoring'] = ''          # inherit
        rules.append(rule)
    return {'retention': retention, 'rules': rules}


def _to_v4(document: dict) -> dict:
    """Sonarr took over the filesystem, and most of the settings went with it.

    Path mappings, the sidecar list, empty-folder cleanup and the plugin's own recycle
    folder all described work Sonarr already does. The deletion guards existed mainly to
    contain a bad path mapping, which can no longer happen. Stored series paths are left
    as they are: the next match run replaces them with Sonarr's own, because it resolves
    by series id first.
    """
    changes = {}
    retention = dict(document.get('retention') or {})
    guards = document.get('guards') or {}
    fallback = guards.get('allow_import_date_fallback')
    retention['allow_estimated_dates'] = (fallback.get('enabled', True)
                                          if isinstance(fallback, dict) else True)
    # Monitoring episodes that have no file starts downloads, so it becomes a per-series
    # decision rather than a global default, and nobody is opted in by an upgrade.
    was_auto_monitor = bool(retention.pop('auto_monitor', False))
    retention.pop('auto_unmonitor', None)
    changes['retention'] = retention
    changes['rules'] = [dict(rule, monitor_missing=bool(rule.get('auto_monitor', was_auto_monitor) is True))
                        for rule in document.get('rules') or []]
    changes['instances'] = [{key: value for key, value in instance.items() if key != 'roots'}
                            for instance in document.get('instances') or []]
    for gone in ('guards', 'sidecars', 'delete_empty_dirs', 'recycle'):
        document.pop(gone, None)
    return changes


def _to_v3(document: dict) -> dict:
    """Preview became Test Mode, which governs the schedule rather than everything.

    The intent survives — a fresh install still cannot delete unattended — but it now
    means "the schedule runs without making changes" rather than "no action does
    anything", which was the source of a button that looked live and was not.
    """
    schedule = dict(document.get('schedule') or {})
    schedule['test_mode'] = bool(document.pop('preview', True))
    return {'schedule': schedule}


def _to_v2(document: dict) -> dict:
    # Dry run became a mode of its own before becoming Test Mode in v3.
    document['preview'] = bool(document.pop('dry_run', True))

    old_schedule = document.get('schedule') or {}
    document['schedule'] = schedule_from_cron(old_schedule.get('cron'), old_schedule.get('enabled', False))

    old_health = document.get('health') or {}
    # Captured before the health block is replaced; it lives in the notification matrix now.
    old_notify_ok = bool(old_health.get('notify_ok', False))
    document['health'] = {
        'series_match': schedule_from_cron(old_health.get('cron', '0 5 * * *'),
                                           old_health.get('enabled', True)),
        'connectivity_seconds': 300,
        'ttl_hours': int(old_health.get('ttl_hours', 24) or 24),
    }

    # Path mappings become roots that can be disabled and remembered rather than deleted.
    for instance in document.get('instances') or []:
        if not isinstance(instance, dict):
            continue
        roots = []
        for entry in instance.pop('path_maps', None) or []:
            if isinstance(entry, dict) and entry.get('from'):
                roots.append({'sonarr_path': entry['from'], 'unraid_path': entry.get('to', ''),
                              'enabled': True})
        instance.setdefault('roots', roots)
        instance.setdefault('verified_at', '')

    guards = document.get('guards') or {}
    document['guards'] = {
        'max_deletes_per_run': _guard(guards.get('max_deletes_per_run'), 200),
        'max_percent_per_rule': _guard(guards.get('max_percent_per_rule'), 50),
        'min_file_age_hours': _guard(guards.get('min_file_age_hours'), 6),
    }

    retention = dict(document.get('retention') or {})
    # Specials are one decision now: counted in monitoring exactly when they are kept.
    retention.pop('monitor_specials', None)
    retention['auto_unmonitor'] = bool(retention.pop('unmonitor_deleted', True))
    retention['auto_monitor'] = bool(retention.pop('remonitor_widened', False))
    document['retention'] = retention

    tmdb = document.get('tmdb') or {}
    # An API key is the switch: nobody enters one they do not want used.
    document['tmdb'] = {'api_key': tmdb.get('api_key', '')}

    notify = bool(document.pop('notify', True))
    document['notifications'] = {
        'run_started': False,
        'run_completed': notify,
        'series_removed': notify,
        'series_ended': notify,
        'health_ok': old_notify_ok,
        'health_problems': notify,
        'errors': notify,
    }
    document.get('health', {}).pop('notify_ok', None)

    document['logging'] = {'level': 'warning', 'max_bytes': 2 * 1024 * 1024}
    # The whole-show deletion gate is replaced by a typed confirmation in the dialog.
    document.pop('allow_series_deletion', None)
    return document
