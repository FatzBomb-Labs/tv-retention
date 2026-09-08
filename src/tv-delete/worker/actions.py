#!/usr/bin/env python3
"""The RPC surface: one function per thing the interface can ask for.

Every handler takes the validated settings and the request, and returns a plain dict. The
work itself lives in main; this module decides what the page is allowed to ask, what comes
back, and nothing else. Keeping it separate means the twenty entry points can be read as a
list rather than found among the machinery they drive.

main imports this lazily, from its dispatch, so the dependency runs one way only.
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import alerts
import main
import schedules
from core import (DEFAULTS, REMOVAL_ACTIONS, VERSION, Rejected, canonical_json,
                  describe_selectability, effective_rule, new_id, normalise, redact,
                  validate_settings)
from sonarr import Sonarr, SonarrError
from store import (CRON, age_seconds, forget_episodes, invalidate_catalogue, job_state,
                   load_health, load_settings, load_state, log_line, now_iso, read_log,
                   read_progress, save_settings, save_state, trim_health, write_cache)
from tmdb import TMDB, TMDBError

MAX_BROWSE_ENTRIES = 500

# ---------------------------------------------------------------------------
# RPC actions
# ---------------------------------------------------------------------------

def plan_summary(settings: dict, health: dict) -> dict:
    """What the next run would do, totalled from the cached per-series plans.

    Reported with the age of the oldest reading and with how many series have no reading
    at all, because "nothing to do" is only as trustworthy as the checks behind it. The
    interface must not hide the Run button on an answer it cannot vouch for.
    """
    totals = {'delete': 0, 'delete_bytes': 0, 'unmonitor': 0, 'monitor': 0, 'remove': 0,
              'newly_scoped': 0, 'series': 0, 'unknown': 0, 'blocked': 0, 'oldest': None,
              'removals_by_action': {}}
    alerts_now = health.get('alerts') or []
    blocking = {alert['rule_id'] for alert in alerts_now if alert.get('blocking') and alert.get('rule_id')}
    for rule in settings.get('rules', []):
        # A queued removal counts wherever the rule stands: it is a change the run makes.
        removal = (rule.get('queue') or {}).get('removal')
        if removal:
            # Counted by what it asks Sonarr to do, not merely that something happens.
            totals['remove'] += 1
            totals.setdefault('removals_by_action', {})
            totals['removals_by_action'][removal['action']] = \
                totals['removals_by_action'].get(removal['action'], 0) + 1
            totals['series'] += 1
            continue
        if not rule.get('enabled'):
            continue
        if rule['id'] in blocking:
            totals['blocked'] += 1
            continue
        entry = (health.get('rules') or {}).get(rule['id']) or {}
        plan = entry.get('plan')
        if not plan:
            totals['unknown'] += 1
            continue
        if plan['delete'] or plan['unmonitor'] or plan['monitor']:
            totals['series'] += 1
        for key in ('delete', 'delete_bytes', 'unmonitor', 'monitor', 'newly_scoped'):
            totals[key] += int(plan.get(key) or 0)
        # The age of the reading, not of the arithmetic over it: the plan is recomputed
        # every time it is asked for, and dating it "now" would hide how old the data is.
        stamp = entry.get('read_at') or entry.get('checked_at')
        if stamp and (totals['oldest'] is None or stamp < totals['oldest']):
            totals['oldest'] = stamp
    totals['actionable'] = (totals['delete'] + totals['unmonitor'] + totals['monitor']
                            + totals['remove'])
    # Only a complete, unblocked picture may be called up to date.
    totals['trustworthy'] = totals['unknown'] == 0
    return totals



def action_snapshot(settings, request):
    state = load_state(settings)
    health = load_health(settings)
    # One small question — "what changed?" — in place of re-reading every series to find
    # out. Rate limited, and it only ever adds to what must be re-read.
    requested = request.get('watch_seconds')
    if main.array_ready() and main.watch_sonarr(settings, health,
                                      min_interval=30 if requested is None else int(requested)):
        write_cache(settings, 'health.json', health)
        health = load_health(settings)
    return {
        'version': VERSION,
        'settings': redact(settings),
        'array_ready': main.array_ready(),
        'runs': list(reversed(state.get('runs', []))),
        'last_run': state.get('last_run'),
        'schedule_active': CRON.exists(),
        # Cached monitoring, rendered immediately. Every entry carries the moment it was
        # read, so nothing on screen pretends to be live.
        'health': trim_health(health),
        'health_stale': main.health_is_stale(settings, health),
        'stale_rules': main.stale_rule_ids(settings, health),
        'progress': read_progress(settings),
        'alerts': (load_health(settings).get('alerts') or []),
        'alert_summary': alerts.summarise(load_health(settings).get('alerts')),
        'schedule_text': schedules.describe(settings.get('schedule') or {}),
        'series_match_text': schedules.describe((settings.get('health') or {}).get('series_match') or {}),
        'jobs': job_state(settings),
        'plan': plan_summary(settings, load_health(settings)),
        'test_mode': bool((settings.get('schedule') or {}).get('test_mode', True)),
    }


def action_watch(settings, request):
    """The open page's heartbeat, and nothing more.

    It asks Sonarr what changed and marks what that affects. It deliberately does not
    re-read those series here: the page has a queue for that which shows each card being
    read, and a heartbeat that blocks for several seconds is not a heartbeat.
    """
    if not main.array_ready():
        return {'array_ready': False, 'stale_rules': []}
    health = load_health(settings)
    # `or` would turn an explicit zero into the default, and zero is the one value a
    # caller passes when it means "ask now".
    requested = request.get('min_interval')
    if main.watch_sonarr(settings, health, min_interval=15 if requested is None else int(requested)):
        write_cache(settings, 'health.json', health)
        health = load_health(settings)
    # Free, and the reason the page can call this every fifteen seconds: the plan is
    # arithmetic over episodes already in hand, and time alone can move a keep window.
    if main.recompute_plans(settings, health):
        write_cache(settings, 'health.json', health)
        health = load_health(settings)
    return {'array_ready': True,
            'progress': read_progress(settings),
            'health': trim_health(health),
            'alerts': health.get('alerts') or [],
            'stale_rules': main.stale_rule_ids(settings, health),
            'plan': plan_summary(settings, health)}


def action_health(settings, request):
    """Refresh the cached health, on demand or because the page found it stale."""
    return {'health': trim_health(main.run_health_check(scheduled=False, force=bool(request.get('force'))))}


def action_progress(settings, request):
    """Cheap poll: what a running check is doing, plus the current cached results."""
    return {'progress': read_progress(settings), 'health': trim_health(load_health(settings))}


def action_check_rule(settings, request):
    """Check one rule, so the interface can update show by show instead of all at once."""
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if not rule:
        raise Rejected('That rule no longer exists.')
    progress = read_progress(settings)
    if progress.get('running'):
        # A sweep is already covering this rule; asking again would only duplicate its work.
        return {'busy': True, 'progress': progress}
    main.require_ready()
    health = load_health(settings)
    instance_state = (health.get('instances') or {}).get(rule['instance_id'])
    if not instance_state or age_seconds(instance_state.get('checked_at')) is None \
            or age_seconds(instance_state.get('checked_at')) > 900:
        instance = next((i for i in settings.get('instances', []) if i['id'] == rule['instance_id']), None)
        if instance:
            instance_state = main.check_instance(settings, instance, force=False)
            health = load_health(settings)
            health.setdefault('instances', {})[instance['id']] = instance_state
            write_cache(settings, 'health.json', health)
    main.bind_rules(settings)
    settings = load_settings()
    rule = next((r for r in settings.get('rules', []) if r['id'] == rule['id']), rule)
    state = main.check_one_rule(settings, rule, instance_state, force=bool(request.get('force')))
    summary = trim_health({'rules': {rule['id']: state}})['rules'][rule['id']]
    # The alerts come back with the check that produced them. Asking for them separately
    # meant a second PHP request and a second Python process for every series read.
    fresh = load_health(settings)
    return {'busy': False, 'rule_id': rule['id'], 'state': summary,
            'alerts': fresh.get('alerts') or [], 'summary': alerts.summarise(fresh.get('alerts'))}


def action_settings(settings, request):
    updated = validate_settings(request.get('settings') or {}, previous=settings)
    save_settings(updated)
    main.write_cron(updated)
    # A changed URL, key or mapping makes the cached series list wrong in a way no
    # timestamp would catch, so it is dropped rather than aged out.
    if canonical_json(settings.get('instances', [])) != canonical_json(updated.get('instances', [])):
        invalidate_catalogue(updated)
    # A rule that is gone must not leave its episodes behind; the store is keyed by rule.
    for gone in {rule['id'] for rule in settings.get('rules', [])} - {rule['id'] for rule in updated.get('rules', [])}:
        forget_episodes(updated, gone)
    return {'settings': redact(updated), 'schedule_active': CRON.exists(),
            'schedule_text': schedules.describe(updated.get('schedule') or {}),
            'series_match_text': schedules.describe((updated.get('health') or {}).get('series_match') or {})}






def action_series(settings, request):
    """The series list behind the picker, answered entirely from Sonarr."""
    instance_id = str(request.get('instance_id') or '')
    catalogue = main.catalogue_for(settings, instance_id, force=bool(request.get('force')))
    used = {r['path'] for r in settings.get('rules', []) if r['id'] != str(request.get('except_rule') or '')}
    return {'series': [dict(entry, in_use=entry['path'] in used,
                            **describe_selectability(entry, entry['path'] in used))
                       for entry in catalogue]}


def action_test_instance(settings, request):
    """Confirm Sonarr answers, and report what it will do with deleted files."""
    from core import validate_instance
    existing = {i['id']: i.get('api_key', '') for i in settings.get('instances', [])}
    instance = validate_instance(request.get('instance') or {}, existing)
    client = Sonarr(instance)
    status = client.status()
    return {
        'sonarr_version': status.get('version'),
        'series_count': len(client.series()),
        'recycle_bin': main.check_recycle_bin(settings, instance),
    }




def action_monitoring(settings, request):
    """Status for the requested rules. The UI asks per card, or for all of them at once."""
    wanted = set(request.get('rule_ids') or [])
    rules = [r for r in settings.get('rules', []) if not wanted or r['id'] in wanted]
    return {'monitoring': [main.monitoring_for(settings, rule) for rule in rules]}


def action_monitor_apply(settings, request):
    """Bring one rule's monitoring into line with its keep frame.

    Two explicit, opposite corrections, never run on a schedule. "monitor-in-frame" arms
    the episodes the rule would keep; "unmonitor-out-frame" disarms the ones it would
    remove, so Sonarr stops fetching what the next run would delete again.
    """
    mode = str(request.get('mode') or '')
    if mode not in ('monitor-in-frame', 'unmonitor-out-frame'):
        raise Rejected('Unknown monitoring action')
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if not rule:
        raise Rejected('That rule no longer exists.')
    status = main.monitoring_for(settings, rule, force=True)
    if not status.get('ok'):
        raise Rejected(status.get('error') or 'This rule is not matched to a Sonarr series.')

    targets = status['in_frame_unmonitored'] if mode == 'monitor-in-frame' else status['out_frame_monitored']
    ids = [entry['episode_id'] for entry in targets if entry.get('episode_id')]
    if not ids:
        return {'changed': 0, 'monitoring': status,
                'ok_message': 'Nothing to change; monitoring already matches the keep frame.'}
    client = main.client_for(settings, rule['instance_id'])
    client.set_monitored(ids, mode == 'monitor-in-frame')

    verb = 'monitored' if mode == 'monitor-in-frame' else 'unmonitored'
    return {'changed': len(ids), 'monitoring': main.monitoring_for(settings, rule, force=True),
            'ok_message': f'{len(ids)} episode(s) {verb} in Sonarr.'}




def action_log(settings, request):
    """A slice of the rolling log, addressed by byte offset so polling stays cheap."""
    offset = _whole_or(request.get('offset'), 0)
    return dict(read_log(settings, offset=offset), level=(settings.get('logging') or {}).get('level'))


def action_alerts(settings, request):
    """Everything currently wrong, split the way the interface shows it."""
    health = load_health(settings)
    current = health.get('alerts') or []
    return {
        'alerts': current,
        'summary': alerts.summarise(current),
        'system': [alert for alert in current if alert.get('scope') == 'system'],
        'series_summary': {rule['id']: alerts.summarise(alerts.for_series(current, rule['id']))
                           for rule in settings.get('rules', [])},
    }


def action_alert_action(settings, request):
    """Carry out the fix an alert offers.

    Every one of these is refused under Preview, because a fix that silently did nothing
    would be worse than a fix that says it is not available.
    """
    kind = str(request.get('kind') or '')
    rule_id = str(request.get('rule_id') or '')
    rule = next((r for r in settings.get('rules', []) if r['id'] == rule_id), None)
    if kind in ('monitor-in-frame', 'unmonitor-out-frame'):
        return action_monitor_apply(settings, {'rule_id': rule_id, 'mode': kind})
    if kind == 'rematch':
        report = main.bind_rules(settings, force=True)
        settings = load_settings()
        if rule:
            rule = next((r for r in settings.get('rules', []) if r['id'] == rule_id), rule)
            main.check_one_rule(settings, rule)
        return {'report': report, 'settings': redact(settings)}
    if kind == 'accept-path':
        if not rule:
            raise Rejected('That rule no longer exists.')
        if settings.get('preview', True):
            raise Rejected('Preview mode is on, so nothing is changed.')
        main.bind_rules(settings, force=True)
        settings = load_settings()
        return {'settings': redact(settings)}
    if kind == 'remove-rule':
        if not rule:
            raise Rejected('That rule no longer exists.')
        if settings.get('preview', True):
            raise Rejected('Preview mode is on, so nothing is changed.')
        settings['rules'] = [r for r in settings.get('rules', []) if r['id'] != rule_id]
        save_settings(settings)
        health = load_health(settings)
        health['rules'].pop(rule_id, None)
        health['alerts'] = [a for a in (health.get('alerts') or []) if a.get('rule_id') != rule_id]
        write_cache(settings, 'health.json', health)
        log_line(settings, 'warning', f'rule removed for {rule.get("series_title") or rule["path"]}')
        return {'settings': redact(load_settings())}
    raise Rejected('That action is not available.')


def _whole_or(value, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def action_enable_recycle_bin(settings, request):
    """Set a recycle bin on a Sonarr instance, at the operator's explicit request.

    This changes Sonarr's own configuration, not the plugin's, so it affects everything
    Sonarr deletes. Never done automatically; the alert offers it and this applies it.
    """
    instance = next((i for i in settings.get('instances', []) if i['id'] == str(request.get('instance_id') or '')), None)
    if not instance:
        raise Rejected('That Sonarr instance no longer exists.')
    from core import validate_path
    path = validate_path(request.get('path'), 'Recycle bin path')
    client = Sonarr(instance)
    media = client._request('GET', 'config/mediamanagement') or {}
    media['recycleBin'] = path
    if not media.get('recycleBinCleanupDays'):
        media['recycleBinCleanupDays'] = 7
    client._request('PUT', f'config/mediamanagement/{media.get("id", 1)}', body=media)
    log_line(settings, 'warning', f'{instance["name"]}: recycle bin set to {path}')
    return {'recycle_bin': path, 'ok_message': f'Sonarr will now move deleted files to {path}.'}


def action_browse(settings, request):
    """Directory listing for the folder picker. Read-only, and confined to /mnt."""
    from core import validate_path
    path = validate_path(request.get('path') or '/mnt/user', 'Folder')
    if not (path == '/mnt' or path.startswith('/mnt/')):
        raise Rejected('Browsing is limited to /mnt.')
    directory = Path(path)
    if not directory.is_dir():
        raise Rejected(f'{path} is not a folder on this server.')
    entries = []
    try:
        for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith('.') or not child.is_dir():
                continue
            entries.append({'name': child.name, 'path': normalise(child)})
            if len(entries) >= MAX_BROWSE_ENTRIES:
                break
    except OSError as error:
        raise Rejected(f'Cannot read {path} ({error})')
    return {'path': normalise(directory), 'parent': normalise(directory.parent) if path != '/mnt' else None,
            'entries': entries}


def action_match(settings, request):
    return {'report': main.bind_rules(settings), 'settings': redact(load_settings())}


def action_preview(settings, request):
    ids = request.get('rule_ids') or None
    return {'result': main.run(preview=True, rule_ids=ids)}


def action_run(settings, request):
    with main.run_lock():
        return {'result': main.run(preview=False, rule_ids=request.get('rule_ids') or None)}


def action_test_tmdb(settings, request):
    from core import _text
    key = _text((request.get('tmdb') or {}).get('api_key'), 'TMDB API key', 128)
    if key in ('', '********'):
        key = (settings.get('tmdb') or {}).get('api_key', '')
    if not key:
        raise Rejected('Enter a TMDB API key first.')
    TMDB(key).check()
    return {'ok_message': 'TMDB accepted the key.'}


def action_clear_history(settings, request):
    state = load_state(settings)
    state['runs'] = []
    state['last_run'] = None
    save_state(settings, state)
    return {'cleared': True}


ACTIONS = {
    'snapshot': action_snapshot,
    'health': action_health,
    'progress': action_progress,
    'log': action_log,
    'alerts': action_alerts,
    'alert-action': action_alert_action,
    'enable-recycle-bin': action_enable_recycle_bin,
    'check-rule': action_check_rule,
    'watch': action_watch,
    'settings': action_settings,
    'test-instance': action_test_instance,
    'series': action_series,
    'monitoring': action_monitoring,
    'monitor-apply': action_monitor_apply,
    'browse': action_browse,
    'match': action_match,
    'preview': action_preview,
    'run': action_run,
    'test-tmdb': action_test_tmdb,
    'clear-history': action_clear_history,
}


def dispatch(request: dict) -> dict:
    action = request.get('action')
    handler = ACTIONS.get(action)
    if not handler:
        return {'ok': False, 'error': 'Unknown action'}
    try:
        settings = load_settings()
        return {'ok': True, **handler(settings, request)}
    except Rejected as error:
        return {'ok': False, 'error': str(error)}
    except Exception as error:  # noqa: BLE001 - the UI must never see a traceback
        print(f'tv-delete rpc failure: {error!r}', file=sys.stderr)
        return {'ok': False, 'error': f'Unexpected backend error: {error}'}


