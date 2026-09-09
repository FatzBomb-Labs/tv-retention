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
                  describe_selectability, effective_rule, new_id, next_episode, normalise,
                  redact, validate_settings)
from sonarr import Sonarr, SonarrError
from store import (SCHEMA, age_seconds, episode_cache as store_episode_cache, forget_episodes, invalidate_catalogue, job_state,
                   load_health, load_settings, load_state, log_line, now_iso, read_cache,
                   read_journal, read_log, read_progress, save_settings, save_state,
                   trim_health, write_cache)
from tmdb import TMDB, TMDBError


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
    if main.sync_is_due(settings):
        with contextlib.suppress(Rejected, SonarrError):
            with main.run_lock():
                main.sync_from_sonarr(settings, reason='opened stale')
        health = load_health(settings)
    return {
        'version': VERSION,
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
        'alerts': visible_alerts(settings, health),
        'alert_summary': alerts.summarise(visible_alerts(settings, health)),
        'schedule_text': schedules.describe(settings.get('schedule') or {}),
        'jobs': job_state(settings),
        'plan': plan_summary(settings, load_health(settings)),
        'sync': main.last_sync(settings),
        'test_mode': bool((settings.get('schedule') or {}).get('test_mode', True)),
    }


def action_sync(settings, request):
    """Read Sonarr now, because someone asked. The only unbounded wait in the interface."""
    with main.run_lock():
        report = main.sync_from_sonarr(settings, reason='asked for')
    health = load_health(settings)
    return {'report': report, 'health': trim_health(health), 'alerts': visible_alerts(settings, health),
            'plan': plan_summary(settings, health), 'sync': main.last_sync(settings),
            'settings': redact(load_settings())}


def action_watch(settings, request):
    """The open page's heartbeat. It never goes to Sonarr.

    Time alone moves a keep window, so the plans are re-decided from the stored reading
    and the page follows. Anything that needs Sonarr waits for the daily sync, or for
    someone to press the button.
    """
    health = load_health(settings)
    # Free, and the reason the page can call this every fifteen seconds: the plan is
    # arithmetic over episodes already in hand, and time alone can move a keep window.
    if main.recompute_plans(settings, health):
        write_cache(settings, 'health.json', health)
        health = load_health(settings)
    return {'progress': read_progress(settings),
            'health': trim_health(health),
            'alerts': visible_alerts(settings, health),
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


def visible_alerts(settings, health):
    """Alerts as the interface should see them: muted ones gone, acknowledged ones marked."""
    return alerts.annotate(health.get('alerts') or [], settings, health.get('acknowledged') or {})


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
        if not alerts.may_acknowledge(found, settings):
            raise Rejected('An error cannot be acknowledged while it is still true.')
        acknowledged[key] = alerts.fingerprint(found)
    health['acknowledged'] = acknowledged
    write_cache(settings, 'health.json', health)
    log_line(settings, 'info',
             f'{"un-" if request.get("undo") else ""}acknowledged: {found.get("title")}')
    return {'alerts': visible_alerts(settings, health),
            'summary': alerts.summarise(visible_alerts(settings, health))}


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
    overrides = {key: draft[key] for key in ('profile_id', 'keep_days', 'keep_episodes',
                                             'keep_seasons', 'combine', 'include_specials')
                 if key in draft}
    active = effective_rule(dict(rule, **overrides), settings.get('profiles'))
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
    _, episodes, frame = series_episodes(settings, request)
    inside = {episode.get('episode_id') for episode in frame['in_frame']}
    seasons = {}
    for episode in sorted(episodes, key=lambda item: (item.get('season') or 0, item.get('episode') or 0)):
        seasons.setdefault(episode.get('season') or 0, []).append({
            'episode_id': episode.get('episode_id'),
            'season': episode.get('season'),
            'episode': episode.get('episode'),
            'title': episode.get('title'),
            'air_date': episode.get('air_date'),
            'has_file': bool(episode.get('has_file')),
            'monitored': bool(episode.get('monitored')),
            'in_scope': episode.get('episode_id') in inside,
        })
    return {'seasons': [{'season': number, 'episodes': rows} for number, rows in sorted(seasons.items())]}


def action_set_monitored(settings, request):
    """Set exactly the monitored flags the tree was left showing.

    Only the difference is sent to Sonarr, and the stored reading is corrected to match, so
    the plan and the counts agree with what was just done rather than waiting for a sync to
    find out.
    """
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if not rule:
        raise Rejected('That series is no longer here.')
    monitor = [int(value) for value in (request.get('monitor') or [])]
    unmonitor = [int(value) for value in (request.get('unmonitor') or [])]
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
    overrides = {key: draft[key] for key in ('profile_id', 'keep_days', 'keep_episodes',
                                             'keep_seasons', 'combine', 'include_specials')
                 if key in draft}
    active = effective_rule(dict(rule, **overrides), settings.get('profiles'))
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
        'plan': draft_plan(settings, saved, overrides),
    }


def draft_plan(settings: dict, rule, overrides: dict):
    """The saved rule's plan, re-decided with the editor's values in place.

    None for a series being added: it has no rule yet, so there is no next run to describe.
    """
    if not rule:
        return None
    state = main.monitoring_for(settings, dict(rule, **overrides), offline=True)
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
    return {'busy': False, 'rule_id': rule['id'], 'state': summary,
            'alerts': visible_alerts(settings, fresh),
            'summary': alerts.summarise(visible_alerts(settings, fresh))}


def action_settings(settings, request):
    updated = validate_settings(request.get('settings') or {}, previous=settings)
    save_settings(updated)
    main.log_settings_change(updated, settings, updated)
    # A changed URL, key or mapping makes the cached series list wrong in a way no
    # timestamp would catch, so it is dropped rather than aged out.
    if canonical_json(settings.get('instances', [])) != canonical_json(updated.get('instances', [])):
        invalidate_catalogue(updated)
    # A rule that is gone must not leave its episodes behind; the store is keyed by rule.
    for gone in {rule['id'] for rule in settings.get('rules', [])} - {rule['id'] for rule in updated.get('rules', [])}:
        forget_episodes(updated, gone)
    return {'settings': redact(updated),
            'schedule_active': bool((updated.get('schedule') or {}).get('enabled')),
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
    current = visible_alerts(settings, health)
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
    'acknowledge': action_acknowledge,
    'scope-counts': action_scope_counts,
    'refresh-series': action_refresh_series,
    'episodes': action_episodes,
    'set-monitored': action_set_monitored,
    'settings': action_settings,
    'test-instance': action_test_instance,
    'series': action_series,
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


