#!/usr/bin/env python3
"""Everything the plugin remembers, and nothing that decides anything.

The filesystem side of the worker: settings, journals, caches, the rolling log, and the
marker a running check publishes. No Sonarr and no judgements, so all of it can be
exercised against a temporary directory, and the modules that do talk to Sonarr have one
place to ask what was already known.

Both object caches hold *mapped* objects, so what is stored is keyed by the shape of the
mapping as well as by age. That is why SCHEMA carries the mapping's own fingerprint:
teaching the mapping a new field retires what is stored, instead of the new field reading
as absent everywhere until each entry happens to expire.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
from pathlib import Path

from core import CACHE_SCHEMA, DEFAULTS, Rejected, atomic_json, validate_settings
from migrate import migrate
from sonarr import MAPPING_SCHEMA

# The number covers everything the caches hold; the fingerprint covers the shape of what
# Sonarr's mapping produces. Either moving retires both caches.
SCHEMA = f'{CACHE_SCHEMA}.{MAPPING_SCHEMA}'

NAME = 'tv-delete'
CONFIG = Path(os.environ.get('TVD_CONFIG', f'/boot/config/plugins/{NAME}/settings.json'))
CRON = Path(os.environ.get('TVD_CRON', f'/boot/config/plugins/{NAME}/schedule.cron'))
RUNTIME = Path(os.environ.get('TVD_RUNTIME', f'/var/run/{NAME}'))
DEVELOPMENT = os.environ.get('TVD_DEVELOPMENT') == '1'
UPDATE_CRON = '/usr/local/sbin/update_cron'
NOTIFY = '/usr/local/emhttp/webGui/scripts/notify'

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
    """Read settings, upgrading and normalising them to the current shape.

    Validation runs on load, not only on save. Merging a stored document over the defaults
    leaves rules exactly as they were last written, so a field added since — the removal
    queue, for one — is simply absent, and the interface has nowhere to put it. Validating
    guarantees the shape in memory always matches the model regardless of the file's age.

    A document that cannot be validated is returned as it was rather than raising: the
    interface must still load so the problem can be seen and fixed.
    """
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
    try:
        return validate_settings(merged, previous=merged)
    except Rejected as error:
        print(f'tv-delete: stored settings did not validate ({error})', file=sys.stderr)
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
        path = cache_path(settings, name)
        # Names may carry a directory — one file per rule keeps a single series check
        # from rewriting every series' episodes.
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, value)
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



# ---------------------------------------------------------------------------
# Episodes, cached
# ---------------------------------------------------------------------------
# Retention is a pure function of a series' episodes and the rule over them: core.py
# takes no network by design. So a rule edited, a shared preset raised, or simply a day
# passing can all be answered from the episodes already read. Only a change in Sonarr
# needs Sonarr, which is what makes a re-check cheap enough to want to do often.

def episode_cache(settings: dict, rule: dict) -> tuple:
    """One rule's stored episodes and when they were read, or (None, None).

    Stored per rule rather than in one file: a single series check would otherwise
    rewrite every series' episodes, and on a fallback to flash that is a megabyte of
    writes to say one thing changed.
    """
    entry = read_cache(settings, f'episodes/{rule["id"]}.json')
    if entry.get('schema') != SCHEMA or entry.get('series_id') != rule.get('series_id'):
        return None, None, None
    return entry.get('episodes') or [], entry.get('series') or {}, entry.get('fetched_at')


def store_episodes(settings: dict, rule: dict, episodes: list, series: dict = None) -> str:
    stamp = now_iso()
    write_cache(settings, f'episodes/{rule["id"]}.json',
                {'schema': SCHEMA, 'series_id': rule.get('series_id'),
                 'instance_id': rule.get('instance_id'), 'fetched_at': stamp,
                 'episodes': episodes, 'series': series or {}})
    return stamp


def forget_episodes(settings: dict, rule_id: str) -> None:
    with contextlib.suppress(OSError):
        cache_path(settings, f'episodes/{rule_id}.json').unlink()



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


# ---------------------------------------------------------------------------
# Rolling log
# ---------------------------------------------------------------------------

# Five levels, ordered. `info` is what the plugin did — checks, sweeps, runs, saves — and
# is the default, because a log that only speaks when something breaks is an empty file
# that teaches nobody anything about a plugin that is working.
LOG_RANK = {'minimal': 0, 'error': 1, 'warning': 2, 'info': 3, 'verbose': 4}


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
                   if key not in ('in_frame_unmonitored', 'out_frame_monitored', 'newly_scoped_rows')}
        summary['in_frame_unmonitored_count'] = len(entry.get('in_frame_unmonitored') or [])
        summary['out_frame_monitored_count'] = len(entry.get('out_frame_monitored') or [])
        summary['newly_scoped_count'] = len(entry.get('newly_scoped_rows') or [])
        rules[rule_id] = summary
    trimmed['rules'] = rules
    return trimmed


def job_state(settings: dict) -> dict:
    """When each scheduled job last ran, and anything waiting to run."""
    stored = read_cache(settings, 'jobs.json')
    stored.setdefault('last_run', None)
    stored.setdefault('last_connectivity', None)
    stored.setdefault('pending_run', None)
    return stored


def save_job_state(settings: dict, state: dict) -> None:
    write_cache(settings, 'jobs.json', state)


