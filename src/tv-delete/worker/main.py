#!/usr/bin/env python3
"""TV Delete worker: RPC bridge for the web UI, and the scheduled retention run.

Production invocations come from Unraid's authenticated PHP bridge (`rpc`) or from the
plugin's own cron entry (`run --scheduled`). Nothing here trusts its input: every
request is re-validated, and every deletion is gated by the settings, the guards, and a
confirmed Sonarr match.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core import (CACHE_SCHEMA, DEFAULTS, REMOVAL_ACTIONS, VERSION, Rejected, atomic_json,
                  canonical_json, classify_monitoring, describe_lifecycle, describe_selectability,
                  effective_rule, evaluate, interpolate_air_dates, new_id, normalise, redact,
                  rule_fingerprint, select_remonitor, validate_settings)
import alerts
from migrate import migrate
from sonarr import Sonarr, SonarrError, match_rule
import schedules
from tmdb import TMDB, TMDBError, fill_air_dates

NAME = 'tv-delete'
CONFIG = Path(os.environ.get('TVD_CONFIG', f'/boot/config/plugins/{NAME}/settings.json'))
CRON = Path(os.environ.get('TVD_CRON', f'/boot/config/plugins/{NAME}/schedule.cron'))
RUNTIME = Path(os.environ.get('TVD_RUNTIME', f'/var/run/{NAME}'))
DEVELOPMENT = os.environ.get('TVD_DEVELOPMENT') == '1'
UPDATE_CRON = '/usr/local/sbin/update_cron'
NOTIFY = '/usr/local/emhttp/webGui/scripts/notify'
MAX_BROWSE_ENTRIES = 500
# Every minute: the tick is cheap, and a finer resolution means an hourly schedule set to
# :07 actually fires at :07 rather than at the next multiple of five.
TICK_CRON = '* * * * *'


# ---------------------------------------------------------------------------
# Configuration and state
# ---------------------------------------------------------------------------

def default_state_dir() -> str:
    """Where to keep journals and caches on a fresh install.

    Unraid records the appdata share in docker.cfg, which is where container and plugin
    working data belongs on this server. The compiled-in default is only a fallback for a
    system that has never configured Docker.
    """
    try:
        for line in Path('/boot/config/docker.cfg').read_text().splitlines():
            if line.startswith('DOCKER_APP_CONFIG_PATH='):
                share = line.split('=', 1)[1].strip().strip('"').rstrip('/')
                if share.startswith('/mnt/'):
                    return f'{share}/{NAME}'
    except OSError:
        pass
    return DEFAULTS['state_dir']


def load_settings() -> dict:
    """Read settings, upgrading their shape if they were written by an older release."""
    if not CONFIG.exists():
        return dict(json.loads(json.dumps(DEFAULTS)), state_dir=default_state_dir())
    try:
        stored = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Settings file is unreadable ({error}). Fix or remove {CONFIG}.')
    stored = migrate(stored if isinstance(stored, dict) else {})
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(stored)
    if not stored.get('state_dir'):
        merged['state_dir'] = default_state_dir()
    return merged


def save_settings(settings: dict) -> None:
    atomic_json(CONFIG, settings)


def state_dir(settings: dict) -> Path:
    """Where journals and caches live. Falls back to the flash config if the array is down."""
    directory = Path(settings.get('state_dir') or DEFAULTS['state_dir'])
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / '.writable'
        probe.write_text('')
        probe.unlink()
        return directory
    except OSError:
        fallback = CONFIG.parent / 'state'
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def load_state(settings: dict) -> dict:
    path = state_dir(settings) / 'state.json'
    if not path.exists():
        return {'runs': [], 'last_run': None}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {'runs': [], 'last_run': None}


def save_state(settings: dict, state: dict) -> None:
    keep = int(settings.get('log_retention_runs', 50))
    state['runs'] = state.get('runs', [])[-keep:]
    atomic_json(state_dir(settings) / 'state.json', state)


def journal(settings: dict, record: dict) -> None:
    """Append-only audit trail. One JSON object per line, never rewritten."""
    path = state_dir(settings) / 'journal.jsonl'
    try:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# Environment guards
# ---------------------------------------------------------------------------

def array_ready() -> bool:
    if DEVELOPMENT:
        return True
    path = Path('/var/local/emhttp/var.ini')
    if not path.exists():
        return False
    return any(line.strip().strip(';') in ('mdState="STARTED"',) for line in path.read_text().splitlines())


def require_ready() -> None:
    if not array_ready():
        raise Rejected('The array is stopped. Start it before running TV Delete.')
    if not DEVELOPMENT and not Path('/mnt/user').is_mount():
        raise Rejected('Unraid user shares are not mounted.')


@contextlib.contextmanager
def run_lock(blocking: bool = False):
    """One run at a time. A scheduled run never overlaps a manual one."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME / 'run.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Rejected('A TV Delete run is already in progress.')
        yield


def notify(settings: dict, subject: str, description: str, importance: str = 'normal',
           event: str = 'errors') -> None:
    """Send an Unraid notification, if this kind of event is one the operator asked for."""
    wanted = (settings.get('notifications') or {}).get(event, True)
    if not wanted or DEVELOPMENT or not Path(NOTIFY).exists():
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([NOTIFY, '-e', 'TV Delete', '-s', subject, '-d', description,
                        '-i', importance], timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def write_cron(settings: dict) -> None:
    """Publish a fixed tick, and let the worker decide what is due.

    A generated crontab cannot express "the first Monday of the month", cannot notice a run
    missed while the server was off, and cannot hold a job back until Sonarr answers. The
    tick costs a few milliseconds when nothing is due and removes all three limits.
    """
    worker = f'/usr/bin/python3 /usr/local/emhttp/plugins/{NAME}/worker/main.py'
    try:
        CRON.parent.mkdir(parents=True, exist_ok=True)
        CRON.write_text(
            '# Generated by the TV Delete plugin. Schedules are set in Tools > TV Delete;\n'
            '# this entry only wakes the worker so it can decide what is due.\n'
            f'{TICK_CRON} {worker} tick 2>&1 | /usr/bin/logger -t {NAME}\n')
    except OSError as error:
        raise Rejected(f'Could not update the schedule file ({error})')
    if DEVELOPMENT or not Path(UPDATE_CRON).exists():
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([UPDATE_CRON], timeout=30, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Sonarr helpers
# ---------------------------------------------------------------------------

def client_for(settings: dict, instance_id: str) -> Sonarr:
    for instance in settings.get('instances', []):
        if instance['id'] == instance_id:
            if not instance.get('enabled', True):
                raise Rejected(f'Sonarr instance "{instance["name"]}" is disabled.')
            return Sonarr(instance)
    raise Rejected('That Sonarr instance no longer exists.')


def bind_rules(settings: dict, force: bool = False) -> list:
    """Re-verify every rule against Sonarr and persist the result.

    A rule is only ever processed when it resolves to exactly one series, so a Sonarr
    outage or a renamed folder downgrades the rule to "unmatched" and stops it acting,
    rather than letting a run guess. The series list comes from the disk cache unless
    forced, because it is the most expensive call Sonarr offers.
    """
    report = []
    catalogues = {}
    for rule in settings.get('rules', []):
        try:
            if rule['instance_id'] not in catalogues:
                catalogues[rule['instance_id']] = catalogue_for(settings, rule['instance_id'], force=force)
            series = catalogues[rule['instance_id']]
        except (Rejected, SonarrError) as error:
            rule['match_status'] = 'unmatched'
            rule['match_error'] = str(error)
            report.append({'rule_id': rule['id'], 'ok': False, 'error': str(error)})
            continue
        outcome = match_rule(rule, series)
        if outcome.get('ok'):
            found = outcome['series']
            rule.update({
                'series_id': found['series_id'],
                'series_title': found['title'],
                'tvdb_id': found['tvdb_id'],
                'path': found['path'] or rule['path'],
                'match_status': 'matched',
                'match_error': '',
                'matched_at': now_iso(),
            })
            report.append({'rule_id': rule['id'], 'ok': True, 'how': outcome['how'],
                           'series_title': found['title'], 'path': found['path']})
        else:
            rule.update({'match_status': 'unmatched', 'match_error': outcome['error']})
            report.append({'rule_id': rule['id'], 'ok': False, 'error': outcome['error']})
    save_settings(settings)
    return report


def collect_episodes(settings: dict, rule: dict, client: Sonarr, tmdb) -> tuple:
    """Sonarr's episode files for one rule.

    Everything the retention pass needs — sizes, air dates, import dates, monitoring —
    arrives with the episodes. Sonarr owns the filesystem; the plugin only asks it what it
    has and tells it what to remove.
    """
    episodes = client.episodes(rule['series_id'])
    if tmdb and rule.get('tvdb_id'):
        with contextlib.suppress(TMDBError):
            fill_air_dates(episodes, tmdb, rule['tvdb_id'])
    interpolate_air_dates(episodes)
    return episodes, [], []


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------





def delete_one(settings: dict, rule: dict, client: Sonarr, episode: dict, dry_run: bool) -> dict:
    """Ask Sonarr to remove one episode file.

    Sonarr deletes the file, moves it to its recycle bin if it has one, takes the extra
    files it imported alongside, and tidies the folder if it is configured to. None of
    that is the plugin's business, and doing it here would only be a second, worse
    implementation of it.
    """
    action = {
        'path': episode['path'],
        'season': episode.get('season'),
        'episode': episode.get('episode'),
        'title': episode.get('title'),
        'air_date': episode.get('air_date'),
        'air_source': episode.get('air_source'),
        'size': episode.get('size'),
        'reason': episode.get('reason'),
        'dry_run': dry_run,
        'ok': True,
        'error': '',
    }
    if dry_run:
        return action
    try:
        client.delete_episode_file(episode['file_id'])
    except (SonarrError, ValueError) as error:
        action['ok'] = False
        action['error'] = str(error)
    return action


# ---------------------------------------------------------------------------
# The unmonitored ledger
# ---------------------------------------------------------------------------

def load_ledger(settings: dict) -> dict:
    """Episodes this plugin unmonitored, per rule.

    Kept so a later, wider rule can put them back. Nothing the operator unmonitored by
    hand is ever recorded here, and so nothing of theirs is ever re-monitored.
    """
    path = state_dir(settings) / 'unmonitored.json'
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text())
        return stored if isinstance(stored, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_ledger(settings: dict, ledger: dict) -> None:
    try:
        atomic_json(state_dir(settings) / 'unmonitored.json', ledger)
    except OSError:
        pass


def ledger_entry(episode: dict) -> dict:
    return {
        'episode_id': episode.get('episode_id'),
        'season': episode.get('season'),
        'episode': episode.get('episode'),
        'title': episode.get('title'),
        'air_date': episode.get('air_date'),
        'air_source': episode.get('air_source'),
        'mtime': episode.get('mtime'),
        'path': episode.get('path'),
        'unmonitored_at': now_iso(),
    }


def rule_flag(settings: dict, rule: dict, name: str, default=True) -> bool:
    """A per-series override, falling back to the global value when not set."""
    value = rule.get(name)
    if value is None:
        return bool((settings.get('retention') or {}).get(name, default))
    return bool(value)


def apply_removals(settings: dict, rules, dry_run: bool) -> list:
    """Carry out the removals queued against series, before anything else runs.

    A series being removed takes no part in the rest of the run: the decision to stop
    managing it has already been made, so evaluating its retention would be work nobody
    asked for. The plugin never deletes a series itself — options five and six ask Sonarr
    to, so Sonarr's own recycle bin and bookkeeping apply.
    """
    done = []
    for rule in rules:
        queued = (rule.get('queue') or {}).get('removal')
        if not queued:
            continue
        action = queued['action']
        record = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                  'action': action, 'label': REMOVAL_ACTIONS.get(action, action),
                  'queued_at': queued.get('created_at'), 'dry_run': dry_run, 'ok': True, 'error': ''}
        if dry_run:
            done.append(record)
            continue
        try:
            client = client_for(settings, rule['instance_id'])
            if action in ('monitor-all', 'unmonitor-all'):
                episodes = client.episodes(rule['series_id'], files_only=False)
                client.set_monitored([e['episode_id'] for e in episodes], action == 'monitor-all')
            elif action == 'monitor-in-frame':
                state = monitoring_for(settings, rule)
                inside = [row['episode_id'] for row in state.get('in_frame_unmonitored') or []]
                client.set_monitored(inside, True)
            elif action in ('delete-series', 'delete-series-files'):
                client.delete_series(rule['series_id'], delete_files=(action == 'delete-series-files'))
                invalidate_catalogue(settings, rule['instance_id'])
                notify(settings, 'TV Delete removed a series',
                       f'{rule.get("series_title")}: {REMOVAL_ACTIONS[action].lower()}.',
                       'warning', event='series_removed')
        except (SonarrError, Rejected) as error:
            record.update(ok=False, error=str(error))
            done.append(record)
            continue
        log_line(settings, 'warning', f'removed {record["series_title"]}: {record["label"].lower()}')
        journal(settings, dict(record, at=now_iso()))
        done.append(record)
    return done


def reconcile_monitoring(settings: dict, rule: dict, state: dict, dry_run: bool) -> dict:
    """Bring a series' monitoring in line with its keep window.

    Unmonitoring everything outside the window is unconditional: an episode out there with
    no file, still monitored, is the fetch-and-delete loop, and nothing else ends it.
    Monitoring the gaps inside the window is the opposite — it starts downloads — so it
    happens only for a series that asked for it.
    """
    result = {'monitored': 0, 'unmonitored': 0, 'searched': 0, 'error': ''}
    outside = [row['episode_id'] for row in (state.get('out_frame_monitored') or []) if row.get('episode_id')]
    inside = [row['episode_id'] for row in (state.get('in_frame_unmonitored') or []) if row.get('episode_id')]
    result['unmonitored'] = len(outside)
    result['monitored'] = len(inside) if rule.get('monitor_missing') else 0
    if dry_run:
        return result
    try:
        client = client_for(settings, rule['instance_id'])
        if outside:
            client.set_monitored(outside, False)
        if result['monitored']:
            client.set_monitored(inside, True)
            if (settings.get('retention') or {}).get('search_after_monitor'):
                # Only what this run newly monitored, never a blanket series search.
                client.search_episodes(inside)
                result['searched'] = len(inside)
    except SonarrError as error:
        result['error'] = str(error)
    return result


def process_rule(settings: dict, rule: dict, tmdb, dry_run: bool) -> dict:
    """Evaluate and (unless previewing) execute one rule."""
    outcome = {
        'rule_id': rule['id'],
        'series_title': rule.get('series_title') or rule['path'],
        'path': rule['path'],
        'preset': '',
        'ok': True,
        'error': '',
        'note': '',
        'blocked': None,
        'considered': 0,
        'deleted': [],
        'kept': 0,
        'protected': 0,
        'freed_bytes': 0,
        'unmonitored': 0,
        'unmonitored_frame': 0,
        'monitored': 0,
        'searched': 0,
        'remonitored': [],
    }
    if rule.get('match_status') != 'matched':
        outcome.update(ok=False, error=rule.get('match_error') or 'Rule is not matched to a Sonarr series')
        return outcome
    try:
        active = effective_rule(rule, settings.get('profiles'))
    except Rejected as error:
        outcome.update(ok=False, error=str(error))
        return outcome
    outcome['preset'] = active.get('profile_name', '')
    client = client_for(settings, rule['instance_id'])
    try:
        episodes, missing, unknown = collect_episodes(settings, rule, client, tmdb)
    except SonarrError as error:
        outcome.update(ok=False, error=str(error))
        return outcome

    # -- put back what a widened rule now covers -------------------------
    ledger = load_ledger(settings)
    entries = ledger.get(rule['id'], [])
    present_ids = {episode.get('episode_id') for episode in episodes}
    # An episode that has a file again left the ledger's remit, however it returned.
    returned = [entry for entry in entries if entry.get('episode_id') in present_ids]
    entries = [entry for entry in entries if entry.get('episode_id') not in present_ids]
    ledger_dirty = bool(returned)
    if (settings.get('retention') or {}).get('auto_monitor') and entries:
        candidates = select_remonitor(episodes, entries, active, settings)
        outcome['remonitored'] = [{'season': entry.get('season'), 'episode': entry.get('episode'),
                                   'title': entry.get('title'), 'air_date': entry.get('air_date'),
                                   'dry_run': dry_run} for entry in candidates]
        if candidates and not dry_run:
            try:
                client.remonitor([entry['episode_id'] for entry in candidates])
                restored = {entry['episode_id'] for entry in candidates}
                entries = [entry for entry in entries if entry.get('episode_id') not in restored]
                ledger_dirty = True
            except SonarrError as error:
                outcome['error'] = f'Could not re-monitor widened episodes: {error}'
                outcome['remonitored'] = []

    # Phase two: bring monitoring in line with the keep window, before anything is
    # removed. Doing it first means the run leaves Sonarr consistent even if the deletion
    # pass is stopped by a guard.
    reconciled = reconcile_monitoring(settings, rule, monitoring_for(settings, rule), dry_run)
    outcome['monitored'] = reconciled['monitored']
    outcome['unmonitored_frame'] = reconciled['unmonitored']
    outcome['searched'] = reconciled['searched']
    if reconciled['error']:
        outcome['error'] = reconciled['error']

    decision = evaluate(episodes, active, settings)
    outcome['considered'] = decision['considered']
    outcome['kept'] = len(decision['keep'])
    outcome['protected'] = len(decision['protected'])
    if decision['blocked']:
        outcome['blocked'] = decision['blocked']
        if ledger_dirty and not dry_run:
            ledger[rule['id']] = entries
            save_ledger(settings, ledger)
        return outcome

    deleted, deleted_ids = [], []
    for episode in decision['delete']:
        action = delete_one(settings, rule, client, episode, dry_run)
        outcome['deleted'].append(action)
        if action['ok']:
            outcome['freed_bytes'] += int(episode.get('size') or 0)
            deleted_ids.append(episode.get('episode_id'))
            deleted.append(episode)
        else:
            outcome['ok'] = False
            outcome['error'] = action['error']

    if deleted_ids and not dry_run:
        # Not optional, and not a setting. Deleting a file while leaving the episode
        # monitored guarantees Sonarr fetches it again and the next run deletes it again.
        # Auto unmonitor governs the episodes around it, never this.
        try:
            client.unmonitor(deleted_ids)
            outcome['unmonitored'] = len(deleted_ids)
            entries = entries + [ledger_entry(episode) for episode in deleted]
            ledger_dirty = True
        except SonarrError as error:
            outcome['error'] = f'Files removed, but unmonitoring failed: {error}'
        if (settings.get('recycle') or {}).get('mode') == 'plugin':
            # Moving files out of the library is invisible to Sonarr until it rescans.
            with contextlib.suppress(SonarrError):
                client.rescan(rule['series_id'])

    if ledger_dirty and not dry_run:
        ledger[rule['id']] = entries
        save_ledger(settings, ledger)

    return outcome


def run(preview: bool = False, rule_ids=None, scheduled: bool = False) -> dict:
    """Evaluate every enabled rule, and delete unless previewing or in dry-run mode."""
    require_ready()
    settings = load_settings()
    # A manual run is always live; Test Mode governs the scheduler alone, and the preview
    # flag is the read-only plan behind "Show scheduled changes".
    test_mode = scheduled and bool((settings.get('schedule') or {}).get('test_mode', True))
    dry_run = preview or test_mode
    started = now_iso()
    clock = time.monotonic()

    bind_rules(settings)
    tmdb = None
    tmdb_cfg = settings.get('tmdb') or {}
    if tmdb_cfg.get('enabled') and tmdb_cfg.get('api_key'):
        tmdb = TMDB(tmdb_cfg['api_key'], cache_path=state_dir(settings) / 'tmdb-cache.json')

    selected = [r for r in settings.get('rules', []) if r.get('enabled')]
    if rule_ids:
        selected = [r for r in selected if r['id'] in set(rule_ids)]

    # Phase one: queued removals. A series leaving takes no further part in this run,
    # because the decision to stop managing it has already been made.
    removals = apply_removals(settings, selected, dry_run)
    if removals and not dry_run:
        removed = {record['rule_id'] for record in removals if record['ok']}
        settings['rules'] = [r for r in settings.get('rules', []) if r['id'] not in removed]
        save_settings(settings)
        health = load_health(settings)
        for rule_id in removed:
            health['rules'].pop(rule_id, None)
        health['alerts'] = [a for a in (health.get('alerts') or []) if a.get('rule_id') not in removed]
        write_cache(settings, 'health.json', health)
        selected = [r for r in selected if r['id'] not in removed]

    results, planned = [], []
    for rule in selected:
        try:
            results.append(process_rule(settings, rule, tmdb, dry_run=True))
        except Rejected as error:
            results.append({'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                            'path': rule['path'], 'ok': False, 'error': str(error), 'deleted': [],
                            'considered': 0, 'kept': 0, 'protected': 0,
                            'blocked': None, 'freed_bytes': 0, 'unmonitored': 0,
                            'remonitored': [], 'preset': '', 'note': ''})
    total = sum(len(result['deleted']) for result in results)

    cap = None
    aborted = ''
    if cap is not None and total > cap:
        aborted = (f'Guard stopped this run: {total} files were selected, above the '
                   f'limit of {cap} per run. Review the plan, then raise the limit if it is correct.')

    summary = {
        'id': new_id(),
        'started': started,
        'finished': now_iso(),
        'scheduled': scheduled,
        'preview': preview,
        'test_mode': test_mode,
        'dry_run': dry_run,
        'aborted': aborted,
        'rules': results,
        'planned': total,
        'removals': removals,
        'deleted': 0,
        'remonitored': sum(len(result.get('remonitored') or []) for result in results),
        'freed_bytes': 0,
        'errors': [r['error'] for r in results if r.get('error')],
        'blocked': [r['blocked'] for r in results if r.get('blocked')],
        'duration_seconds': 0,
    }

    if not preview and not dry_run and not aborted:
        # Second pass: the plan is re-derived immediately before acting, so a file that
        # changed between planning and execution is judged on its current state.
        executed = []
        for rule in selected:
            try:
                executed.append(process_rule(settings, rule, tmdb, dry_run=False))
            except Rejected as error:
                executed.append({'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                                 'path': rule['path'], 'ok': False, 'error': str(error), 'deleted': [],
                                 'considered': 0, 'kept': 0, 'protected': 0,
                                 'blocked': None, 'freed_bytes': 0, 'unmonitored': 0,
                                 'remonitored': [], 'preset': '', 'note': ''})
        summary['rules'] = executed
        summary['deleted'] = sum(len([d for d in r['deleted'] if d['ok']]) for r in executed)
        summary['remonitored'] = sum(len(r.get('remonitored') or []) for r in executed)
        summary['freed_bytes'] = sum(r['freed_bytes'] for r in executed)
        summary['errors'] = [r['error'] for r in executed if r.get('error')]
        summary['blocked'] = [r['blocked'] for r in executed if r.get('blocked')]
        # Deleting unmonitors, so any cached monitoring for those shows is now wrong.
        for result in executed:
            if not result.get('deleted') and not result.get('unmonitored'):
                continue
            rule = next((r for r in selected if r['id'] == result['rule_id']), None)
            if rule:
                with contextlib.suppress(Rejected, SonarrError):
                    record_monitoring(settings, rule, monitoring_for(settings, rule))

    summary['finished'] = now_iso()
    summary['duration_seconds'] = round(time.monotonic() - clock, 1)

    if not preview:
        state = load_state(settings)
        state['runs'] = state.get('runs', []) + [{
            'id': summary['id'], 'started': summary['started'], 'finished': summary['finished'],
            'scheduled': scheduled, 'dry_run': dry_run, 'planned': summary['planned'],
            'deleted': summary['deleted'], 'freed_bytes': summary['freed_bytes'],
            'remonitored': summary['remonitored'],
            'aborted': summary['aborted'], 'errors': summary['errors'][:10],
        }]
        state['last_run'] = summary
        save_state(settings, state)
        journal(settings, summary)
        log_line(settings, 'warning',
                 ('[TEST MODE] ' if test_mode else '')
                 + f'run finished: {summary["planned"]} planned, {summary["deleted"]} deleted, '
                 + f'{summary["freed_bytes"] // 1024 // 1024} MiB')
        if aborted:
            notify(settings, 'TV Delete stopped by a guard', aborted, 'warning', event='errors')
        elif summary['errors']:
            notify(settings, 'TV Delete finished with errors', '; '.join(summary['errors'])[:400],
                   'warning', event='errors')
        elif test_mode:
            # A test run notifies exactly as a real one would: that is how you learn the
            # schedule fired correctly at four in the morning.
            notify(settings, '[TEST MODE] TV Delete scheduled run',
                   f'{summary["planned"]} file(s) would have been removed. Nothing was changed.',
                   event='run_completed')
        elif summary['deleted']:
            gigabytes = summary['freed_bytes'] / 1024 ** 3
            notify(settings, 'TV Delete removed old episodes',
                   f'{summary["deleted"]} files removed, {gigabytes:.1f} GiB reclaimed.',
                   event='run_completed')
    return summary


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

def cache_path(settings: dict, name: str) -> Path:
    return state_dir(settings) / name


def read_cache(settings: dict, name: str) -> dict:
    path = cache_path(settings, name)
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text())
        return stored if isinstance(stored, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache(settings: dict, name: str, value: dict) -> None:
    try:
        atomic_json(cache_path(settings, name), value)
    except OSError:
        pass


def age_seconds(stamp: str):
    """Seconds since an ISO timestamp, or None if it is missing or unreadable."""
    if not stamp:
        return None
    try:
        moment = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - moment).total_seconds())


def catalogue_for(settings: dict, instance_id: str, force: bool = False) -> list:
    """The instance's series list, cached on disk.

    This is the single most expensive Sonarr call — 12 MB and about two seconds on a
    three thousand series library — and it is needed for matching, for the series picker,
    and for the connection test.

    The cache holds *mapped* series, so it is keyed by the cache schema as well as by age:
    adding a field to the mapping has to retire what is stored, or the new field reads as
    absent everywhere until the entry happens to expire. That is exactly how the ended
    pill stayed blank after the mapping learned to carry a series' ended flag.
    """
    cache = read_cache(settings, 'catalogue.json')
    entry = cache.get(instance_id) or {}
    ttl = int(settings.get('catalogue_ttl_minutes', 60)) * 60
    age = age_seconds(entry.get('fetched_at'))
    fresh = entry.get('series') and entry.get('schema') == CACHE_SCHEMA and age is not None and age < ttl
    if not force and fresh:
        return entry['series']
    series = client_for(settings, instance_id).series()
    cache[instance_id] = {'schema': CACHE_SCHEMA, 'fetched_at': now_iso(), 'series': series}
    write_cache(settings, 'catalogue.json', cache)
    return series


def invalidate_catalogue(settings: dict, instance_id: str = '') -> None:
    cache = read_cache(settings, 'catalogue.json')
    if instance_id:
        cache.pop(instance_id, None)
    else:
        cache = {}
    write_cache(settings, 'catalogue.json', cache)


def load_health(settings: dict) -> dict:
    health = read_cache(settings, 'health.json')
    health.setdefault('rules', {})
    health.setdefault('instances', {})
    return health


def stale_rule_ids(settings: dict, health: dict) -> list:
    """The enabled rules whose cached result is missing, outdated, or no longer applicable.

    Only these are re-read when the page opens. Everything else is served from the cache,
    which is the whole point of keeping one.
    """
    ttl = int(settings.get('health', {}).get('ttl_hours', 24)) * 3600
    stale = []
    for rule in settings.get('rules', []):
        if not rule.get('enabled'):
            continue
        entry = (health.get('rules') or {}).get(rule['id'])
        if not entry:
            stale.append(rule['id'])
            continue
        try:
            if entry.get('fingerprint') != rule_fingerprint(rule, settings):
                stale.append(rule['id'])
                continue
        except Rejected:
            stale.append(rule['id'])
            continue
        age = age_seconds(entry.get('checked_at'))
        if age is None or age > ttl:
            stale.append(rule['id'])
    return stale


def health_is_stale(settings: dict, health: dict) -> bool:
    """True when any enabled rule has no usable cached result."""
    return bool(stale_rule_ids(settings, health))


def record_monitoring(settings: dict, rule: dict, state: dict) -> None:
    """Store one rule's monitoring result. Called by the health check and by every run,
    so a library that is checked nightly costs nothing extra to keep current."""
    health = load_health(settings)
    health['rules'][rule['id']] = dict(state, checked_at=now_iso(),
                                       fingerprint=rule_fingerprint(rule, settings))
    write_cache(settings, 'health.json', health)


# ---------------------------------------------------------------------------
# Rolling log
# ---------------------------------------------------------------------------

LOG_RANK = {'minimal': 0, 'error': 1, 'warning': 2, 'verbose': 3}


def log_line(settings: dict, level: str, message: str) -> None:
    """Append one line to the rolling log, if the configured level asks for it.

    A plain text file rather than the journal: the journal records what a run decided, in a
    shape meant for machines, while this is the running commentary a person reads while
    something is happening. Size-capped with a single rotation, so it cannot fill a share.
    """
    configured = (settings.get('logging') or {}).get('level', 'warning')
    if LOG_RANK.get(level, 3) > LOG_RANK.get(configured, 2):
        return
    stamp = dt.datetime.now(dt.timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    prefix = ''
    line = f'{stamp} [{level.upper()}] {prefix}{message}\n'
    try:
        path = state_dir(settings) / 'tv-delete.log'
        limit = int((settings.get('logging') or {}).get('max_bytes', 2 * 1024 * 1024))
        if path.exists() and path.stat().st_size + len(line) > limit:
            path.replace(path.with_suffix('.log.1'))
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(line)
    except OSError:
        pass


def read_log(settings: dict, offset: int = 0, limit: int = 65536) -> dict:
    """A slice of the log for the viewer, addressed by byte offset so polling is cheap."""
    path = state_dir(settings) / 'tv-delete.log'
    try:
        size = path.stat().st_size
    except OSError:
        return {'offset': 0, 'size': 0, 'text': ''}
    # A rotation moves the file out from under the reader; start again from the beginning.
    if offset > size:
        offset = 0
    if offset <= 0:
        offset = max(0, size - limit)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            handle.seek(offset)
            text = handle.read(limit)
    except OSError:
        return {'offset': 0, 'size': size, 'text': ''}
    return {'offset': offset + len(text.encode('utf-8')), 'size': size, 'text': text}


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

def sonarr_reachable(settings: dict) -> bool:
    """Whether every enabled instance answered its last connectivity check."""
    health = load_health(settings)
    instances = health.get('instances') or {}
    for instance in settings.get('instances', []):
        if not instance.get('enabled', True):
            continue
        state = instances.get(instance['id'])
        if not state or not state.get('reachable', state.get('ok', False)):
            return False
    return True


def check_connectivity(settings: dict) -> bool:
    """Ask each enabled instance whether it is there. Cheap: one status call each."""
    health = load_health(settings)
    health.setdefault('instances', {})
    everything_up = True
    for instance in settings.get('instances', []):
        entry = dict(health['instances'].get(instance['id']) or {},
                     instance_id=instance['id'], name=instance['name'],
                     checked_at=now_iso())
        if not instance.get('enabled', True):
            entry.update(reachable=True, disabled=True, error='')
            health['instances'][instance['id']] = entry
            continue
        try:
            version = Sonarr(instance).status().get('version', '')
            was_down = entry.get('reachable') is False
            entry.update(reachable=True, ok=entry.get('ok', True), error='', sonarr_version=version)
            if was_down:
                log_line(settings, 'warning', f'{instance["name"]} is reachable again')
        except (SonarrError, Rejected) as error:
            everything_up = False
            if entry.get('reachable') is not False:
                log_line(settings, 'error', f'{instance["name"]} is unreachable: {error}')
            entry.update(reachable=False, ok=False, error=str(error))
        health['instances'][instance['id']] = entry
    write_cache(settings, 'health.json', health)
    return everything_up


# ---------------------------------------------------------------------------
# Check progress
# ---------------------------------------------------------------------------

def read_progress(settings: dict) -> dict:
    """What a check is currently doing, if one is running.

    Written to disk rather than held in memory because the reader is a different process
    every time: each RPC call is its own interpreter, and the scheduled check is another.
    A stale marker from a killed process is ignored after a few minutes rather than
    blocking the interface for ever.
    """
    progress = read_cache(settings, 'checking.json')
    if not progress.get('running'):
        return {'running': False}
    age = age_seconds(progress.get('started'))
    if age is None or age > 3600:
        return {'running': False}
    return progress


def set_progress(settings: dict, **fields) -> None:
    progress = read_cache(settings, 'checking.json')
    progress.update(fields)
    write_cache(settings, 'checking.json', progress)


def clear_progress(settings: dict) -> None:
    write_cache(settings, 'checking.json', {'running': False, 'finished': now_iso()})


def check_one_rule(settings: dict, rule: dict, instance_state: dict = None) -> dict:
    """Verify one rule, cache the result, and refresh that rule's alerts.

    Only this rule's alerts are replaced; everything else in the set is left alone, so a
    single-series recheck cannot clear a problem it never looked at.
    """
    if instance_state and not instance_state.get('ok', True):
        state = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                 'ok': False, 'status': 'unmatched', 'label': 'Sonarr unavailable',
                 'error': instance_state.get('error', ''), 'checked_at': now_iso(),
                 'fingerprint': rule_fingerprint(rule, settings)}
    else:
        state = monitoring_for(settings, rule)
        state['checked_at'] = now_iso()
        state['fingerprint'] = rule_fingerprint(rule, settings)
    health = load_health(settings)
    was = (health.get('rules') or {}).get(rule['id']) or {}
    # Said once, when Sonarr first reports it, rather than on every check thereafter.
    if state.get('ended') and not was.get('ended'):
        notify(settings, 'A series has ended',
               f'Sonarr reports {state.get("series_title")} as ended'
               + (' and nothing remains inside its keep window.'
                  if state.get('retention_expired') else '.'),
               event='series_ended')
        log_line(settings, 'warning', f'{state.get("series_title")} has ended in Sonarr')
    health['rules'][rule['id']] = state
    others = [alert for alert in (health.get('alerts') or []) if alert.get('rule_id') != rule['id']]
    health['alerts'] = alerts.merge(health.get('alerts') or [],
                                    others + alerts_for_rule(settings, rule, state))
    write_cache(settings, 'health.json', health)
    return state


# ---------------------------------------------------------------------------
# Scheduled health check
# ---------------------------------------------------------------------------

def check_instance(settings: dict, instance: dict, force: bool = True) -> dict:
    """Reachability, credentials, and whether Sonarr has somewhere to put deleted files."""
    result = {'instance_id': instance['id'], 'name': instance['name'], 'ok': True,
              'error': '', 'checked_at': now_iso(), 'reachable': True}
    if not instance.get('enabled', True):
        return dict(result, disabled=True)
    try:
        client = Sonarr(instance)
        status = client.status()
        catalogue = catalogue_for(settings, instance['id'], force=force)
    except (SonarrError, Rejected) as error:
        return dict(result, ok=False, reachable=False, error=str(error))
    result['sonarr_version'] = status.get('version')
    result['series_count'] = len(catalogue)
    result['recycle_bin'] = check_recycle_bin(settings, instance)
    return result


def alerts_for_rule(settings: dict, rule: dict, state: dict) -> list:
    """Turn one rule's checked state into the alerts it warrants.

    Monitoring drift is deliberately absent. Every run reconciles the whole keep window,
    so an alert about it would be a warning that the next run silently fixes — and the
    scheduled-changes line already says what that run will do, which is the useful form of
    the same information.
    """
    if rule.get('match_status') != 'matched':
        return [alerts.make('unmatched', rule_id=rule['id'],
                            detail=rule.get('match_error') or 'No Sonarr series resolves to this rule')]
    if not state.get('ok'):
        return [alerts.make('unmatched', rule_id=rule['id'],
                            detail=state.get('error') or state.get('label') or 'Could not read Sonarr')]
    if state.get('lifecycle') in ('ended_expired', 'ended_empty'):
        return [alerts.make('ended-expired', rule_id=rule['id'],
                            detail='Sonarr reports this series as ended and nothing remains '
                                   'inside the keep window')]
    return []


def system_alerts(settings: dict, health: dict) -> list:
    """Alerts about the installation rather than about one series."""
    found = []
    for instance in settings.get('instances', []):
        if not instance.get('enabled', True):
            continue
        state = (health.get('instances') or {}).get(instance['id']) or {}
        if state.get('reachable') is False:
            found.append(alerts.make('sonarr-unreachable', instance_id=instance['id'],
                                     detail=f'{instance["name"]}: {state.get("error", "no answer")}'))
            continue
        if not state.get('recycle_bin'):
            found.append(alerts.make('no-recycle-bin', instance_id=instance['id'],
                                     detail=f'{instance["name"]} deletes files outright; nothing is recoverable'))
    return found


def store_alerts(settings: dict, current: list) -> dict:
    """Persist the current set, keeping first-seen dates and reporting what cleared."""
    health = load_health(settings)
    existing = health.get('alerts') or []
    gone = alerts.resolved(existing, current)
    health['alerts'] = alerts.merge(existing, current)
    write_cache(settings, 'health.json', health)
    for alert in gone:
        log_line(settings, 'warning', f'resolved: {alert["title"]} — {alert.get("detail", "")}')
    return health


def run_health_check(scheduled: bool = False, force: bool = True) -> dict:
    """Verify instances, matches, folders, and monitoring, and cache the results.

    Read-only against both Sonarr and the filesystem: it never deletes and never changes a
    monitored flag. Progress is published rule by rule, so an interface open while this
    runs can show each show updating instead of waiting for the whole sweep.
    """
    require_ready()
    settings = load_settings()
    started = now_iso()
    clock = time.monotonic()
    health = load_health(settings)
    rules = [rule for rule in settings.get('rules', []) if rule.get('enabled')]
    set_progress(settings, running=True, started=started, scheduled=scheduled,
                 total=len(rules), done=0, current='', current_title='',
                 phase='instances')
    try:
        instances = {}
        for instance in settings.get('instances', []):
            instances[instance['id']] = check_instance(settings, instance, force=force)
        health['instances'] = instances
        write_cache(settings, 'health.json', health)

        set_progress(settings, phase='matching')
        bind_rules(settings, force=force)
        settings = load_settings()
        rules = [rule for rule in settings.get('rules', []) if rule.get('enabled')]
        set_progress(settings, phase='rules', total=len(rules))

        found, checked = [], 0
        found.extend(system_alerts(settings, health))
        for rule in rules:
            set_progress(settings, done=checked, current=rule['id'],
                         current_title=rule.get('series_title') or rule['path'])
            state = check_one_rule(settings, rule, instances.get(rule['instance_id']))
            checked += 1
            found.extend(alerts_for_rule(settings, rule, state))

        health = load_health(settings)
        # Results for rules that no longer exist would otherwise accumulate for ever and
        # make the counts disagree with the list on screen.
        live = {rule['id'] for rule in settings.get('rules', [])}
        health['rules'] = {rid: entry for rid, entry in (health.get('rules') or {}).items() if rid in live}
        previous = health.get('alerts') or []
        cleared = alerts.resolved(previous, found)
        health['alerts'] = alerts.merge(previous, found)
        health['checked_at'] = started
        health['duration_seconds'] = round(time.monotonic() - clock, 1)
        health['scheduled'] = scheduled
        health['rules_checked'] = checked
        write_cache(settings, 'health.json', health)
    finally:
        clear_progress(settings)

    for alert in cleared:
        log_line(settings, 'warning', f'resolved: {alert["title"]} — {alert.get("detail", "")}')
    summary = alerts.summarise(health['alerts'])
    if summary['error']:
        headline = [a for a in health['alerts'] if a['severity'] == alerts.ERROR]
        notify(settings, f'TV Delete: {summary["error"]} problem(s) need attention',
               ' | '.join(f'{a["title"]}: {a["detail"]}' for a in headline)[:600],
               'warning', event='health_problems')
    elif scheduled:
        notify(settings, 'TV Delete health check passed',
               f'{checked} rule(s) verified against Sonarr.', event='health_ok')
    log_line(settings, 'warning' if summary['error'] else 'verbose',
             f'series match check: {checked} rule(s), {summary["error"]} error(s), '
             f'{summary["warning"]} warning(s)')
    return health




# ---------------------------------------------------------------------------
# RPC actions
# ---------------------------------------------------------------------------

def plan_summary(settings: dict, health: dict) -> dict:
    """What the next run would do, totalled from the cached per-series plans.

    Reported with the age of the oldest reading and with how many series have no reading
    at all, because "nothing to do" is only as trustworthy as the checks behind it. The
    interface must not hide the Run button on an answer it cannot vouch for.
    """
    totals = {'delete': 0, 'delete_bytes': 0, 'unmonitor': 0, 'monitor': 0,
              'series': 0, 'unknown': 0, 'blocked': 0, 'oldest': None}
    alerts_now = health.get('alerts') or []
    blocking = {alert['rule_id'] for alert in alerts_now if alert.get('blocking') and alert.get('rule_id')}
    for rule in settings.get('rules', []):
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
        for key in ('delete', 'delete_bytes', 'unmonitor', 'monitor'):
            totals[key] += int(plan.get(key) or 0)
        stamp = entry.get('checked_at')
        if stamp and (totals['oldest'] is None or stamp < totals['oldest']):
            totals['oldest'] = stamp
    totals['actionable'] = totals['delete'] + totals['unmonitor'] + totals['monitor']
    # Only a complete, unblocked picture may be called up to date.
    totals['trustworthy'] = totals['unknown'] == 0
    return totals


def trim_health(health: dict) -> dict:
    """Health without the per-episode detail.

    A show with heavy drift carries hundreds of episode records; sending them for every
    rule would make the page load pay for data it only needs if you open one show's
    details. The counts drive the pills; the detail is fetched per show when asked for.
    """
    trimmed = dict(health)
    rules = {}
    for rule_id, entry in (health.get('rules') or {}).items():
        summary = {key: value for key, value in entry.items()
                   if key not in ('in_frame_unmonitored', 'out_frame_monitored')}
        summary['in_frame_unmonitored_count'] = len(entry.get('in_frame_unmonitored') or [])
        summary['out_frame_monitored_count'] = len(entry.get('out_frame_monitored') or [])
        rules[rule_id] = summary
    trimmed['rules'] = rules
    return trimmed


def job_state(settings: dict) -> dict:
    """When each scheduled job last ran, and anything waiting to run."""
    stored = read_cache(settings, 'jobs.json')
    stored.setdefault('last_run', None)
    stored.setdefault('last_series_match', None)
    stored.setdefault('last_connectivity', None)
    stored.setdefault('pending_run', None)
    return stored


def save_job_state(settings: dict, state: dict) -> None:
    write_cache(settings, 'jobs.json', state)


def tick() -> int:
    """Decide what is due and do it. Invoked every minute by the plugin's cron entry.

    Nothing here is expensive unless something is actually due: the common case is reading
    two small files and comparing timestamps.
    """
    settings = load_settings()
    if not array_ready():
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    state = job_state(settings)
    actions = []

    connectivity = int((settings.get('health') or {}).get('connectivity_seconds', 300))
    if age_seconds(state.get('last_connectivity')) is None or \
            age_seconds(state.get('last_connectivity')) >= connectivity:
        reachable = check_connectivity(settings)
        state['last_connectivity'] = now_iso()
        actions.append(f'connectivity: {"reachable" if reachable else "unreachable"}')
        # A run held back because Sonarr was unreachable goes as soon as it answers.
        if reachable and state.get('pending_run'):
            state['pending_run'] = None
            save_job_state(settings, state)
            actions.append('released the run that was waiting for Sonarr')
            with contextlib.suppress(Rejected):
                with run_lock():
                    run(preview=False, scheduled=True)
            state = job_state(settings)
            state['last_run'] = now_iso()

    match_schedule = (settings.get('health') or {}).get('series_match') or {}
    if schedules.is_due(match_schedule, now, state.get('last_series_match')):
        actions.append('series match check')
        state['last_series_match'] = now_iso()
        save_job_state(settings, state)
        with contextlib.suppress(Rejected):
            with run_lock():
                run_health_check(scheduled=True)

    if schedules.is_due(settings.get('schedule') or {}, now, state.get('last_run')):
        if not sonarr_reachable(settings):
            # Queued rather than skipped: exactly one pending run, so an outage over a
            # weekend produces one catch-up rather than a backlog.
            state['pending_run'] = now_iso()
            actions.append('run queued: Sonarr is unreachable')
        else:
            state['last_run'] = now_iso()
            save_job_state(settings, state)
            actions.append('scheduled run')
            with contextlib.suppress(Rejected):
                with run_lock():
                    run(preview=False, scheduled=True)
            state = job_state(settings)
            state['last_run'] = now_iso()

    save_job_state(settings, state)
    for message in actions:
        log_line(settings, 'verbose', f'tick: {message}')
    return 0


def action_snapshot(settings, request):
    state = load_state(settings)
    health = load_health(settings)
    return {
        'version': VERSION,
        'settings': redact(settings),
        'array_ready': array_ready(),
        'runs': list(reversed(state.get('runs', []))),
        'last_run': state.get('last_run'),
        'schedule_active': CRON.exists(),
        # Cached monitoring, rendered immediately. Every entry carries the moment it was
        # read, so nothing on screen pretends to be live.
        'health': trim_health(health),
        'health_stale': health_is_stale(settings, health),
        'stale_rules': stale_rule_ids(settings, health),
        'progress': read_progress(settings),
        'alerts': (load_health(settings).get('alerts') or []),
        'alert_summary': alerts.summarise(load_health(settings).get('alerts')),
        'schedule_text': schedules.describe(settings.get('schedule') or {}),
        'series_match_text': schedules.describe((settings.get('health') or {}).get('series_match') or {}),
        'jobs': job_state(settings),
        'plan': plan_summary(settings, load_health(settings)),
        'test_mode': bool((settings.get('schedule') or {}).get('test_mode', True)),
    }


def action_health(settings, request):
    """Refresh the cached health, on demand or because the page found it stale."""
    return {'health': trim_health(run_health_check(scheduled=False, force=bool(request.get('force'))))}


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
    require_ready()
    health = load_health(settings)
    instance_state = (health.get('instances') or {}).get(rule['instance_id'])
    if not instance_state or age_seconds(instance_state.get('checked_at')) is None \
            or age_seconds(instance_state.get('checked_at')) > 900:
        instance = next((i for i in settings.get('instances', []) if i['id'] == rule['instance_id']), None)
        if instance:
            instance_state = check_instance(settings, instance, force=False)
            health = load_health(settings)
            health.setdefault('instances', {})[instance['id']] = instance_state
            write_cache(settings, 'health.json', health)
    bind_rules(settings)
    settings = load_settings()
    rule = next((r for r in settings.get('rules', []) if r['id'] == rule['id']), rule)
    state = check_one_rule(settings, rule, instance_state)
    summary = trim_health({'rules': {rule['id']: state}})['rules'][rule['id']]
    return {'busy': False, 'rule_id': rule['id'], 'state': summary}


def action_settings(settings, request):
    updated = validate_settings(request.get('settings') or {}, previous=settings)
    save_settings(updated)
    write_cron(updated)
    # A changed URL, key or mapping makes the cached series list wrong in a way no
    # timestamp would catch, so it is dropped rather than aged out.
    if canonical_json(settings.get('instances', [])) != canonical_json(updated.get('instances', [])):
        invalidate_catalogue(updated)
    return {'settings': redact(updated), 'schedule_active': CRON.exists(),
            'schedule_text': schedules.describe(updated.get('schedule') or {}),
            'series_match_text': schedules.describe((updated.get('health') or {}).get('series_match') or {})}






def action_series(settings, request):
    """The series list behind the picker, answered entirely from Sonarr."""
    instance_id = str(request.get('instance_id') or '')
    catalogue = catalogue_for(settings, instance_id, force=bool(request.get('force')))
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
        'recycle_bin': check_recycle_bin(settings, instance),
    }




def monitoring_for(settings: dict, rule: dict) -> dict:
    """Everything one check knows about a series: monitoring, lifecycle, and the plan.

    Entirely from Sonarr. Sizes, air dates, import dates, monitoring and season numbers all
    arrive with the episodes, so this touches no filesystem and needs no path mapping.
    """
    base = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'], 'ok': True}
    if rule.get('match_status') != 'matched':
        return dict(base, ok=False, error=rule.get('match_error') or 'Rule is not matched to a Sonarr series',
                    status='unmatched', label='Not matched to Sonarr')
    try:
        active = effective_rule(rule, settings.get('profiles'))
        client = client_for(settings, rule['instance_id'])
        episodes = client.episodes(rule['series_id'], files_only=False)
    except Rejected as error:
        return dict(base, ok=False, error=str(error), status='unmatched', label='Could not read Sonarr')
    interpolate_air_dates(episodes)

    state = dict(base, **classify_monitoring(episodes, active, settings))
    series = next((entry for entry in catalogue_for(settings, rule['instance_id'])
                   if entry['series_id'] == rule['series_id']), None)
    if series:
        state.update(describe_lifecycle(state, series))

    present = [episode for episode in episodes if episode.get('has_file')]
    decision = evaluate(present, active, settings)
    would_delete = decision['delete']
    deleting = {item.get('episode_id') for item in would_delete}
    outside = state.get('out_frame_monitored') or []
    inside = state.get('in_frame_unmonitored') or []
    # Deleting always unmonitors, and the run also unmonitors everything else outside the
    # window; both are counted here because both are changes someone would want to see.
    unmonitor = {row.get('episode_id') for row in outside} | deleting
    state['plan'] = {
        'delete': len(would_delete),
        'delete_bytes': sum(int(item.get('size') or 0) for item in would_delete),
        'blocked': decision['blocked'] or '',
        'unmonitor': len([row for row in outside if row.get('episode_id') in unmonitor]),
        # Monitoring episodes that have no file starts downloads, so only when asked for.
        'monitor': len(inside) if rule.get('monitor_missing') else 0,
        'computed_at': now_iso(),
    }
    return state


def action_monitoring(settings, request):
    """Status for the requested rules. The UI asks per card, or for all of them at once."""
    wanted = set(request.get('rule_ids') or [])
    rules = [r for r in settings.get('rules', []) if not wanted or r['id'] in wanted]
    return {'monitoring': [monitoring_for(settings, rule) for rule in rules]}


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
    status = monitoring_for(settings, rule)
    if not status.get('ok'):
        raise Rejected(status.get('error') or 'This rule is not matched to a Sonarr series.')

    targets = status['in_frame_unmonitored'] if mode == 'monitor-in-frame' else status['out_frame_monitored']
    ids = [entry['episode_id'] for entry in targets if entry.get('episode_id')]
    if not ids:
        return {'changed': 0, 'monitoring': monitoring_for(settings, rule),
                'ok_message': 'Nothing to change; monitoring already matches the keep frame.'}
    client = client_for(settings, rule['instance_id'])
    client.set_monitored(ids, mode == 'monitor-in-frame')

    if mode == 'unmonitor-out-frame':
        # Recorded like any other unmonitoring this plugin performs, so widening the rule
        # later can put these episodes back.
        ledger = load_ledger(settings)
        entries = ledger.get(rule['id'], [])
        known = {entry.get('episode_id') for entry in entries}
        for target in targets:
            if target['episode_id'] in known:
                continue
            entries.append({'episode_id': target['episode_id'], 'season': target['season'],
                            'episode': target['episode'], 'title': target['title'],
                            'air_date': target['air_date'], 'air_source': 'sonarr',
                            'mtime': None, 'path': '', 'unmonitored_at': now_iso()})
        ledger[rule['id']] = entries
        save_ledger(settings, ledger)
    else:
        # Monitored again by hand: the ledger no longer speaks for these episodes.
        ledger = load_ledger(settings)
        restored = {entry['episode_id'] for entry in targets}
        ledger[rule['id']] = [e for e in ledger.get(rule['id'], []) if e.get('episode_id') not in restored]
        save_ledger(settings, ledger)

    verb = 'monitored' if mode == 'monitor-in-frame' else 'unmonitored'
    return {'changed': len(ids), 'monitoring': monitoring_for(settings, rule),
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
        report = bind_rules(settings, force=True)
        settings = load_settings()
        if rule:
            rule = next((r for r in settings.get('rules', []) if r['id'] == rule_id), rule)
            check_one_rule(settings, rule)
        return {'report': report, 'settings': redact(settings)}
    if kind == 'accept-path':
        if not rule:
            raise Rejected('That rule no longer exists.')
        if settings.get('preview', True):
            raise Rejected('Preview mode is on, so nothing is changed.')
        bind_rules(settings, force=True)
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


def check_recycle_bin(settings: dict, instance: dict) -> str:
    """Sonarr's recycle bin path, or '' when it has none. Cached with the instance state."""
    try:
        media = Sonarr(instance)._request('GET', 'config/mediamanagement') or {}
        return str(media.get('recycleBin') or '')
    except (SonarrError, Rejected):
        return ''


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
    return {'report': bind_rules(settings), 'settings': redact(load_settings())}


def action_preview(settings, request):
    ids = request.get('rule_ids') or None
    return {'result': run(preview=True, rule_ids=ids)}


def action_run(settings, request):
    with run_lock():
        return {'result': run(preview=False, rule_ids=request.get('rule_ids') or None)}


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


def rpc(request: dict) -> dict:
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli() -> int:
    parser = argparse.ArgumentParser(description='TV Delete worker')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('rpc', help='read one JSON request on stdin, write one JSON reply')
    run_parser = sub.add_parser('run', help='evaluate rules and delete')
    run_parser.add_argument('--scheduled', action='store_true')
    run_parser.add_argument('--preview', action='store_true')
    sub.add_parser('preview', help='evaluate rules without deleting')
    check_parser = sub.add_parser('check', help='verify Sonarr, matches, folders and monitoring')
    check_parser.add_argument('--scheduled', action='store_true')
    sub.add_parser('tick', help='decide what is due and run it (called every minute by cron)')
    sub.add_parser('resume', help='re-publish the cron entry after a reboot or array start')
    sub.add_parser('stop', help='no-op placeholder kept for symmetry with array events')
    args = parser.parse_args()

    if args.command == 'rpc':
        try:
            request = json.loads(sys.stdin.read() or '{}')
        except json.JSONDecodeError:
            print(json.dumps({'ok': False, 'error': 'Malformed request'}))
            return 0
        print(json.dumps(rpc(request if isinstance(request, dict) else {}), ensure_ascii=False))
        return 0
    if args.command in ('run', 'preview'):
        preview = args.command == 'preview' or getattr(args, 'preview', False)
        try:
            with run_lock(blocking=False):
                result = run(preview=preview, scheduled=getattr(args, 'scheduled', False))
        except Rejected as error:
            print(f'TV Delete: {error}', file=sys.stderr)
            return 1
        verb = 'would delete' if result['dry_run'] else 'deleted'
        print(f'TV Delete {verb} {result["planned"] if result["dry_run"] else result["deleted"]} files '
              f'across {len(result["rules"])} rules in {result["duration_seconds"]}s')
        for message in result['errors'] + result['blocked'] + ([result['aborted']] if result['aborted'] else []):
            print(f'  ! {message}')
        return 0
    if args.command == 'tick':
        return tick()
    if args.command == 'check':
        try:
            with run_lock(blocking=False):
                health = run_health_check(scheduled=getattr(args, 'scheduled', False))
        except Rejected as error:
            print(f'TV Delete: {error}', file=sys.stderr)
            return 1
        summary = alerts.summarise(health.get('alerts'))
        print(f'TV Delete checked {health.get("rules_checked", 0)} rule(s) in '
              f'{health.get("duration_seconds", 0)}s; {summary["error"]} error(s), '
              f'{summary["warning"]} warning(s)')
        for alert in health.get('alerts') or []:
            if alert['severity'] != alerts.NOTICE:
                print(f'  {alert["severity"]}: {alert["title"]} — {alert["detail"]}')
        return 0
    if args.command == 'resume':
        with contextlib.suppress(Rejected):
            write_cron(load_settings())
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(cli())
