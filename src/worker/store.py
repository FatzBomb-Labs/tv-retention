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
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # Windows filesystem-only tests; the container path is Linux.
    fcntl = None

from core import CACHE_SCHEMA, DEFAULTS, Rejected, StorageError, atomic_json, validate_settings
from migrate import UnsupportedVersion, migrate
from sonarr import MAPPING_SCHEMA

# The number covers everything the caches hold; the fingerprint covers the shape of what
# Sonarr's mapping produces. Either moving retires both caches.
SCHEMA = f'{CACHE_SCHEMA}.{MAPPING_SCHEMA}'

NAME = 'tv-retention'
# One volume. Settings, caches, the journal and the poster cache all live under it, so a
# backup is a directory and a migration is a copy. TVR_CONFIG stays overridable because
# every test and every live read-only check points it somewhere disposable.
CONFIG_DIR = Path(os.environ.get('TVR_CONFIG_DIR', '/config'))
CONFIG = Path(os.environ.get('TVR_CONFIG', CONFIG_DIR / 'settings.json'))
RUNTIME = Path(os.environ.get('TVR_RUNTIME', '/tmp/tv-retention'))
DEVELOPMENT = os.environ.get('TVR_DEVELOPMENT') == '1'
_settings_thread_lock = threading.RLock()
_settings_transaction_state = threading.local()


@contextlib.contextmanager
def settings_transaction():
    """Serialize short settings read/validate/write transactions across worker processes."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    depth = getattr(_settings_transaction_state, 'depth', 0)
    if depth:
        _settings_transaction_state.depth = depth + 1
        try:
            yield
        finally:
            _settings_transaction_state.depth = depth
        return
    if fcntl is None:
        with _settings_thread_lock:
            _settings_transaction_state.depth = 1
            try:
                yield
            finally:
                _settings_transaction_state.depth = 0
        return
    with open(RUNTIME / 'settings.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _settings_transaction_state.depth = 1
        try:
            yield
        finally:
            _settings_transaction_state.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)

# ---------------------------------------------------------------------------
# Configuration and state
# ---------------------------------------------------------------------------

def default_state_dir() -> str:
    """Where journals and caches go: beside the settings, inside the one volume.

    There is nothing to discover. The plugin read Unraid's docker.cfg to find the appdata
    share because it had to place its working data on a system it did not own; a container
    is given its volume.
    """
    return str(CONFIG_DIR / 'state')


def _load_settings(strict: bool = False) -> dict:
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
    try:
        stored = migrate(stored if isinstance(stored, dict) else {})
    except UnsupportedVersion as error:
        raise Rejected(f'{error} ({CONFIG})') from error
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(stored)
    if not stored.get('state_dir'):
        merged['state_dir'] = default_state_dir()
    try:
        return validate_settings(merged, previous=merged)
    except Rejected as error:
        if strict:
            raise Rejected(f'Saved settings are invalid ({error}). Fix {CONFIG}.') from error
        print(f'tv-retention: stored settings did not validate ({error})', file=sys.stderr)
        return merged


def load_settings() -> dict:
    """Load settings for read-only views, retaining a repairable document on failure."""
    return _load_settings()


def load_settings_strict() -> dict:
    """Load settings for writes and execution, refusing damaged authority."""
    return _load_settings(strict=True)




def save_settings(settings: dict) -> None:
    # The flag is retained for test fixtures and local diagnostics; it never changes the
    # write contract (Test Mode is a settings value, not an environment shortcut).
    with settings_transaction():
        save_settings_unlocked(settings)


def save_settings_unlocked(settings: dict) -> None:
    """Write settings while the caller already owns settings_transaction()."""
    if DEVELOPMENT:
        pass
    settings['settings_revision'] = int(settings.get('settings_revision') or 0) + 1
    try:
        atomic_json(CONFIG, settings)
    except OSError as error:
        raise StorageError(f'Could not persist settings.json: {error}') from error


def save_settings_if_current(settings: dict, expected_revision: int, owned_fields=(), writer=None) -> None:
    """Commit a background update unless a newer edit changed an owned field."""
    with settings_transaction():
        latest = load_settings_strict()
        newer = latest.get('settings_revision', 0) > expected_revision
        if newer and not owned_fields:
            raise Rejected('Settings changed while a background update was in progress; '
                           'the newer settings were preserved.')
        if newer:
            for rule_id, field, expected in owned_fields:
                target = next((rule for rule in latest.get('rules') or []
                               if rule.get('id') == rule_id), None)
                if target is not None and target.get(field) != expected:
                    raise Rejected('Settings changed while a background update was in progress; '
                                   'one of its owned fields changed, so the newer settings were preserved.')
            latest_rules = {rule.get('id'): rule for rule in latest.get('rules') or []}
            source_rules = {rule.get('id'): rule for rule in settings.get('rules') or []}
            for rule_id, field, _expected in owned_fields:
                target = latest_rules.get(rule_id)
                source = source_rules.get(rule_id)
                if target is not None and source is not None:
                    target[field] = source.get(field)
            settings = latest
        (writer or save_settings_unlocked)(settings)


# How long a directory that just proved writable is trusted without probing again. This
# function runs on every cache read, cache write, log line and journal append, so writing
# and unlinking a probe file on every single call was steady churn worth avoiding on
# flash-backed storage — but the reason the probe exists at all is to notice the array
# going down, so it is a cache with a short lifetime rather than a fact learned once and
# believed forever.
WRITABLE_RECHECK_SECONDS = 60
_writable_since: dict[str, float] = {}


def state_dir(settings: dict, required: bool = False) -> Path:
    """Where journals and caches live. Falls back to the flash config if the array is down."""
    directory = Path(settings.get('state_dir') or DEFAULTS['state_dir'])
    key = str(directory)
    now = time.monotonic()
    if now - _writable_since.get(key, -WRITABLE_RECHECK_SECONDS) < WRITABLE_RECHECK_SECONDS:
        return directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / '.writable'
        probe.write_text('')
        probe.unlink()
        _writable_since[key] = now
        return directory
    except OSError as error:
        _writable_since.pop(key, None)
        if required:
            raise StorageError(f'Configured state directory is unavailable ({directory}): {error}') from error
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


def load_state_strict(settings: dict) -> dict:
    """Load authoritative run history, refusing damaged content."""
    path = state_dir(settings, required=True) / 'state.json'
    if not path.exists():
        return {'runs': [], 'last_run': None}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Saved run history is unreadable ({error}). Fix or remove {path}.') from error
    if not isinstance(value, dict) or not isinstance(value.get('runs'), list):
        raise Rejected(f'Saved run history is invalid ({path} must contain a runs list).')
    if value.get('last_run') is not None and not isinstance(value.get('last_run'), dict):
        raise Rejected(f'Saved run history is invalid ({path} has no valid last_run).')
    for run in value['runs']:
        if not isinstance(run, dict) or not isinstance(run.get('id'), str) or not run['id']:
            raise Rejected(f'Saved run history is invalid ({path} has a malformed run).')
    return value




def save_state(settings: dict, state: dict, required: bool = True) -> None:
    keep = int(settings.get('log_retention_runs', 50))
    state['runs'] = state.get('runs', [])[-keep:]
    try:
        atomic_json(state_dir(settings, required=required) / 'state.json', state)
    except OSError as error:
        if required:
            raise StorageError(f'Could not persist state.json: {error}') from error


def _run_record(summary: dict) -> dict:
    return {
        'id': summary['id'], 'started': summary['started'], 'finished': summary['finished'],
        'scheduled': summary.get('scheduled', False), 'dry_run': False,
        'planned': summary.get('planned', 0), 'deleted': summary.get('deleted', 0),
        'freed_bytes': summary.get('freed_bytes', 0), 'errors': summary.get('errors', [])[:10],
    }


def _journal_unlocked(settings: dict, record: dict) -> None:
    """Append one audit record while the caller owns settings_transaction()."""
    path = state_dir(settings, required=True) / 'journal.jsonl'
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def record_run(settings: dict, summary: dict) -> bool:
    """Record one completed run exactly once across retries and restarts."""
    record = _run_record(summary)
    with settings_transaction():
        state = load_state_strict(settings)
        runs = list(state.get('runs') or [])
        already_state = any(run.get('id') == record['id'] for run in runs)
        if not already_state:
            runs.append(record)
        state['runs'] = runs
        state['last_run'] = summary
        journal_path = state_dir(settings, required=True) / 'journal.jsonl'
        known = False
        if journal_path.exists():
            try:
                with open(journal_path, encoding='utf-8') as handle:
                    known = any(json.loads(line).get('id') == record['id']
                                for line in handle if line.strip())
            except (OSError, json.JSONDecodeError):
                known = False
        if not known:
            _journal_unlocked(settings, summary)
        save_state(settings, state)
    return not already_state or not known


def load_intent(settings: dict) -> dict | None:
    """The last staged run, if a real run was interrupted before it finished.

    This is deliberately separate from the append-only journal. The intent records
    operation checkpoints, not continuing permission: unfinished ordinary-only decisions
    are archived before fresh planning. Explicit/mixed removal recovery is separate.
    """
    path = state_dir(settings) / 'run-intent.json'
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_intent_strict(settings: dict) -> dict | None:
    """Load the executable recovery record, refusing damaged content."""
    path = state_dir(settings, required=True) / 'run-intent.json'
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Saved run intent is unreadable ({error}). Fix or remove {path}.') from error
    if not isinstance(value, dict):
        raise Rejected(f'Saved run intent is invalid ({path} must contain an object).')
    try:
        _validate_intent(value)
    except Rejected as error:
        raise Rejected(f'Saved run intent is invalid ({error}). Fix or remove {path}.') from error
    return value


def _validate_intent(intent: dict) -> None:
    """Validate the fields recovery may use as executable authority."""
    if not isinstance(intent.get('id'), str) or not intent['id']:
        raise Rejected('the intent has no run ID')
    if intent.get('status') not in {'staged', 'incomplete', 'complete'}:
        raise Rejected('the intent has an unknown status')
    for field in ('operations', 'rules', 'removals', 'recovery_removals'):
        if field in intent and not isinstance(intent[field], list):
            raise Rejected(f'{field} is not a list')
    if 'summary' in intent and not isinstance(intent['summary'], dict):
        raise Rejected('summary is not an object')
    for operation in intent.get('operations') or []:
        if not isinstance(operation, dict):
            raise Rejected('an operation is not an object')
        for field in ('id', 'kind', 'rule_id', 'instance_id'):
            if not isinstance(operation.get(field), str) or not operation[field]:
                raise Rejected(f'an operation has no valid {field}')
        if not isinstance(operation.get('series_id'), int) or isinstance(operation['series_id'], bool):
            raise Rejected('an operation has no valid series_id')
        if operation.get('kind') not in {
                'set-monitored', 'delete-episode-file', 'delete-series', 'remove-rule'}:
            raise Rejected('an operation has an unknown kind')
        if operation.get('status') not in {'pending', 'in-progress', 'done', 'failed', 'resolved'}:
            raise Rejected('an operation has an unknown status')
        attempts = operation.get('attempts')
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise Rejected('an operation has an invalid attempt count')
        kind = operation['kind']
        if kind == 'set-monitored':
            if not isinstance(operation.get('episode_ids'), list):
                raise Rejected('a monitoring operation has no episode list')
            if not isinstance(operation.get('monitored'), bool):
                raise Rejected('a monitoring operation has no monitored value')
        elif kind == 'delete-episode-file':
            if not isinstance(operation.get('file_id'), int) or isinstance(operation['file_id'], bool):
                raise Rejected('a file operation has no valid file_id')
            if not isinstance(operation.get('episode_id'), int) or isinstance(operation['episode_id'], bool):
                raise Rejected('a file operation has no valid episode_id')
            if not isinstance(operation.get('file_episode_ids'), list) or not operation['file_episode_ids']:
                raise Rejected('a file operation has no file membership')
        elif kind == 'delete-series' and not isinstance(operation.get('delete_files'), bool):
            raise Rejected('a series operation has no delete_files value')
        if operation.get('removal_action'):
            if not isinstance(operation.get('request_id'), str) or not operation['request_id']:
                raise Rejected('a removal operation has no request ID')
            if operation.get('target') is not None and not isinstance(operation.get('target'), dict):
                raise Rejected('a removal operation has no target snapshot')




def save_intent(settings: dict, intent: dict) -> None:
    """Durably replace the run intent before or after one external write."""
    try:
        atomic_json(state_dir(settings, required=True) / 'run-intent.json', intent)
    except OSError as error:
        raise StorageError(f'Could not persist run-intent.json: {error}') from error


def archive_intent(settings: dict, intent: dict) -> None:
    """Preserve exact checkpoints before replacing abandoned ordinary work.

    Content addressing makes repeated attempts idempotent without trusting an intent ID
    as a path. Errors propagate: losing audit evidence must stop replacement and writes.
    This is historical data only, never an executable queue or a success summary.
    """
    digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode('utf-8')).hexdigest()
    try:
        atomic_json(state_dir(settings, required=True) / 'run-history' / f'{digest}.json', intent)
    except OSError as error:
        raise StorageError(f'Could not archive run intent: {error}') from error


def load_removal_ledger(settings: dict) -> dict:
    """Missing is new; unreadable or malformed executable history must fail closed."""
    path = state_dir(settings, required=True) / 'removal-ledger.json'
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {'version': 1, 'batches': []}
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Saved removal recovery ledger is unreadable ({error}). Fix or remove {path}.') from error
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('batches'), list)
            or any(not isinstance(batch, dict) or not batch.get('id')
                   or not isinstance(batch.get('operations'), list)
                   or not isinstance(batch.get('removals'), list)
                   or any(not isinstance(op, dict) or not op.get('removal_action')
                          for op in batch['operations']) for batch in value['batches'])):
        raise ValueError('Invalid removal recovery ledger')
    return value


def save_removal_ledger(settings: dict, ledger: dict) -> None:
    """Persist one-time checkpoints independently of replaceable ordinary decisions."""
    try:
        atomic_json(state_dir(settings, required=True) / 'removal-ledger.json', ledger)
    except OSError as error:
        raise StorageError(f'Could not persist removal-ledger.json: {error}') from error


def journal(settings: dict, record: dict) -> None:
    """Append-only audit trail. One JSON object per line, never rewritten."""
    try:
        with settings_transaction():
            _journal_unlocked(settings, record)
    except OSError:
        pass


def read_journal(settings: dict) -> list:
    """Every run ever recorded, oldest first. A bad line is skipped, not fatal.

    The journal is the audit trail, so it is only ever appended to; anything that totals
    it reads it whole rather than keeping a second tally that could disagree.
    """
    path = state_dir(settings) / 'journal.jsonl'
    records = []
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

def cache_path(settings: dict, name: str, required: bool = False) -> Path:
    return state_dir(settings, required=required) / name


def read_cache(settings: dict, name: str) -> dict:
    path = cache_path(settings, name)
    if not path.exists():
        return {}
    try:
        stored = json.loads(path.read_text())
        return stored if isinstance(stored, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache(settings: dict, name: str, value: dict, required: bool = True) -> None:
    if name == 'health.json':
        with settings_transaction():
            latest = load_health_strict(settings)
            merged = dict(value)
            # Operator decisions do not belong to a background health reading. Keep
            # them from being erased by a stale check that finished later.
            for field in ('acknowledged', 'suppressed'):
                merged[field] = latest.get(field, merged.get(field, {}))
            _write_cache_unlocked(settings, name, merged, required=True)
        return
    _write_cache_unlocked(settings, name, value, required=required)


def _write_cache_unlocked(settings: dict, name: str, value: dict, required: bool = True) -> None:
    try:
        path = cache_path(settings, name, required=required)
        # Names may carry a directory — one file per rule keeps a single series check
        # from rewriting every series' episodes.
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, value)
    except OSError as error:
        if required:
            raise StorageError(f'Could not persist {name}: {error}') from error


def update_health(settings: dict, update) -> dict:
    """Apply one operator health-cache update to the newest document."""
    with settings_transaction():
        health = load_health_strict(settings)
        result = update(health)
        _write_cache_unlocked(settings, 'health.json', health)
        return health, result


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
    health.setdefault('suppressed', {})
    return health


def load_health_strict(settings: dict) -> dict:
    """Load health bookkeeping without treating a damaged cache as an empty reading."""
    path = cache_path(settings, 'health.json', required=True)
    if not path.exists():
        return {'rules': {}, 'instances': {}, 'suppressed': {}}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Saved health cache is unreadable ({error}). Fix or remove {path}.') from error
    if not isinstance(value, dict):
        raise Rejected(f'Saved health cache is invalid ({path} must contain an object).')
    for field in ('rules', 'instances', 'suppressed', 'acknowledged'):
        if field in value and not isinstance(value[field], dict):
            raise Rejected(f'Saved health cache is invalid ({field} is not an object).')
    if 'alerts' in value and not isinstance(value['alerts'], list):
        raise Rejected(f'Saved health cache is invalid (alerts is not a list).')
    if 'dirty' in value and not isinstance(value['dirty'], list):
        raise Rejected(f'Saved health cache is invalid (dirty is not a list).')
    value.setdefault('rules', {})
    value.setdefault('instances', {})
    value.setdefault('suppressed', {})
    return value




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
    zone_name = (settings.get('schedule') or {}).get('timezone') or DEFAULTS['schedule']['timezone']
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = dt.timezone.utc
    stamp = dt.datetime.now(zone).strftime('%Y-%m-%d %H:%M:%S')
    prefix = ''
    line = f'{stamp} [{level.upper()}] {prefix}{message}\n'
    try:
        path = state_dir(settings) / 'tv-retention.log'
        limit = int((settings.get('logging') or {}).get('max_bytes', 2 * 1024 * 1024))
        if path.exists() and path.stat().st_size + len(line) > limit:
            path.replace(path.with_suffix('.log.1'))
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(line)
    except OSError:
        pass


def read_log(settings: dict, offset: int = 0, limit: int = 65536) -> dict:
    """A slice of the log for the viewer, addressed by byte offset so polling is cheap."""
    path = state_dir(settings) / 'tv-retention.log'
    try:
        size = path.stat().st_size
    except OSError:
        return {'offset': 0, 'size': 0, 'text': ''}
    # A rotation moves the file out from under the reader; start again from the beginning.
    if offset > size:
        offset = 0
    if offset <= 0:
        offset = max(0, size - limit)
    # Read as bytes and decode afterwards, rather than seeking and reading a text stream:
    # `read(limit)` on a text file reads `limit` *characters*, and re-encoding what came
    # back to count the bytes consumed only agrees with what was actually read as long as
    # every byte decoded cleanly. A malformed byte under `errors='replace'` becomes one
    # replacement character that re-encodes to three bytes while consuming as few as one,
    # so the offset drifted and the next poll could skip text or repeat it. A byte offset
    # computed from bytes read cannot drift, whatever is in them.
    try:
        with open(path, 'rb') as handle:
            handle.seek(offset)
            raw = handle.read(limit)
    except OSError:
        return {'offset': 0, 'size': size, 'text': ''}
    return {'offset': offset + len(raw), 'size': size, 'text': raw.decode('utf-8', errors='replace')}




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
    write_cache(settings, 'checking.json', progress, required=False)


def clear_progress(settings: dict) -> None:
    write_cache(settings, 'checking.json', {'running': False, 'finished': now_iso()}, required=False)



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
    stored.setdefault('last_occurrence', None)
    return stored


def job_state_strict(settings: dict) -> dict:
    """Load scheduler bookkeeping, refusing damaged pending-run authority."""
    path = cache_path(settings, 'jobs.json', required=True)
    if not path.exists():
        return {'last_run': None, 'last_connectivity': None, 'pending_run': None,
            'last_occurrence': None}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Saved scheduler state is unreadable ({error}). Fix or remove {path}.') from error
    if not isinstance(value, dict):
        raise Rejected(f'Saved scheduler state is invalid ({path} must contain an object).')
    for field in ('last_run', 'last_connectivity', 'pending_run', 'last_occurrence'):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise Rejected(f'Saved scheduler state is invalid ({field} is not a timestamp).')
    return dict(value, last_run=value.get('last_run'),
                last_connectivity=value.get('last_connectivity'),
                pending_run=value.get('pending_run'),
                last_occurrence=value.get('last_occurrence'))




def save_job_state(settings: dict, state: dict) -> None:
    write_cache(settings, 'jobs.json', state, required=True)


def integrity_errors(settings: dict) -> dict:
    """Every authoritative document that will not load, keyed by its alert kind.

    Five near-identical `*_error` helpers used to sit next to the five strict readers,
    each wrapping one in the same try/except, and the single caller repeated the same
    three lines five times to turn them into alerts. The readers genuinely differ —
    what a damaged run history looks like is not what a damaged health cache looks
    like — but asking them does not, and neither does reporting the answer.

    Returning the alert kind rather than a bare string is what removes the last of the
    repetition at the call site, and it puts the document-to-kind mapping somewhere a
    test can see it.
    """
    readers = (
        ('settings-invalid', lambda _: load_settings_strict()),
        ('intent-invalid', load_intent_strict),
        ('state-invalid', load_state_strict),
        ('jobs-invalid', job_state_strict),
        ('health-invalid', load_health_strict),
    )
    found = {}
    for kind, read in readers:
        try:
            read(settings)
        except Rejected as error:
            found[kind] = str(error)
    return found
