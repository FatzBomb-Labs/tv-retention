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
                   load_health, load_settings, load_state, log_line, now_iso, read_cache,
                   read_journal, read_log, read_progress, save_settings, save_state,
                   trim_health, write_cache)
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
    # A page opening on a reading older than the interval syncs first, so "it is probably
    # up to date" is true rather than hopeful.
    if main.array_ready() and main.sync_is_due(settings):
        with contextlib.suppress(Rejected, SonarrError):
            with main.run_lock():
                main.sync_from_sonarr(settings, reason='opened stale')
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
        'jobs': job_state(settings),
        'plan': plan_summary(settings, load_health(settings)),
        'sync': main.last_sync(settings),
        'test_mode': bool((settings.get('schedule') or {}).get('test_mode', True)),
    }


def action_sync(settings, request):
    """Read Sonarr now, because someone asked. The only unbounded wait in the interface."""
    main.require_ready()
    with main.run_lock():
        report = main.sync_from_sonarr(settings, reason='asked for')
    health = load_health(settings)
    return {'report': report, 'health': trim_health(health), 'alerts': health.get('alerts') or [],
            'plan': plan_summary(settings, health), 'sync': main.last_sync(settings),
            'settings': redact(load_settings())}


def action_watch(settings, request):
    """The open page's heartbeat. It never goes to Sonarr.

    Time alone moves a keep window, so the plans are re-decided from the stored reading
    and the page follows. Anything that needs Sonarr waits for the daily sync, or for
    someone to press the button.
    """
    if not main.array_ready():
        return {'array_ready': False, 'stale_rules': []}
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
            'sync': main.last_sync(settings),
            'plan': plan_summary(settings, health)}


def action_scope_pass(settings, request):
    """Apply one or both one-time monitoring passes for a rule, now.

    Asked for on a save, and applied then rather than queued: a run already unmonitors
    what falls outside the window, so a queued version of that half would arrive after the
    downloads it exists to prevent.
    """
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if not rule:
        raise Rejected('That series is no longer here.')
    if rule.get('match_status') != 'matched':
        raise Rejected('This series is not matched to Sonarr, so its monitoring cannot be set.')
    return main.scope_pass(settings, rule,
                           monitor_new=bool(request.get('monitor_new')),
                           unmonitor_outside=bool(request.get('unmonitor_outside')),
                           previous_scope=request.get('previous_scope') or None)


def action_stats(settings, request):
    """Totals, from what is already kept.

    The run journal answers what has been reclaimed and by which series; the stored
    reading answers the shape of the library. Nothing is recorded specially for this, so
    the numbers cannot disagree with the history they come from.
    """
    runs = {'count': 0, 'deleted': 0, 'freed_bytes': 0, 'first': None, 'last': None}
    months, series = {}, {}
    for record in read_journal(settings):
        if record.get('preview') or record.get('dry_run'):
            continue          # a test run reclaimed nothing, and should not say it did
        runs['count'] += 1
        runs['deleted'] += int(record.get('deleted') or 0)
        runs['freed_bytes'] += int(record.get('freed_bytes') or 0)
        started = str(record.get('started') or '')
        if started:
            runs['first'] = min(runs['first'] or started, started)
            runs['last'] = max(runs['last'] or started, started)
            month = started[:7]
            months[month] = months.get(month, 0) + int(record.get('freed_bytes') or 0)
        for rule in record.get('rules') or []:
            deleted = len(rule.get('deleted') or []) if isinstance(rule.get('deleted'), list) \
                else int(rule.get('deleted') or 0)
            if not deleted:
                continue
            name = rule.get('series_title') or rule.get('path') or rule.get('rule_id')
            entry = series.setdefault(name, {'title': name, 'runs': 0, 'deleted': 0, 'freed_bytes': 0})
            entry['runs'] += 1
            entry['deleted'] += deleted
            entry['freed_bytes'] += int(rule.get('freed_bytes') or 0)

    catalogue, managed, managed_bytes, files, episodes, ended, largest = [], 0, 0, 0, 0, 0, None
    bound = {(rule.get('instance_id'), rule.get('series_id')) for rule in settings.get('rules', [])}
    for instance in settings.get('instances', []):
        entry = read_cache(settings, 'catalogue.json').get(instance['id']) or {}
        catalogue.extend(entry.get('series') or [])
    for entry in catalogue:
        files += int(entry.get('episode_file_count') or 0)
        episodes += int(entry.get('total_episode_count') or 0)
        ended += 1 if entry.get('ended') else 0
        size = int(entry.get('size_on_disk') or 0)
        if largest is None or size > largest['bytes']:
            largest = {'title': entry.get('title'), 'bytes': size}
        if (entry.get('instance_id'), entry.get('series_id')) in bound:
            managed += 1
            managed_bytes += size

    return {
        'runs': runs,
        'months': [{'month': month, 'freed_bytes': freed}
                   for month, freed in sorted(months.items())][-12:],
        'series': sorted(series.values(), key=lambda row: -row['freed_bytes'])[:40],
        'library': {'series': len(catalogue), 'managed': managed, 'managed_bytes': managed_bytes,
                    'bytes': sum(int(e.get('size_on_disk') or 0) for e in catalogue),
                    'files': files, 'episodes': episodes, 'ended': ended, 'largest': largest},
    }


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
    main.log_settings_change(updated, settings, updated)
    # A changed URL, key or mapping makes the cached series list wrong in a way no
    # timestamp would catch, so it is dropped rather than aged out.
    if canonical_json(settings.get('instances', [])) != canonical_json(updated.get('instances', [])):
        invalidate_catalogue(updated)
    # A rule that is gone must not leave its episodes behind; the store is keyed by rule.
    for gone in {rule['id'] for rule in settings.get('rules', [])} - {rule['id'] for rule in updated.get('rules', [])}:
        forget_episodes(updated, gone)
    return {'settings': redact(updated), 'schedule_active': CRON.exists(),
            'schedule_text': schedules.describe(updated.get('schedule') or {}),
            }






# What a list needs to draw a series. The overview is a paragraph and the per-season
# breakdown is an array, and three thousand of each turned a 1.8 MB store into a 4.3 MB
# reply — for two fields a list never shows. They stay on disk for whatever needs them.
LIST_FIELDS = ('instance_id', 'instance_name', 'series_id', 'title', 'sort_title', 'slug',
               'year', 'monitored', 'ended', 'status', 'episode_file_count', 'size_on_disk',
               'path', 'added', 'episode_count', 'total_episode_count', 'season_count',
               'next_airing', 'previous_airing', 'network', 'runtime', 'certification', 'poster')


def action_series(settings, request):
    """The series list behind the library, answered from the stored reading."""
    instance_id = str(request.get('instance_id') or '')
    catalogue = main.catalogue_for(settings, instance_id, force=bool(request.get('force')))
    used = {r['path'] for r in settings.get('rules', []) if r['id'] != str(request.get('except_rule') or '')}
    return {'series': [dict({key: entry.get(key) for key in LIST_FIELDS},
                            in_use=entry['path'] in used,
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
    if kind == 'rematch':
        report = main.bind_rules(settings, force=True)
        settings = load_settings()
        if rule:
            rule = next((r for r in settings.get('rules', []) if r['id'] == rule_id), rule)
            main.check_one_rule(settings, rule)
        return {'report': report, 'settings': redact(settings)}
    # No mode gates these. Test Mode governs the scheduler, and a fix asked for by hand is
    # always live — the check that stood here read a setting removed three versions ago,
    # defaulted to "preview is on", and refused every quick action ever since.
    if kind == 'remove-rule':
        if not rule:
            raise Rejected('That rule no longer exists.')
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
    'sync': action_sync,
    'scope-pass': action_scope_pass,
    'stats': action_stats,
    'settings': action_settings,
    'test-instance': action_test_instance,
    'series': action_series,
    'monitoring': action_monitoring,
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
        print(f'tv-retention rpc failure: {error!r}', file=sys.stderr)
        return {'ok': False, 'error': f'Unexpected backend error: {error}'}


