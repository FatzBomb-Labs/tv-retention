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
import datetime as dt
import hashlib
import os
import secrets
import sys
from pathlib import Path

import alerts
import backup
import main
import schedules
from core import (DEFAULTS, REMOVAL_ACTIONS, VERSION, Rejected, canonical_json,
                  describe_selectability, effective_rule, exclusion_summary, excluded_causes,
                  new_id, next_episode, normalise, redact, validate_conditions, validate_settings,
                  validate_text)
from sonarr import Sonarr, SonarrError
from store import (SCHEMA, age_seconds, episode_cache as store_episode_cache, forget_episodes, invalidate_catalogue, job_state,
                   load_health, load_settings, load_state, log_line, now_iso, read_cache,
                   read_journal, read_log, read_progress, save_settings, save_state,
                   trim_health, write_cache)
from tmdb import TMDB, TMDBError


def package_metadata(name, environment):
    override = os.environ.get(environment, '').strip()
    if override:
        return override
    # In the image metadata sits under /app; in a source checkout it sits at the
    # repository root. Missing metadata means an unpackaged development run, while any
    # other read failure is real and should surface.
    for parent in Path(__file__).resolve().parents[1:3]:
        try:
            return (parent / name).read_text().strip()
        except FileNotFoundError:
            continue
    return ''


def status_payload(settings, health=None):
    """Attach release metadata to the read-only operational status model."""
    status = main.status_snapshot(settings, health)
    status.update(build_number=package_metadata('BUILD', 'TVR_BUILD_NUMBER'),
                  build_date=package_metadata('BUILD_DATE', 'TVR_BUILD_DATE'))
    return status


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
    status, current_alerts, hidden_alerts = _status_bundle(settings, health)
    return {
        'version': VERSION,
        'build_number': package_metadata('BUILD', 'TVR_BUILD_NUMBER'),
        'build_date': package_metadata('BUILD_DATE', 'TVR_BUILD_DATE'),
        'settings': redact(settings),
        'runs': list(reversed(state.get('runs', []))),
        'last_run': state.get('last_run'),
        'schedule_active': bool((settings.get('schedule') or {}).get('enabled')),
        # Cached monitoring, rendered immediately. Every entry carries the moment it was
        # read, so nothing on screen pretends to be live.
        'health': trim_health(health),
        'health_stale': main.health_is_stale(settings, health),
        'stale_rules': main.stale_rule_ids(settings, health),
        'progress': read_progress(settings),
        'alerts': current_alerts,
        'suppressed_alerts': hidden_alerts,
        'alert_summary': alerts.summarise(current_alerts),
        'schedule_text': schedules.describe(settings.get('schedule') or {}),
        'jobs': job_state(settings),
        'plan': plan_summary(settings, load_health(settings)),
        'sync': main.last_sync(settings),
        'sync_due': main.sync_is_due(settings, main.PAGE_REFRESH_SECONDS),
        'test_mode': bool((settings.get('schedule') or {}).get('test_mode', True)),
        'status': status,
    }


def action_sync(settings, request):
    """Coalesce manual, page-open, visible-page, scheduled, and multi-tab refreshes."""
    reason = str(request.get('reason') or 'manual')
    force = bool(request.get('force'))
    page_refresh = reason in ('opened', 'visible')
    report = None
    try:
        with main.run_lock():
            # Re-check after winning the lock. Another tab may have completed the refresh
            # between this request being dispatched and this line.
            should_sync = force or main.sync_is_due(
                settings, main.PAGE_REFRESH_SECONDS if page_refresh else main.SYNC_FRESH_SECONDS)
            if should_sync:
                report = main.sync_from_sonarr(settings, reason=reason)
            # These paths also refresh reachability and recycle-bin state. This is
            # read-only and never invokes retention or changes monitored flags.
            health = main.refresh_instance_health(settings, force=False)
    except Rejected as error:
        if str(error) != 'A TV Retention run is already in progress.':
            raise
        health = load_health(settings)
        return _sync_response(settings, health, report=None, busy=True)
    return _sync_response(settings, health, report=report, busy=False)


def _sync_response(settings, health, report, busy: bool) -> dict:
    status, current_alerts, hidden_alerts = _status_bundle(settings, health)
    return {'report': report, 'busy': busy, 'health': trim_health(health),
            'alerts': current_alerts,
            'suppressed_alerts': hidden_alerts,
            'plan': plan_summary(settings, health), 'sync': main.last_sync(settings),
            'sync_due': main.sync_is_due(settings, main.PAGE_REFRESH_SECONDS),
            'settings': redact(load_settings()), 'status': status}


def action_watch(settings, request):
    """The open page's heartbeat. It never goes to Sonarr.

    Time alone moves a keep window, so the plans are re-decided from the stored reading
    and the page follows. Sonarr freshness is handled separately by the resident interval
    and quiet page-open/visibility refreshes.
    """
    health = load_health(settings)
    # Free, and the reason the page can call this every fifteen seconds: the plan is
    # arithmetic over episodes already in hand, and time alone can move a keep window.
    if main.recompute_plans(settings, health):
        write_cache(settings, 'health.json', health)
        health = load_health(settings)
    status, current_alerts, hidden_alerts = _status_bundle(settings, health)
    return {'progress': read_progress(settings),
            'health': trim_health(health),
            'alerts': current_alerts,
            'suppressed_alerts': hidden_alerts,
            'stale_rules': main.stale_rule_ids(settings, health),
            'sync': main.last_sync(settings),
            'sync_due': main.sync_is_due(settings, main.PAGE_REFRESH_SECONDS),
            'plan': plan_summary(settings, health), 'status': status}


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


def visible_alerts(settings, health):
    """Alerts as the interface should see them: suppressed facts omitted."""
    return alerts.annotate(health.get('alerts') or [], settings,
                           health.get('acknowledged') or {}, health.get('suppressed') or {})


def suppressed_alerts(health):
    return alerts.suppressed(health.get('alerts') or [], health.get('suppressed') or {})


def _status_bundle(settings, health):
    """Return the status model and one alert view that includes its read-only checks."""
    status = status_payload(settings, health)
    combined = dict(health, alerts=list(health.get('alerts') or []) + status.get('alerts', []))
    return status, visible_alerts(settings, combined), suppressed_alerts(health)


def action_acknowledge(settings, request):
    """Mark an alert seen, as it is now.

    Stored against a fingerprint of the alert rather than its key alone, so a change in
    what it says brings it back. An error can never be acknowledged: one of them stops a
    series from running, and hiding that would not stop it being true.
    """
    key = str(request.get('key') or '')
    health = load_health(settings)
    found = next((alert for alert in (health.get('alerts') or []) if alert['key'] == key), None)
    if not found:
        raise Rejected('That alert is no longer present.')
    acknowledged = dict(health.get('acknowledged') or {})
    if request.get('undo'):
        acknowledged.pop(key, None)
    else:
        if not alerts.may_acknowledge(found):
            raise Rejected('An error cannot be acknowledged while it is still true.')
        acknowledged[key] = alerts.fingerprint(found)
    health['acknowledged'] = acknowledged
    write_cache(settings, 'health.json', health)
    log_line(settings, 'info',
             f'{"un-" if request.get("undo") else ""}acknowledged: {found.get("title")}')
    status, current_alerts, hidden_alerts = _status_bundle(settings, health)
    return {'alerts': current_alerts,
            'suppressed_alerts': hidden_alerts,
            'summary': alerts.summarise(current_alerts), 'status': status}


def action_suppress_alert(settings, request):
    """Hide one recurring warning by exact alert key, never by global kind."""
    key = str(request.get('key') or '')
    health = load_health(settings)
    found = next((alert for alert in (health.get('alerts') or []) if alert['key'] == key), None)
    suppressed = dict(health.get('suppressed') or {})
    if request.get('undo'):
        suppressed.pop(key, None)
    else:
        if not found or found.get('kind') != 'no-recycle-bin':
            raise Rejected('That alert cannot be hidden permanently.')
        suppressed[key] = {'kind': found['kind'], 'instance_id': found.get('instance_id')}
    health['suppressed'] = suppressed
    write_cache(settings, 'health.json', health)
    log_line(settings, 'info',
             f'{"restored" if request.get("undo") else "suppressed"}: {key}')
    status, current_alerts, hidden_alerts = _status_bundle(settings, health)
    return {'alerts': current_alerts,
            'suppressed_alerts': hidden_alerts,
            'summary': alerts.summarise(current_alerts), 'status': status}


def rule_with_draft(rule, draft):
    """Apply and validate the editor's unsaved keep window before calculating with it."""
    candidate = dict(rule)
    candidate.update({key: draft[key] for key in ('profile_id', 'keep_days', 'keep_episodes',
                                                   'keep_seasons', 'combine', 'include_specials')
                      if key in draft})
    candidate.update(validate_conditions(candidate))
    return candidate


def series_episodes(settings, request):
    """The episodes behind a panel, with whatever the reading says Sonarr monitors.

    From the stored reading where there is one. A series being added has none yet, so its
    episodes are read on their own — the only time this reaches Sonarr.
    """
    draft = request.get('draft') or {}
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if rule:
        episodes, _, _, _ = main.episodes_for(settings, rule, offline=True)
    else:
        instance_id = str(request.get('instance_id') or '')
        series_id = int(request.get('series_id') or 0)
        if not instance_id or not series_id:
            raise Rejected('No series to read.')
        rule = {'id': '', 'instance_id': instance_id, 'series_id': series_id}
        episodes = main.client_for(settings, instance_id).episodes(series_id, files_only=False)
        main.interpolate_air_dates(episodes)
    active = effective_rule(rule_with_draft(rule, draft), settings.get('profiles'))
    return rule, episodes, main.keep_frame(episodes, active, settings)


def action_refresh_series(settings, request):
    """Re-read one series from Sonarr, and correct the stored catalogue with it.

    Everything the panel says about a series that has no rule comes from the catalogue,
    which only the daily sync refreshes as a whole. So this is what refresh does there:
    one series, eleven kilobytes against the catalogue's twelve megabytes, written back
    into the stored list so the page is not left fresher than the cache behind it.
    """
    instance_id = str(request.get('instance_id') or '')
    series_id = int(request.get('series_id') or 0)
    if not instance_id or not series_id:
        raise Rejected('No series to read.')
    series = main.client_for(settings, instance_id).series_one(series_id)
    cache = read_cache(settings, 'catalogue.json')
    entry = cache.get(instance_id) or {}
    # Only into a list this mapping produced. Writing one new-shaped series into an old
    # entry would leave the cache half in each shape, which is the failure SCHEMA exists
    # to prevent.
    if entry.get('schema') == SCHEMA and isinstance(entry.get('series'), list):
        entry['series'] = [series if other.get('series_id') == series_id else other
                           for other in entry['series']]
        cache[instance_id] = entry
        write_cache(settings, 'catalogue.json', cache)
    return {'series': series, 'read_at': now_iso()}


def action_episodes(settings, request):
    """Seasons and episodes, grouped, for the monitoring tree.

    Every episode is returned and each says whether it falls inside the keep window, so
    one call serves both the tree that offers the window and the one that offers the lot.
    """
    rule, episodes, frame = series_episodes(settings, request)
    inside = {episode.get('episode_id') for episode in frame['in_frame']}
    # Why each excluded episode is excluded, so the picker can show an automatic exclusion
    # as something to understand rather than something to untick. Only a manual one is a
    # box anybody may move here; the rest come from Automation and change there.
    causes = excluded_causes(episodes, rule, settings)
    seasons = {}
    for episode in sorted(episodes, key=lambda item: (item.get('season') or 0, item.get('episode') or 0)):
        reason, detail = causes.get(episode.get('episode_id'), (None, None))
        seasons.setdefault(episode.get('season') or 0, []).append({
            'episode_id': episode.get('episode_id'),
            'season': episode.get('season'),
            'episode': episode.get('episode'),
            'title': episode.get('title'),
            'air_date': episode.get('air_date'),
            'has_file': bool(episode.get('has_file')),
            'monitored': bool(episode.get('monitored')),
            'in_scope': episode.get('episode_id') in inside,
            'excluded': reason,
            'excluded_by': detail,
        })
    return {'seasons': [{'season': number, 'episodes': rows} for number, rows in sorted(seasons.items())]}


def action_exclusions(settings, request):
    """What is being done to one series without anybody asking for it, per series.

    Read from the stored reading, so opening the pane costs nothing and needs no network.
    Everything here is derivable from the settings and the episodes already held; it is
    gathered in one place because "why is this episode never deleted" should be answerable
    where the series is, not by opening Automation and doing the matching in your head.
    """
    rule, episodes, _ = series_episodes(settings, request)
    automation = settings.get('automation') or {}
    override = rule.get('include_specials')
    return {
        # Whether specials are *kept*, which is the question the pane asks. The setting is
        # phrased the other way round because it is an exclusion now.
        'specials': bool(override) if override is not None else not automation.get('exclude_specials'),
        'specials_default': override is None,
        'exclusions': exclusion_summary(episodes, rule, settings),
    }



def _episode_ids(value, field: str) -> list:
    """A list of Sonarr episode ids from the request, or a clear rejection.

    `int()` on a malformed entry raised uncaught before this, so a bad payload — a
    non-numeric string, `null` mixed into the list, the field sent as something other
    than a list — surfaced as "Unexpected backend error" instead of saying what was
    actually wrong with the request.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise Rejected(f'{field} must be a list of episode ids.')
    try:
        return [int(item) for item in value]
    except (TypeError, ValueError):
        raise Rejected(f'{field} must be a list of episode ids.') from None


def action_set_monitored(settings, request):
    """Set exactly the monitored flags the tree was left showing.

    Only the difference is sent to Sonarr, and the stored reading is corrected to match, so
    the plan and the counts agree with what was just done rather than waiting for a sync to
    find out.
    """
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if not rule:
        raise Rejected('That series is no longer here.')
    monitor = _episode_ids(request.get('monitor'), 'monitor')
    unmonitor = _episode_ids(request.get('unmonitor'), 'unmonitor')
    if not monitor and not unmonitor:
        return {'monitored': 0, 'unmonitored': 0}
    client = main.client_for(settings, rule['instance_id'])
    if monitor:
        client.set_monitored(monitor, True)
    if unmonitor:
        client.set_monitored(unmonitor, False)

    episodes, series, _ = store_episode_cache(settings, rule)
    if episodes is not None:
        wanted = {episode_id: True for episode_id in monitor}
        wanted.update({episode_id: False for episode_id in unmonitor})
        for episode in episodes:
            if episode.get('episode_id') in wanted:
                episode['monitored'] = wanted[episode['episode_id']]
        main.store_episodes(settings, rule, episodes, series)
    with contextlib.suppress(Rejected, SonarrError):
        main.check_one_rule(settings, rule)
    log_line(settings, 'info',
             f'{rule.get("series_title") or rule["path"]}: monitored {len(monitor)}, '
             f'unmonitored {len(unmonitor)} by hand')
    return {'monitored': len(monitor), 'unmonitored': len(unmonitor)}


def action_scope_counts(settings, request):
    """What the one-time passes would touch, without touching anything.

    Answered against the draft in the editor rather than the saved rule, so the numbers
    move as the keep window does. From the stored reading for a series that has one; a
    series being added has no reading yet, so its episodes are read on their own — about
    thirty milliseconds, and only because a panel was opened.
    """
    draft = request.get('draft') or {}
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    saved = rule
    if rule:
        try:
            episodes, _, _, _ = main.episodes_for(settings, rule, offline=True)
        except Rejected:
            return {'known': False}
    else:
        instance_id = str(request.get('instance_id') or '')
        series_id = int(request.get('series_id') or 0)
        if not instance_id or not series_id:
            raise Rejected('No series to count against.')
        rule = {'id': '', 'instance_id': instance_id, 'series_id': series_id}
        try:
            episodes = main.client_for(settings, instance_id).episodes(series_id, files_only=False)
        except Rejected as error:
            return {'known': False, 'error': str(error)}
        main.interpolate_air_dates(episodes)

    # Only what the draft actually carries: a key it leaves out keeps the rule's own value
    # rather than being overridden with nothing, which would count against no window at all.
    active = effective_rule(rule_with_draft(rule, draft), settings.get('profiles'))
    frame = main.keep_frame(episodes, active, settings)
    inside, outside = frame['in_frame'], frame['out_frame']
    unmonitored_inside = [episode for episode in inside if not episode.get('monitored')]
    monitored_outside = [episode for episode in outside if episode.get('monitored')]
    previous = draft.get('previous_scope') or None
    newly = main.newly_scoped_rows(settings, dict(rule, **active), episodes, previous) \
        if previous else unmonitored_inside
    return {
        'known': True,
        'in_scope': len(inside),
        'in_scope_unmonitored': len(unmonitored_inside),
        'would_monitor': len(newly),
        'out_scope': len(outside),
        'out_scope_monitored': len(monitored_outside),
        # What the panel's header says about the series itself. Free here, and a call of
        # its own anywhere else.
        'episodes': len(episodes),
        'episodes_monitored': sum(1 for episode in episodes if episode.get('monitored')),
        'episodes_on_disk': sum(1 for episode in episodes if episode.get('has_file')),
        'next_episode': next_episode(episodes),
        # What the next run would do to the *draft*, so the panel's next-run lines move as
        # the keep window is typed rather than describing the rule as it was last saved.
        # The same decision the run makes, from the same stored episodes: nothing here
        # touches Sonarr, and nothing is written.
        'plan': draft_plan(settings, saved, draft),
    }


def draft_plan(settings: dict, rule, draft: dict):
    """The saved rule's plan, re-decided with the editor's values in place.

    None for a series being added: it has no rule yet, so there is no next run to describe.
    """
    if not rule:
        return None
    state = main.monitoring_for(settings, rule_with_draft(rule, draft), offline=True)
    return state.get('plan') if state.get('ok') else None


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
    status, current_alerts, hidden_alerts = _status_bundle(settings, fresh)
    return {'busy': False, 'rule_id': rule['id'], 'state': summary,
            'alerts': current_alerts,
            'suppressed_alerts': hidden_alerts,
            'summary': alerts.summarise(current_alerts), 'status': status}


def stamp_reenable_watermarks(previous, updated) -> None:
    """Record the newest air date a rule knows about, the moment it is armed.

    Taken at arming rather than at disabling: arming is when somebody says "tell me if
    this changes", so it is the reading the change should be measured against. Cleared
    when the option goes away, so re-arming later takes a fresh one rather than reviving a
    mark from a situation nobody is in any more.
    """
    was = {rule['id']: rule.get('auto_reenable') for rule in previous.get('rules') or []}
    for rule in updated.get('rules') or []:
        if not rule.get('auto_reenable'):
            rule['auto_reenable_after'] = ''
        elif not was.get(rule['id']) or not rule.get('auto_reenable_after'):
            rule['auto_reenable_after'] = main.latest_air_date(updated, rule)


def action_settings(settings, request):
    draft = request.get('settings') or {}
    updated = validate_settings(draft, previous=settings)
    current_rules = {rule['id']: rule for rule in settings.get('rules') or []}
    for rule in updated.get('rules') or []:
        removal = (rule.get('queue') or {}).get('removal')
        if not removal:
            continue
        current = ((current_rules.get(rule['id'], {}).get('queue') or {}).get('removal') or {})
        # An existing request ID is an echo of current state, never permission to
        # restore canceled work. A newly queued action omits it and receives a new ID.
        submitted = next((r for r in draft.get('rules') or [] if r.get('id') == rule['id']), {})
        supplied_id = ((submitted.get('queue') or {}).get('removal') or {}).get('request_id')
        if supplied_id and (supplied_id != current.get('request_id')
                            or removal['action'] != current.get('action')):
            raise Rejected('Saved removal was changed or canceled; reload before saving.')
    stamp_reenable_watermarks(settings, updated)
    save_settings(updated)
    main.log_settings_change(updated, settings, updated)
    # A changed URL, key or mapping makes the cached series list wrong in a way no
    # timestamp would catch, so it is dropped rather than aged out.
    if canonical_json(settings.get('instances', [])) != canonical_json(updated.get('instances', [])):
        invalidate_catalogue(updated)
        before = {item['id']: item for item in settings.get('instances', [])}
        changed = [item['id'] for item in updated.get('instances', [])
                   if canonical_json(before.get(item['id'])) != canonical_json(item)]
        main.refresh_instance_health(updated, changed, force=True)
    # A rule that is gone must not leave its episodes behind; the store is keyed by rule.
    for gone in {rule['id'] for rule in settings.get('rules', [])} - {rule['id'] for rule in updated.get('rules', [])}:
        forget_episodes(updated, gone)
    health = load_health(updated)
    status, current_alerts, hidden_alerts = _status_bundle(updated, health)
    return {'settings': redact(updated),
            'schedule_active': bool((updated.get('schedule') or {}).get('enabled')),
            'schedule_text': schedules.describe(updated.get('schedule') or {}),
            'health': trim_health(health),
            'alerts': current_alerts,
            'suppressed_alerts': hidden_alerts,
            'status': status,
            }


def _api_key_metadata(settings):
    """Return lifecycle state without exposing the stored digest."""
    value = dict(settings.get('api_key') or {})
    value.pop('hash', None)
    return value


def _new_api_key(settings, reason='created'):
    value = secrets.token_urlsafe(32)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    metadata = {'status': 'created',
                'hash': hashlib.sha256(value.encode('utf-8')).hexdigest(),
                'prefix': value[:8], 'created_at': stamp, 'revoked_at': ''}
    settings['api_key'] = metadata
    save_settings(settings)
    log_line(settings, 'info', f'API key {reason}')
    return value, metadata


def action_api_key(settings, request):
    """Create, regenerate, revoke, or inspect the future API credential."""
    operation = str(request.get('operation') or 'status').lower()
    if operation == 'status':
        return {'api_key': _api_key_metadata(settings)}
    if operation in ('create', 'regenerate'):
        if operation == 'create' and (settings.get('api_key') or {}).get('status') == 'created':
            raise Rejected('An API key already exists. Regenerate it to replace it.')
        value, metadata = _new_api_key(settings, operation)
        return {'api_key': _api_key_metadata({'api_key': metadata}), 'key': value,
                'shown_once': True}
    if operation == 'revoke':
        current = dict(settings.get('api_key') or {})
        if current.get('status') != 'created':
            raise Rejected('There is no active API key to revoke.')
        current.update(status='revoked', revoked_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'))
        settings['api_key'] = current
        save_settings(settings)
        log_line(settings, 'warning', 'API key revoked')
        return {'api_key': _api_key_metadata(settings)}
    raise Rejected('Unknown API-key operation.')


def action_test_connection(settings, request):
    """Test one optional enrichment provider without persisting its secret."""
    kind = str(request.get('kind') or '').lower()
    if kind not in main.OPTIONAL_PROVIDER_KINDS:
        raise Rejected('Unknown optional connection.')
    connection = dict((settings.get('connections') or {}).get(kind) or {})
    incoming = request.get('connection') or {}
    if isinstance(incoming, dict):
        connection.update(incoming)
    credential_name = 'token' if kind == 'plex' else 'api_key'
    if connection.get(credential_name) in ('********', '••••••••'):
        stored = ((settings.get('connections') or {}).get(kind) or {}).get(credential_name, '')
        if not stored and kind == 'tmdb':
            stored = ((settings.get('tmdb') or {}).get('api_key') or '')
        connection[credential_name] = stored
    if kind == 'tmdb':
        key = str(connection.get('api_key') or '')
        if not key:
            raise Rejected('Enter a TMDB API key first.')
        TMDB(key).check()
        return {'ok_message': 'TMDB accepted the key.'}
    provider = main.optional_provider(kind, connection, settings)
    if provider is None:
        raise Rejected(f'{kind.title()} connection is not configured.')
    provider.check()
    return {'ok_message': f'{kind.title()} connection answered.'}


def action_backup(settings, request):
    def remember_error(message):
        # The archive operation is the useful result; a failure to record its diagnostic
        # must not replace it with a second, less actionable error (for example when a
        # hand-edited settings file becomes read-only at the same time as the destination).
        with contextlib.suppress(Exception):
            updated = load_settings()
            updated.setdefault('backup', {})['last_error'] = str(message)[:500]
            save_settings(updated)

    operation = str(request.get('operation') or 'list').lower()
    if operation == 'list':
        configured = (settings.get('backup') or {}).get('path')
        backups = backup.list_backups(settings) if str(configured or '').strip() else []
        return {'backups': backups,
                'backup': dict(settings.get('backup') or {})}
    if operation == 'create':
        try:
            result = backup.create(settings)
        except Rejected as error:
            remember_error(error)
            raise
        updated = load_settings()
        updated.setdefault('backup', {}).update(last=result['created_at'], last_error='')
        save_settings(updated)
        return {'result': result, 'backups': backup.list_backups(updated)}
    if operation == 'restore':
        try:
            result = backup.restore(settings, request.get('file'), request.get('confirm', ''))
        except Rejected as error:
            remember_error(error)
            raise
        updated = load_settings()
        updated.setdefault('backup', {}).update(last_error='')
        save_settings(updated)
        return {'result': result}
    raise Rejected('Unknown backup operation.')






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
    # By the binding a rule actually holds, and per instance: a bare set of folders said
    # "already used" about a series a different Sonarr owns, and said nothing about a
    # series whose folder had moved since the rule was written.
    except_rule = str(request.get('except_rule') or '')
    used = {(r['instance_id'], r['series_id']) for r in settings.get('rules', [])
            if r['id'] != except_rule and r.get('series_id')}
    taken = lambda entry: (entry['instance_id'], entry['series_id']) in used
    return {'series': [dict({key: entry.get(key) for key in LIST_FIELDS},
                            in_use=taken(entry),
                            **describe_selectability(entry, taken(entry)))
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




def action_log(settings, request):
    """A slice of the rolling log, addressed by byte offset so polling stays cheap."""
    offset = _whole_or(request.get('offset'), 0)
    return dict(read_log(settings, offset=offset), level=(settings.get('logging') or {}).get('level'))


def action_alerts(settings, request):
    """Everything currently wrong, split the way the interface shows it."""
    health = load_health(settings)
    status, current, hidden = _status_bundle(settings, health)
    return {
        'alerts': current,
        'suppressed_alerts': hidden,
        'summary': alerts.summarise(current),
        'system': [alert for alert in current if alert.get('scope') == 'system'],
        'series_summary': {rule['id']: alerts.summarise(alerts.for_series(current, rule['id']))
                           for rule in settings.get('rules', [])},
        'status': status,
    }


def action_status(settings, request):
    """Return the read-only operational status page model."""
    health = load_health(settings)
    status, current, _ = _status_bundle(settings, health)
    status['alerts'] = current
    return status


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
    media = client.media_management()
    media['recycleBin'] = path
    if not media.get('recycleBinCleanupDays'):
        media['recycleBinCleanupDays'] = 7
    client.set_media_management(media)
    log_line(settings, 'warning', f'{instance["name"]}: recycle bin set to {path}')
    return {'recycle_bin': path, 'ok_message': f'Sonarr will now move deleted files to {path}.'}


def action_match(settings, request):
    return {'report': main.bind_rules(settings), 'settings': redact(load_settings())}


def action_preview(settings, request):
    ids = request.get('rule_ids') or None
    return {'result': main.run(preview=True, rule_ids=ids)}


def action_run(settings, request):
    with main.run_lock():
        return {'result': main.run(preview=False, rule_ids=request.get('rule_ids') or None)}


def action_test_tmdb(settings, request):
    key = validate_text((request.get('tmdb') or {}).get('api_key'), 'TMDB API key', 128)
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
    'progress': action_progress,
    'log': action_log,
    'alerts': action_alerts,
    'status': action_status,
    'enable-recycle-bin': action_enable_recycle_bin,
    'check-rule': action_check_rule,
    'watch': action_watch,
    'sync': action_sync,
    'scope-pass': action_scope_pass,
    'stats': action_stats,
    'acknowledge': action_acknowledge,
    'suppress-alert': action_suppress_alert,
    'scope-counts': action_scope_counts,
    'refresh-series': action_refresh_series,
    'episodes': action_episodes,
    'exclusions': action_exclusions,
    'set-monitored': action_set_monitored,
    'settings': action_settings,
    'test-instance': action_test_instance,
    'series': action_series,
    'match': action_match,
    'preview': action_preview,
    'run': action_run,
    'test-tmdb': action_test_tmdb,
    'test-connection': action_test_connection,
    'api-key': action_api_key,
    'backup': action_backup,
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
