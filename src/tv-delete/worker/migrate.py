#!/usr/bin/env python3
"""Bring a stored settings document up to the current shape.

Settings live on the flash device and outlive any release, so every change of shape needs
a path forward for what is already there. Migration runs on load, is idempotent, and never
discards a value it does not recognise: an unknown key is left in place rather than
dropped, so downgrading and re-upgrading cannot lose a setting.
"""
from __future__ import annotations

import re

SETTINGS_VERSION = 2

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

    # Dry run became Preview: one mode that makes every action a simulation, not a flag
    # that only gated deletion.
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
    document['settings_version'] = SETTINGS_VERSION
    return document
