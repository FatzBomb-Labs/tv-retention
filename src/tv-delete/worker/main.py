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

from core import (CACHE_SCHEMA, DEFAULTS, VERSION, Rejected, atomic_json, canonical_json, derive_mappings,
                  effective_rule, empty_directories, evaluate, new_id, normalise, redact,
                  scan_media, select_remonitor, sidecars_for, validate_cron, validate_settings,
                  classify_monitoring, classify_orphan, describe_lifecycle, describe_selectability,
                  is_media, normalise_title, rule_fingerprint)
from sonarr import Sonarr, SonarrError, match_rule
from tmdb import TMDB, TMDBError, fill_air_dates

NAME = 'tv-delete'
CONFIG = Path(os.environ.get('TVD_CONFIG', f'/boot/config/plugins/{NAME}/settings.json'))
CRON = Path(os.environ.get('TVD_CRON', f'/boot/config/plugins/{NAME}/schedule.cron'))
RUNTIME = Path(os.environ.get('TVD_RUNTIME', f'/var/run/{NAME}'))
DEVELOPMENT = os.environ.get('TVD_DEVELOPMENT') == '1'
UPDATE_CRON = '/usr/local/sbin/update_cron'
NOTIFY = '/usr/local/emhttp/webGui/scripts/notify'
MAX_BROWSE_ENTRIES = 500


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
    if not CONFIG.exists():
        return dict(json.loads(json.dumps(DEFAULTS)), state_dir=default_state_dir())
    try:
        stored = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Settings file is unreadable ({error}). Fix or remove {CONFIG}.')
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(stored if isinstance(stored, dict) else {})
    if not (stored or {}).get('state_dir'):
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


def notify(settings: dict, subject: str, description: str, importance: str = 'normal') -> None:
    if not settings.get('notify') or DEVELOPMENT or not Path(NOTIFY).exists():
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([NOTIFY, '-e', 'TV Delete', '-s', subject, '-d', description,
                        '-i', importance], timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def write_cron(settings: dict) -> None:
    """Publish the plugin's cron entries, then ask Unraid to rebuild the crontab.

    Two independent schedules. The health check is read-only and is worth running even
    when the retention run is off or in dry run, which is exactly when a broken mapping or
    an unreachable Sonarr would otherwise go unnoticed.
    """
    schedule = settings.get('schedule') or {}
    health = settings.get('health') or {}
    worker = f'/usr/bin/python3 /usr/local/emhttp/plugins/{NAME}/worker/main.py'
    lines = ['# Generated by the TV Delete plugin. Edit the schedules in Tools > TV Delete.\n']
    try:
        if schedule.get('enabled'):
            lines.append(f'{validate_cron(schedule.get("cron"))} {worker} run --scheduled '
                         f'2>&1 | /usr/bin/logger -t {NAME}\n')
        if health.get('enabled'):
            lines.append(f'{validate_cron(health.get("cron"))} {worker} check --scheduled '
                         f'2>&1 | /usr/bin/logger -t {NAME}\n')
        if len(lines) > 1:
            CRON.parent.mkdir(parents=True, exist_ok=True)
            CRON.write_text(''.join(lines))
        elif CRON.exists():
            CRON.unlink()
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
    """Sonarr's episode files for one rule, annotated with on-disk facts.

    Returns the episodes Sonarr knows about that exist on disk, plus the media files
    found under the folder that Sonarr does not know about. Unknown files are reported
    and never deleted, because without a Sonarr record there is no air date, no episode
    number, and no safe way to unmonitor.
    """
    episodes = client.episodes(rule['series_id'])
    if tmdb and rule.get('tvdb_id'):
        with contextlib.suppress(TMDBError):
            fill_air_dates(episodes, tmdb, rule['tvdb_id'])
    present, missing = [], []
    for episode in episodes:
        path = Path(episode['path'])
        try:
            stat = path.stat()
        except OSError:
            missing.append(episode)
            continue
        episode['mtime'] = stat.st_mtime
        episode['size'] = episode.get('size') or stat.st_size
        present.append(episode)
    known = {normalise(episode['path']) for episode in present}
    unknown = [entry for entry in scan_media(rule['path']) if entry['path'] not in known]
    return present, missing, unknown


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def recycle_file(settings: dict, rule: dict, path: Path) -> str:
    """Move a file into the plugin recycle folder, mirroring its library path."""
    root = Path(settings['recycle']['path'])
    relative = Path(path).relative_to(Path(rule['path']).parent) if str(path).startswith(str(rule['path'])) else Path(path).name
    target = root / Path(rule.get('series_title') or 'unknown').name / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target = target.with_name(f'{target.stem}.{int(time.time())}{target.suffix}')
    shutil.move(str(path), str(target))
    return str(target)


def purge_recycle(settings: dict) -> int:
    """Delete recycled files past their retention period. Only the plugin's own folder."""
    recycle = settings.get('recycle') or {}
    if recycle.get('mode') != 'plugin' or not recycle.get('path'):
        return 0
    root = Path(recycle['path'])
    if not root.is_dir():
        return 0
    cutoff = time.time() - int(recycle.get('retention_days', 14)) * 86400
    removed = 0
    for base, directories, files in os.walk(root, topdown=False, onerror=lambda error: None):
        for name in files:
            path = Path(base) / name
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        with contextlib.suppress(OSError):
            if Path(base) != root and not any(Path(base).iterdir()):
                Path(base).rmdir()
    return removed


def delete_one(settings: dict, rule: dict, client: Sonarr, episode: dict, dry_run: bool) -> dict:
    """Remove one episode file and its sidecars. Returns what was (or would be) done."""
    mode = (settings.get('recycle') or {}).get('mode', 'sonarr')
    sidecar_cfg = settings.get('sidecars') or {}
    sidecars = sidecars_for(episode['path'], sidecar_cfg.get('extensions')) if sidecar_cfg.get('enabled') else []
    action = {
        'path': episode['path'],
        'season': episode.get('season'),
        'episode': episode.get('episode'),
        'title': episode.get('title'),
        'air_date': episode.get('air_date'),
        'air_source': episode.get('air_source'),
        'size': episode.get('size'),
        'reason': episode.get('reason'),
        'sidecars': sidecars,
        'method': 'recycle' if mode == 'plugin' else 'sonarr-api',
        'dry_run': dry_run,
        'ok': True,
        'error': '',
    }
    if dry_run:
        return action
    try:
        if mode == 'plugin':
            action['recycled_to'] = recycle_file(settings, rule, Path(episode['path']))
        else:
            client.delete_episode_file(episode['file_id'])
            # Sonarr removes the media file itself; a leftover means the path mapping is wrong.
            if Path(episode['path']).exists():
                action['ok'] = False
                action['error'] = ('Sonarr reported the file deleted but it is still on disk. '
                                   'Check this instance\'s path mapping.')
                return action
        for sidecar in sidecars:
            with contextlib.suppress(OSError):
                Path(sidecar).unlink()
    except (SonarrError, OSError, ValueError) as error:
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
        'unknown_files': [],
        'missing_files': 0,
        'freed_bytes': 0,
        'emptied_dirs': [],
        'unmonitored': 0,
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

    if not Path(rule['path']).is_dir():
        # Sonarr creates a series folder on its first import. Absent with files expected is
        # a mapping fault; absent with nothing imported is simply a series with no episodes.
        if missing:
            outcome.update(ok=False, error=(
                f'Sonarr reports {len(missing)} file(s) for this series, but {rule["path"]} '
                'does not exist on this server. Check this instance\'s path mapping.'))
        else:
            outcome['note'] = ('Sonarr has no files for this series yet, so its folder has not '
                               'been created. Nothing to do.')
        return outcome

    # -- put back what a widened rule now covers -------------------------
    ledger = load_ledger(settings)
    entries = ledger.get(rule['id'], [])
    present_ids = {episode.get('episode_id') for episode in episodes}
    # An episode that has a file again left the ledger's remit, however it returned.
    returned = [entry for entry in entries if entry.get('episode_id') in present_ids]
    entries = [entry for entry in entries if entry.get('episode_id') not in present_ids]
    ledger_dirty = bool(returned)
    if (settings.get('retention') or {}).get('remonitor_widened') and entries:
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

    decision = evaluate(episodes, active, settings)
    outcome['considered'] = decision['considered']
    outcome['kept'] = len(decision['keep'])
    outcome['protected'] = len(decision['protected'])
    outcome['missing_files'] = len(missing)
    outcome['unknown_files'] = [entry['path'] for entry in unknown[:50]]
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
        if rule.get('unmonitor') and (settings.get('retention') or {}).get('unmonitor_deleted', True):
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

    if settings.get('delete_empty_dirs') and (deleted_ids or dry_run):
        for directory in empty_directories(rule['path']):
            outcome['emptied_dirs'].append(directory)
            if not dry_run:
                with contextlib.suppress(OSError):
                    Path(directory).rmdir()
    return outcome


def run(preview: bool = False, rule_ids=None, scheduled: bool = False) -> dict:
    """Evaluate every enabled rule, and delete unless previewing or in dry-run mode."""
    require_ready()
    settings = load_settings()
    dry_run = preview or bool(settings.get('dry_run', True))
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

    results, planned = [], []
    for rule in selected:
        try:
            results.append(process_rule(settings, rule, tmdb, dry_run=True))
        except Rejected as error:
            results.append({'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                            'path': rule['path'], 'ok': False, 'error': str(error), 'deleted': [],
                            'considered': 0, 'kept': 0, 'protected': 0, 'unknown_files': [],
                            'blocked': None, 'freed_bytes': 0, 'emptied_dirs': [], 'unmonitored': 0,
                            'missing_files': 0, 'remonitored': [], 'preset': '', 'note': ''})
    total = sum(len(result['deleted']) for result in results)

    cap = int((settings.get('guards') or {}).get('max_deletes_per_run', 200))
    aborted = ''
    if total > cap:
        aborted = (f'Guard stopped this run: {total} files were selected, above the '
                   f'limit of {cap} per run. Review the plan, then raise the limit if it is correct.')

    summary = {
        'id': new_id(),
        'started': started,
        'finished': now_iso(),
        'scheduled': scheduled,
        'preview': preview,
        'dry_run': dry_run,
        'aborted': aborted,
        'rules': results,
        'planned': total,
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
                                 'considered': 0, 'kept': 0, 'protected': 0, 'unknown_files': [],
                                 'blocked': None, 'freed_bytes': 0, 'emptied_dirs': [], 'unmonitored': 0,
                                 'missing_files': 0, 'remonitored': [], 'preset': '', 'note': ''})
        summary['rules'] = executed
        summary['deleted'] = sum(len([d for d in r['deleted'] if d['ok']]) for r in executed)
        summary['remonitored'] = sum(len(r.get('remonitored') or []) for r in executed)
        summary['freed_bytes'] = sum(r['freed_bytes'] for r in executed)
        summary['errors'] = [r['error'] for r in executed if r.get('error')]
        summary['blocked'] = [r['blocked'] for r in executed if r.get('blocked')]
        summary['recycle_purged'] = purge_recycle(settings)
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
        if aborted:
            notify(settings, 'TV Delete stopped by a guard', aborted, 'warning')
        elif summary['errors']:
            notify(settings, 'TV Delete finished with errors', '; '.join(summary['errors'])[:400], 'warning')
        elif summary['deleted']:
            gigabytes = summary['freed_bytes'] / 1024 ** 3
            notify(settings, 'TV Delete removed old episodes',
                   f'{summary["deleted"]} files removed, {gigabytes:.1f} GiB reclaimed.')
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
    """Verify one rule and cache the result. The unit both callers work in."""
    if instance_state and not instance_state.get('ok', True):
        entry = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                 'ok': False, 'status': 'unmatched', 'label': 'Sonarr unavailable',
                 'error': instance_state.get('error', ''), 'checked_at': now_iso(),
                 'fingerprint': rule_fingerprint(rule, settings)}
        health = load_health(settings)
        health['rules'][rule['id']] = entry
        write_cache(settings, 'health.json', health)
        return entry
    state = monitoring_for(settings, rule)
    folder_ok, folder_note = folder_state(settings, rule)
    state['folder_ok'] = folder_ok
    state['folder_note'] = folder_note
    state['checked_at'] = now_iso()
    state['fingerprint'] = rule_fingerprint(rule, settings)
    health = load_health(settings)
    health['rules'][rule['id']] = state
    write_cache(settings, 'health.json', health)
    return state


# ---------------------------------------------------------------------------
# Scheduled health check
# ---------------------------------------------------------------------------

def check_instance(settings: dict, instance: dict, force: bool = True) -> dict:
    """Reachability, credentials, and whether the path mapping resolves."""
    result = {'instance_id': instance['id'], 'name': instance['name'], 'ok': True,
              'error': '', 'checked_at': now_iso(), 'folders_missing': 0}
    if not instance.get('enabled', True):
        return dict(result, ok=True, disabled=True)
    try:
        client = Sonarr(instance)
        client.status()
        catalogue = catalogue_for(settings, instance['id'], force=force)
    except (SonarrError, Rejected) as error:
        return dict(result, ok=False, error=str(error))
    with_files = [entry for entry in catalogue if entry['path'] and entry['episode_file_count'] > 0]
    missing = [entry for entry in with_files if not Path(entry['path']).is_dir()]
    result['series_count'] = len(catalogue)
    result['folders_missing'] = len(missing)
    if missing:
        result.update(ok=False, error=(
            f'{len(missing)} series folder(s) that Sonarr says hold files are not on this '
            'server. Check this instance\'s path mapping.'))
    return result


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

        problems, checked = [], 0
        for rule in rules:
            set_progress(settings, done=checked, current=rule['id'],
                         current_title=rule.get('series_title') or rule['path'])
            state = check_one_rule(settings, rule, instances.get(rule['instance_id']))
            checked += 1
            if not state.get('ok'):
                problems.append(f'{state["series_title"]}: {state.get("error") or state.get("label")}')
            elif state.get('folder_ok') is False:
                problems.append(f'{state["series_title"]}: {state.get("folder_note")}')

        health = load_health(settings)
        # Results for rules that no longer exist would otherwise accumulate for ever and
        # make the counts disagree with the list on screen.
        live = {rule['id'] for rule in settings.get('rules', [])}
        health['rules'] = {rid: entry for rid, entry in (health.get('rules') or {}).items() if rid in live}
        health['checked_at'] = started
        health['duration_seconds'] = round(time.monotonic() - clock, 1)
        health['scheduled'] = scheduled
        health['problems'] = problems
        health['rules_checked'] = checked
        write_cache(settings, 'health.json', health)
    finally:
        clear_progress(settings)

    if problems:
        notify(settings, f'TV Delete found {len(problems)} problem(s)',
               ' | '.join(problems)[:600], 'warning')
    elif scheduled and settings.get('health', {}).get('notify_ok'):
        notify(settings, 'TV Delete health check passed',
               f'{checked} rule(s) verified against Sonarr.')
    return health


def folder_state(settings: dict, rule: dict) -> tuple:
    """Whether a rule's folder is a problem, distinguishing "not created yet"."""
    if Path(rule['path']).is_dir():
        return True, ''
    try:
        catalogue = catalogue_for(settings, rule['instance_id'])
    except (SonarrError, Rejected):
        return True, ''
    series = next((entry for entry in catalogue if entry['series_id'] == rule.get('series_id')), None)
    if series and series.get('episode_file_count'):
        return False, (f'Sonarr reports {series["episode_file_count"]} file(s) but {rule["path"]} '
                       'is not on this server. Check the path mapping.')
    return True, 'Sonarr has no files for this series yet, so its folder has not been created.'


# ---------------------------------------------------------------------------
# RPC actions
# ---------------------------------------------------------------------------

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
    return {'settings': redact(updated), 'schedule_active': CRON.exists()}


def action_test_instance(settings, request):
    """Verify one Sonarr instance: reachable, authorised, and correctly path-mapped.

    A series folder that is absent is only evidence of a bad mapping when Sonarr believes
    it has files there. Sonarr does not create a series folder until it imports something,
    so an empty series legitimately has no folder and must not be reported as an error.
    """
    from core import validate_instance
    existing = {i['id']: i.get('api_key', '') for i in settings.get('instances', [])}
    instance = validate_instance(request.get('instance') or {}, existing)
    client = Sonarr(instance)
    status = client.status()
    catalogue = client.series()
    mapped = [entry for entry in catalogue if entry['path']]
    with_files = [entry for entry in mapped if entry['episode_file_count'] > 0]
    found = [entry for entry in with_files if Path(entry['path']).is_dir()]
    broken = [entry for entry in with_files if not Path(entry['path']).is_dir()]
    not_created = [entry for entry in mapped
                   if entry['episode_file_count'] == 0 and not Path(entry['path']).is_dir()]
    recycle_bin = ''
    try:
        media = client._request('GET', 'config/mediamanagement') or {}
        recycle_bin = media.get('recycleBin') or ''
    except SonarrError:
        recycle_bin = ''
    return {
        'sonarr_version': status.get('version'),
        'series_count': len(catalogue),
        'series_with_files': len(with_files),
        'folders_found': len(found),
        'folders_missing': [entry['path'] for entry in broken][:10],
        'folders_not_created': len(not_created),
        'mapping_ok': not broken and bool(with_files),
        'sonarr_recycle_bin': recycle_bin,
        'sample': [{'title': entry['title'], 'sonarr_path': entry['sonarr_path'], 'path': entry['path'],
                    'files': entry['episode_file_count'],
                    'exists': Path(entry['path']).is_dir()} for entry in (broken + with_files)[:5]],
    }


def docker_mounts(hint_port: str = '') -> list:
    """Bind mounts of the running containers, preferring one that publishes hint_port.

    Used to translate the paths Sonarr reports into Unraid share paths without the
    operator having to look either of them up. Read-only: `docker ps` and `docker inspect`
    only.
    """
    if not shutil.which('docker'):
        return []
    try:
        ids = subprocess.run(['docker', 'ps', '-q'], text=True, timeout=20, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.split()
        if not ids:
            return []
        containers = json.loads(subprocess.run(['docker', 'inspect', *ids], text=True, timeout=30,
                                               check=True, stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL).stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return []

    def publishes(container) -> bool:
        ports = ((container.get('NetworkSettings') or {}).get('Ports') or {})
        for bindings in ports.values():
            for binding in bindings or []:
                if str(binding.get('HostPort')) == str(hint_port):
                    return True
        return False

    # A container publishing the port from the Sonarr URL is almost certainly that Sonarr.
    ordered = sorted(containers, key=lambda container: 0 if (hint_port and publishes(container)) else 1)
    if hint_port and ordered and publishes(ordered[0]):
        ordered = [container for container in ordered if publishes(container)]
    mounts = []
    for container in ordered:
        for mount in container.get('Mounts') or []:
            if mount.get('Type') == 'bind' and mount.get('Source') and mount.get('Destination'):
                mounts.append({'source': mount['Source'], 'destination': mount['Destination'],
                               'container': (container.get('Name') or '').lstrip('/')})
    return mounts


def action_detect_mappings(settings, request):
    """Propose path mappings for one Sonarr instance, from its root folders and Docker."""
    from core import validate_instance
    existing = {i['id']: i.get('api_key', '') for i in settings.get('instances', [])}
    instance = validate_instance(request.get('instance') or {}, existing)
    roots = Sonarr(instance).root_folders()
    port = urllib.parse.urlparse(instance['url']).port or ''
    mounts = docker_mounts(str(port))
    mappings = derive_mappings(roots, mounts)
    for mapping in mappings:
        mapping['exists'] = Path(mapping['to']).is_dir()
    return {
        'root_folders': roots,
        'mappings': mappings,
        'containers': sorted({mount['container'] for mount in mounts if mount.get('container')}),
        'note': '' if mappings else ('Sonarr reported its root folders, but no running container '
                                     'mounts them. Enter the mapping by hand.'),
    }


def action_series(settings, request):
    """The series list behind the picker, each marked selectable or explained."""
    catalogue = catalogue_for(settings, str(request.get('instance_id') or ''))
    used = {r['path']: r for r in settings.get('rules', []) if r['id'] != str(request.get('except_rule') or '')}
    results = []
    for entry in catalogue:
        exists = Path(entry['path']).is_dir() if entry['path'] else False
        in_use = entry['path'] in used
        results.append(dict(entry, exists=exists, in_use=in_use,
                            **describe_selectability(entry, exists, in_use)))
    return {'series': results}


def monitoring_for(settings: dict, rule: dict) -> dict:
    """Live monitoring status for one rule, or an explanation of why it cannot be read."""
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
    state = dict(base, **classify_monitoring(episodes, active, settings))
    series = next((entry for entry in catalogue_for(settings, rule['instance_id'])
                   if entry['series_id'] == rule['series_id']), None)
    if series:
        state.update(describe_lifecycle(state, series))
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


def folder_size(path: Path, limit: int = 20000) -> tuple:
    """Total bytes and media-file count below a folder, bounded so a huge tree cannot stall."""
    total, files = 0, 0
    for base, directories, names in os.walk(path, onerror=lambda error: None):
        for name in names:
            if not is_media(Path(name)):
                continue
            files += 1
            with contextlib.suppress(OSError):
                total += (Path(base) / name).stat().st_size
            if files >= limit:
                return total, files
    return total, files


def holds_media(path: Path) -> bool:
    for base, directories, names in os.walk(path, onerror=lambda error: None):
        if any(is_media(Path(name)) for name in names):
            return True
    return False


def looks_like_show(path: Path) -> bool:
    """Whether a folder is a show rather than a category holding shows.

    A show folder either holds episodes directly or holds season folders that do. A
    category folder holds show folders, so its media is two levels down. Without this a
    category whose every show is unclaimed would be reported as one row instead of naming
    the shows inside it.
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    if any(entry.is_file() and is_media(entry) for entry in entries):
        return True
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            if any(child.is_file() and is_media(child) for child in entry.iterdir()):
                return True
        except OSError:
            continue
    return False


def scan_library(root: Path, series_paths, max_depth: int = 3) -> list:
    """Folders under a root that no Sonarr series claims.

    Descends through category folders and reports show folders whole, rather than once per
    season and never as a whole category.
    """
    claimed = {normalise(path) for path in series_paths if path}
    orphans = []

    def descend(directory: Path, depth: int) -> None:
        try:
            children = sorted(entry for entry in directory.iterdir() if entry.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith('.'):
                continue
            here = normalise(child)
            if here in claimed:
                continue
            if looks_like_show(child):
                orphans.append(child)
                continue
            if depth < max_depth:
                descend(child, depth + 1)

    descend(Path(root), 1)
    return orphans


def action_scan_folders(settings, request):
    """Compare the library on disk against what Sonarr claims."""
    from core import validate_library_path
    root = validate_library_path(request.get('root') or '', 'Library folder')
    if not Path(root).is_dir():
        raise Rejected(f'{root} is not a folder on this server.')
    catalogue = []
    for instance in settings.get('instances', []):
        if instance.get('enabled', True):
            with contextlib.suppress(Rejected, SonarrError):
                catalogue.extend(catalogue_for(settings, instance['id']))
    by_tvdb = {entry['tvdb_id']: entry for entry in catalogue if entry.get('tvdb_id')}
    by_title = {}
    for entry in catalogue:
        by_title.setdefault(normalise_title(entry['title']), entry)
    ruled = {normalise(rule['path']) for rule in settings.get('rules', [])}

    found = []
    for folder in scan_library(Path(root), [entry['path'] for entry in catalogue]):
        size, files = folder_size(folder)
        found.append(dict(classify_orphan(folder.name, str(folder), by_tvdb, by_title),
                          path=str(folder), name=folder.name, size=size, files=files,
                          ruled=normalise(folder) in ruled))
    found.sort(key=lambda item: -item['size'])
    return {
        'root': root,
        'series_known': len(catalogue),
        'orphans': found[:500],
        'orphan_count': len(found),
        'orphan_bytes': sum(item['size'] for item in found),
    }


def action_remove_series(settings, request):
    """Delete a finished show from Sonarr and from disk, on explicit confirmation.

    The only action in this plugin that removes a whole series at once. It is refused
    while dry run is on, refused unless the operator has enabled it, and refused unless
    the show's title is typed back exactly.
    """
    rule = next((r for r in settings.get('rules', []) if r['id'] == str(request.get('rule_id') or '')), None)
    if not rule:
        raise Rejected('That rule no longer exists.')
    if settings.get('dry_run', True):
        raise Rejected('Dry run is on, so nothing is removed. Turn dry run off first if you '
                       'really mean to delete this show.')
    if not settings.get('allow_series_deletion'):
        raise Rejected('Deleting a whole show is switched off. Enable it in Schedule & safety '
                       'if you want this action.')
    if rule.get('match_status') != 'matched':
        raise Rejected('This rule is not matched to a Sonarr series.')
    typed = str(request.get('confirm_title') or '').strip()
    if typed.casefold() != str(rule.get('series_title') or '').strip().casefold():
        raise Rejected('The typed title did not match. Nothing was removed.')

    client = client_for(settings, rule['instance_id'])
    before = folder_size(Path(rule['path'])) if Path(rule['path']).is_dir() else (0, 0)
    client.delete_series(rule['series_id'], delete_files=True)
    settings['rules'] = [r for r in settings.get('rules', []) if r['id'] != rule['id']]
    save_settings(settings)

    health = load_health(settings)
    health['rules'].pop(rule['id'], None)
    write_cache(settings, 'health.json', health)
    ledger = load_ledger(settings)
    ledger.pop(rule['id'], None)
    save_ledger(settings, ledger)
    invalidate_catalogue(settings, rule['instance_id'])

    record = {'action': 'remove-series', 'at': now_iso(), 'series_title': rule.get('series_title'),
              'path': rule['path'], 'series_id': rule['series_id'], 'files': before[1], 'bytes': before[0]}
    journal(settings, record)
    notify(settings, 'TV Delete removed a show',
           f'{rule.get("series_title")} was deleted from Sonarr and disk '
           f'({before[1]} file(s), {before[0] / 1024 ** 3:.1f} GiB).', 'warning')
    return {'removed': record, 'settings': redact(load_settings())}


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
    'check-rule': action_check_rule,
    'settings': action_settings,
    'test-instance': action_test_instance,
    'detect-mappings': action_detect_mappings,
    'series': action_series,
    'monitoring': action_monitoring,
    'scan-folders': action_scan_folders,
    'remove-series': action_remove_series,
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
    if args.command == 'check':
        try:
            with run_lock(blocking=False):
                health = run_health_check(scheduled=getattr(args, 'scheduled', False))
        except Rejected as error:
            print(f'TV Delete: {error}', file=sys.stderr)
            return 1
        problems = health.get('problems') or []
        print(f'TV Delete checked {health.get("rules_checked", 0)} rule(s) in '
              f'{health.get("duration_seconds", 0)}s; {len(problems)} problem(s)')
        for message in problems:
            print(f'  ! {message}')
        return 0
    if args.command == 'resume':
        with contextlib.suppress(Rejected):
            write_cron(load_settings())
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(cli())
