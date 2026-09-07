#!/usr/bin/env python3
"""Pure logic for TV Delete: settings validation, retention evaluation, filesystem helpers.

Nothing in this module performs network access or deletes anything on its own. The
retention decision is deliberately separated from execution so it can be unit tested
against fixtures, and so the UI preview and the scheduled run share one code path.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
import uuid
from pathlib import Path

import schedules

VERSION = '2026.09.06'
SETTINGS_VERSION = 4
# Bumped whenever anything cached changes shape — a health result, or the mapped series in
# the catalogue. Both caches store mapped objects, so a change to the mapping must retire
# them; otherwise a new field reads as absent until the cache happens to expire.
CACHE_SCHEMA = 7

# Extensions treated as episode media. Anything else in a season folder is a sidecar
# candidate or is left alone entirely.
MEDIA_EXTENSIONS = ['mkv', 'mp4', 'avi', 'mov', 'm4v', 'ts', 'wmv', 'mpg', 'mpeg']
# Sidecars are removed only when they sit beside a deleted episode and share its stem.
SIDECAR_EXTENSIONS = ['jpg', 'jpeg', 'png', 'nfo', 'txt', 'srt', 'sub', 'idx', 'ass', 'ssa', 'vtt', 'sup']

COMBINE_MODES = ['earliest', 'latest', 'any']

DEFAULTS = {
    'settings_version': SETTINGS_VERSION,
    'schedule': {'enabled': False,
                 # On by default, so enabling a schedule cannot delete anything until the
                 # operator has watched a run go through and switched this off.
                 'test_mode': True,
                 'frequency': 'daily', 'minute': 0, 'hour': 4,
                 'weekday': 0, 'monthly_mode': 'day', 'monthly_day': 1,
                 'monthly_weekday': '', 'cron': '0 4 * * *'},
    'health': {
        # Verifies every rule still resolves to one Sonarr series at the expected path.
        'series_match': {'enabled': True, 'frequency': 'daily', 'minute': 0, 'hour': 5,
                         'weekday': 0, 'monthly_mode': 'day', 'monthly_day': 1,
                         'monthly_weekday': '', 'cron': '0 5 * * *'},
        # How often to confirm Sonarr is reachable. Also checked before any job that needs it.
        'connectivity_seconds': 300,
        'ttl_hours': 24,
    },
    'catalogue_ttl_minutes': 60,
    'instances': [],
    # Named retention presets. A rule either points at one, or carries its own values.
    'profiles': [],
    'rules': [],
    # An API key is the switch: nobody enters one they do not want used.
    'tmdb': {'api_key': ''},
    'retention': {
        # One decision: specials are kept, and counted in monitoring, together or not at all.
        'include_specials': False,
        # Sonarr's air date, then TMDB, then a date estimated from the episodes either
        # side. With this off, an episode none of those can date is never deleted.
        'allow_estimated_dates': True,
        # Monitoring an episode does not fetch it until Sonarr's next RSS pass. Searching
        # closes that gap, and can turn a metadata change into a great many downloads.
        'search_after_monitor': False,
    },
    'notifications': {
        'run_started': False,
        'run_completed': True,
        'series_removed': True,
        'series_ended': True,
        'health_ok': False,
        'health_problems': True,
        'errors': True,
    },
    'logging': {'level': 'warning', 'max_bytes': 2 * 1024 * 1024},
    # Run journals, logs and caches live on the array, not on the flash device.
    'state_dir': '/mnt/user/appdata/tv-delete',
    'log_retention_runs': 50,
}

CRON_FIELD = re.compile(r'^[0-9*/,\-]+$')


class Rejected(Exception):
    """A request that failed validation or a guard. Surfaced verbatim in the UI."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def atomic_json(path: Path, value) -> None:
    """Write JSON so a crash or a power loss can never leave a half-written config."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f'.tmp{os.getpid()}')
    try:
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

def _text(value, field, limit=512, required=False) -> str:
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise Rejected(f'{field} must be text')
    value = value.strip()
    if len(value) > limit:
        raise Rejected(f'{field} is too long')
    if required and not value:
        raise Rejected(f'{field} is required')
    return value


def _whole(value, field, low, high, allow_none=True):
    if value in (None, '', 'null'):
        if allow_none:
            return None
        raise Rejected(f'{field} is required')
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Rejected(f'{field} must be a whole number')
    try:
        number = int(str(value).strip())
    except ValueError:
        raise Rejected(f'{field} must be a whole number')
    if not low <= number <= high:
        raise Rejected(f'{field} must be between {low} and {high}')
    return number


def _flag(value) -> bool:
    return value in (True, 'true', 'True', 1, '1', 'on', 'yes')


def _tristate(value, field):
    """A per-rule override: yes, no, or inherit the global setting."""
    if value in (None, '', 'inherit', 'default'):
        return None
    if value in (True, 'true', 'True', 1, '1', 'yes', 'on'):
        return True
    if value in (False, 'false', 'False', 0, '0', 'no', 'off'):
        return False
    raise Rejected(f'{field} must be yes, no, or inherit')


def validate_cron(expression: str) -> str:
    """Accept the five-field cron subset Unraid's crontab understands."""
    expression = _text(expression, 'Schedule', 120, required=True)
    fields = expression.split()
    if len(fields) != 5:
        raise Rejected('Schedule must have five cron fields, for example "0 4 * * *"')
    for field in fields:
        if not CRON_FIELD.match(field):
            raise Rejected(f'Unsupported cron field "{field}"')
    return ' '.join(fields)


def validate_url(url: str) -> str:
    url = _text(url, 'Sonarr URL', 300, required=True).rstrip('/')
    if not re.match(r'^https?://[^\s/][^\s]*$', url):
        raise Rejected('Sonarr URL must start with http:// or https://')
    return url


def validate_path(value, field='Folder') -> str:
    """Absolute, normalised, no traversal. Deletion targets must be under /mnt."""
    value = _text(value, field, 1024, required=True)
    if '\x00' in value:
        raise Rejected(f'{field} contains an invalid character')
    if not value.startswith('/'):
        raise Rejected(f'{field} must be an absolute path')
    # Reject traversal in the value as typed. Normalising first would quietly rewrite
    # "/mnt/user/media/../../etc" into a different, still-valid-looking folder.
    if '..' in Path(value).parts:
        raise Rejected(f'{field} may not contain ".."')
    return os.path.normpath(value)


def validate_library_path(value, field='Folder') -> str:
    path = validate_path(value, field)
    if not (path.startswith('/mnt/') and len(Path(path).parts) > 3):
        raise Rejected(f'{field} must be a share path under /mnt, at least three levels deep')
    return path


LOG_LEVELS = ['minimal', 'error', 'warning', 'verbose']


def validate_schedule(raw, field='Schedule') -> dict:
    """A structured schedule. Cron is one option among several, not the storage format."""
    raw = raw or {}
    frequency = _text(raw.get('frequency'), f'{field} frequency', 16) or 'daily'
    if frequency not in schedules.FREQUENCIES:
        raise Rejected(f'{field} frequency must be one of: {", ".join(schedules.FREQUENCIES)}')
    monthly_mode = _text(raw.get('monthly_mode'), f'{field} monthly mode', 16) or 'day'
    if monthly_mode not in schedules.MONTHLY_MODES:
        raise Rejected(f'{field} monthly mode must be day, first, or last')
    weekday = raw.get('monthly_weekday')
    monthly_weekday = '' if weekday in (None, '', 'any') else _whole(weekday, f'{field} weekday', 0, 6, allow_none=False)
    result = {
        'enabled': _flag(raw.get('enabled', False)),
        'frequency': frequency,
        'minute': _whole(raw.get('minute', 0), f'{field} minute', 0, 59, allow_none=False),
        'hour': _whole(raw.get('hour', 0), f'{field} hour', 0, 23, allow_none=False),
        'weekday': _whole(raw.get('weekday', 0), f'{field} day of week', 0, 6, allow_none=False),
        'monthly_mode': monthly_mode,
        # Capped at 28 so every month has the day; 29-31 would silently skip months.
        'monthly_day': _whole(raw.get('monthly_day', 1), f'{field} day of month', 1, 28, allow_none=False),
        'monthly_weekday': monthly_weekday,
        'cron': _text(raw.get('cron'), f'{field} cron', 120) or '0 4 * * *',
    }
    if frequency == 'custom':
        try:
            schedules.cron_matches(result['cron'], dt.datetime(2026, 1, 1))
        except schedules.ScheduleError as error:
            raise Rejected(f'{field}: {error}')
    return result


def validate_instance(raw, existing_keys=None) -> dict:
    if not isinstance(raw, dict):
        raise Rejected('Invalid Sonarr instance')
    identifier = _text(raw.get('id'), 'Instance id', 32) or new_id()
    key = _text(raw.get('api_key'), 'API key', 128)
    # The UI never receives real keys; it echoes a mask, which means "keep the stored key".
    if key in ('', '********') and existing_keys:
        key = existing_keys.get(identifier, '')
    if not key:
        raise Rejected('A Sonarr API key is required')
    if not re.match(r'^[A-Za-z0-9]{16,128}$', key):
        raise Rejected('Sonarr API keys are 16+ letters and digits')
    return {
        'id': identifier,
        'name': _text(raw.get('name'), 'Instance name', 80, required=True),
        'url': validate_url(raw.get('url')),
        'api_key': key,
        'enabled': _flag(raw.get('enabled', True)),
        'verify_tls': _flag(raw.get('verify_tls', True)),
        # Set by a successful connection test.
        'verified_at': _text(raw.get('verified_at'), 'Verified at', 40),
    }


def validate_conditions(raw, field_prefix='') -> dict:
    """The three retention numbers plus the combine mode, shared by rules and profiles."""
    keep_days = _whole(raw.get('keep_days'), f'{field_prefix}Keep days'.strip(), 1, 36500)
    keep_episodes = _whole(raw.get('keep_episodes'), f'{field_prefix}Keep episodes'.strip(), 1, 100000)
    keep_seasons = _whole(raw.get('keep_seasons'), f'{field_prefix}Keep seasons'.strip(), 1, 1000)
    combine = _text(raw.get('combine'), 'Combine mode', 16) or 'earliest'
    if combine not in COMBINE_MODES:
        raise Rejected('Combine mode must be earliest, latest, or any')
    return {'keep_days': keep_days, 'keep_episodes': keep_episodes,
            'keep_seasons': keep_seasons, 'combine': combine}


def validate_profile(raw) -> dict:
    """A named preset such as "Keep 30 days", reusable across any number of shows."""
    if not isinstance(raw, dict):
        raise Rejected('Invalid retention preset')
    conditions = validate_conditions(raw, 'Preset ')
    if all(conditions[key] is None for key in ('keep_days', 'keep_episodes', 'keep_seasons')):
        raise Rejected('A preset needs at least one of: keep days, keep episodes, keep seasons')
    return dict(conditions,
                id=_text(raw.get('id'), 'Preset id', 32) or new_id(),
                name=_text(raw.get('name'), 'Preset name', 80, required=True))


def profile_label(profile: dict) -> str:
    """A short description of a preset, for menus and reports."""
    parts = []
    if profile.get('keep_days'):
        parts.append(f'{profile["keep_days"]}d')
    if profile.get('keep_episodes'):
        parts.append(f'{profile["keep_episodes"]} eps')
    if profile.get('keep_seasons'):
        parts.append(f'{profile["keep_seasons"]} seasons')
    return f'{profile.get("name", "")} ({", ".join(parts)}, {profile.get("combine", "earliest")})'


def effective_rule(rule: dict, profiles) -> dict:
    """Resolve a rule to the conditions a run should apply.

    A rule that names a preset takes every condition from it, so raising a shared
    "Keep 30 days" preset to 90 days widens every show using it at once. A rule with no
    preset carries its own values.
    """
    resolved = dict(rule)
    if rule.get('profile_id'):
        profile = next((p for p in profiles or [] if p['id'] == rule['profile_id']), None)
        if not profile:
            raise Rejected(f'Rule "{rule.get("series_title") or rule.get("path")}" uses a '
                           'retention preset that no longer exists.')
        resolved.update({key: profile.get(key) for key in
                         ('keep_days', 'keep_episodes', 'keep_seasons', 'combine')})
        resolved['profile_name'] = profile['name']
    return resolved


# What a queued removal asks Sonarr to do on its way out. The plugin never deletes a
# series itself: options five and six ask Sonarr to, so its bookkeeping and its recycle
# bin apply. Everything the plugin removes on its own is still individual episode files.
REMOVAL_ACTIONS = {
    'remove': 'Leave the series untouched in Sonarr',
    'monitor-all': 'Set the entire series to monitored',
    'unmonitor-all': 'Set the entire series to unmonitored',
    'monitor-in-frame': 'Set only episodes inside the keep window to monitored',
    'delete-series': 'Ask Sonarr to delete the series, keeping the files',
    'delete-series-files': 'Ask Sonarr to delete the series and its files',
}
# What has to be typed for the two that destroy something.
REMOVAL_CONFIRMATIONS = {'delete-series': 'DELETE', 'delete-series-files': 'DELETE ALL'}
QUEUED_FIXES = ['monitor-in-frame', 'unmonitor-out-frame']


def validate_queue(raw) -> dict:
    """A rule's pending intent. Nothing here has happened yet; a run is what applies it."""
    raw = raw if isinstance(raw, dict) else {}
    queue = {'removal': None, 'fixes': []}
    removal = raw.get('removal')
    if isinstance(removal, dict) and removal.get('action'):
        action = _text(removal.get('action'), 'Removal action', 32)
        if action not in REMOVAL_ACTIONS:
            raise Rejected(f'Unknown removal action "{action}"')
        queue['removal'] = {'action': action,
                            'created_at': _text(removal.get('created_at'), 'Queued at', 40) or ''}
    for entry in raw.get('fixes') or []:
        kind = _text((entry or {}).get('kind'), 'Queued fix', 32)
        if kind not in QUEUED_FIXES:
            raise Rejected(f'Unknown queued fix "{kind}"')
        if any(existing['kind'] == kind for existing in queue['fixes']):
            continue
        queue['fixes'].append({'kind': kind,
                               'created_at': _text((entry or {}).get('created_at'), 'Queued at', 40) or ''})
    return queue


def validate_rule(raw, instance_ids, profile_ids=()) -> dict:
    if not isinstance(raw, dict):
        raise Rejected('Invalid rule')
    instance_id = _text(raw.get('instance_id'), 'Sonarr instance', 32, required=True)
    if instance_id not in instance_ids:
        raise Rejected('This rule points at a Sonarr instance that no longer exists')
    profile_id = _text(raw.get('profile_id'), 'Retention preset', 32)
    if profile_id and profile_id not in profile_ids:
        raise Rejected('This rule points at a retention preset that no longer exists')
    conditions = validate_conditions(raw)
    if not profile_id and all(conditions[key] is None for key in ('keep_days', 'keep_episodes', 'keep_seasons')):
        raise Rejected('A rule needs a retention preset, or at least one of: '
                       'keep days, keep episodes, keep seasons')
    series_id = _whole(raw.get('series_id'), 'Sonarr series id', 1, 2 ** 31 - 1)
    rule = dict(
        conditions,
        id=_text(raw.get('id'), 'Rule id', 32) or new_id(),
        enabled=_flag(raw.get('enabled', True)),
        instance_id=instance_id,
        profile_id=profile_id,
        series_id=series_id,
        series_title=_text(raw.get('series_title'), 'Series title', 300),
        tvdb_id=_whole(raw.get('tvdb_id'), 'TVDB id', 1, 2 ** 31 - 1),
        slug=_text(raw.get('slug'), 'Sonarr slug', 200),
        # Sonarr's own path, stored for display and for matching. The plugin no longer
        # resolves it locally: Sonarr owns the filesystem.
        path=validate_path(raw.get('path'), 'Series folder'),
        # Per show, because one series' specials are worth keeping and another's are not.
        include_specials=_tristate(raw.get('include_specials'), 'Include specials'),
        # Monitoring episodes inside the window that have no file starts downloads, so it
        # is a decision per series rather than a global default.
        monitor_missing=_flag(raw.get('monitor_missing', False)),
        queue=validate_queue(raw.get('queue')),
        # Match state is owned by the backend; the UI cannot assert a rule is matched.
        match_status='matched' if series_id else 'unmatched',
        match_error=_text(raw.get('match_error'), 'Match error', 500),
        matched_at=_text(raw.get('matched_at'), 'Matched at', 40),
    )
    if profile_id:
        # Preset-driven rules store no numbers of their own, so there is one source of truth.
        rule.update({'keep_days': None, 'keep_episodes': None, 'keep_seasons': None, 'combine': 'earliest'})
    if not rule['series_id']:
        rule['match_status'] = 'unmatched'
        rule['match_error'] = rule['match_error'] or 'Not linked to a Sonarr series yet'
    return rule


def validate_settings(raw, previous=None) -> dict:
    """Return a fully normalised settings document, or raise Rejected with a UI message."""
    if not isinstance(raw, dict):
        raise Rejected('Invalid settings payload')
    previous = previous or {}
    existing_keys = {i['id']: i.get('api_key', '') for i in previous.get('instances', []) if isinstance(i, dict) and i.get('id')}
    previous_tmdb = (previous.get('tmdb') or {}).get('api_key', '')

    instances = []
    seen = set()
    for entry in raw.get('instances') or []:
        instance = validate_instance(entry, existing_keys)
        if instance['id'] in seen:
            raise Rejected('Duplicate Sonarr instance id')
        seen.add(instance['id'])
        instances.append(instance)
    if len({i['name'].lower() for i in instances}) != len(instances):
        raise Rejected('Sonarr instance names must be unique')

    profiles = []
    profile_ids = set()
    for entry in raw.get('profiles') or []:
        profile = validate_profile(entry)
        if profile['id'] in profile_ids:
            raise Rejected('Duplicate retention preset id')
        profile_ids.add(profile['id'])
        profiles.append(profile)
    if len({p['name'].lower() for p in profiles}) != len(profiles):
        raise Rejected('Retention preset names must be unique')

    rules = []
    rule_ids = set()
    for entry in raw.get('rules') or []:
        rule = validate_rule(entry, seen, profile_ids)
        if rule['id'] in rule_ids:
            raise Rejected('Duplicate rule id')
        rule_ids.add(rule['id'])
        rules.append(rule)
    paths = [r['path'] for r in rules]
    if len(set(paths)) != len(paths):
        raise Rejected('Two rules point at the same folder; merge them instead')

    schedule_raw = raw.get('schedule') or {}
    health_raw = raw.get('health') or {}
    retention_raw = raw.get('retention') or {}
    tmdb_raw = raw.get('tmdb') or {}
    notify_raw = raw.get('notifications') or {}
    logging_raw = raw.get('logging') or {}

    tmdb_key = _text(tmdb_raw.get('api_key'), 'TMDB API key', 128)
    if tmdb_key == '********':
        tmdb_key = previous_tmdb
    if tmdb_key and not re.match(r'^[A-Za-z0-9._\-]{16,128}$', tmdb_key):
        raise Rejected('TMDB API key looks malformed')

    level = _text(logging_raw.get('level'), 'Log level', 16) or 'warning'
    if level not in LOG_LEVELS:
        raise Rejected(f'Log level must be one of: {", ".join(LOG_LEVELS)}')

    settings = {
        'settings_version': SETTINGS_VERSION,
        'schedule': dict(validate_schedule(schedule_raw, 'Schedule'),
                         test_mode=_flag(schedule_raw.get('test_mode', True))),
        'health': {
            # Always on: a rule that no longer resolves to a Sonarr series must not be
            # allowed to act, so the check that notices is not something to switch off.
            # Only its cadence is configurable, and it runs before any job regardless.
            'series_match': dict(validate_schedule(health_raw.get('series_match') or
                                                   DEFAULTS['health']['series_match'],
                                                   'Series match schedule'), enabled=True),
            'connectivity_seconds': _whole(health_raw.get('connectivity_seconds', 300),
                                           'Connectivity check', 30, 86400, allow_none=False),
            'ttl_hours': _whole(health_raw.get('ttl_hours', 24), 'Health cache lifetime', 1, 720, allow_none=False),
        },
        'catalogue_ttl_minutes': _whole(raw.get('catalogue_ttl_minutes', 60), 'Series list cache', 1, 10080, allow_none=False),
        'instances': instances,
        'profiles': profiles,
        'rules': rules,
        'tmdb': {'api_key': tmdb_key},
        'retention': {
            'include_specials': _flag(retention_raw.get('include_specials', False)),
            'allow_estimated_dates': _flag(retention_raw.get('allow_estimated_dates', True)),
            'search_after_monitor': _flag(retention_raw.get('search_after_monitor', False)),
        },
        'notifications': {name: _flag(notify_raw.get(name, default))
                          for name, default in DEFAULTS['notifications'].items()},
        'logging': {
            'level': level,
            'max_bytes': _whole(logging_raw.get('max_bytes', 2 * 1024 * 1024), 'Log size', 65536, 64 * 1024 * 1024, allow_none=False),
        },
        'state_dir': validate_library_path(raw.get('state_dir') or DEFAULTS['state_dir'], 'App storage folder'),
        'log_retention_runs': _whole(raw.get('log_retention_runs', 50), 'History size', 1, 500, allow_none=False),
    }
    return settings


def redact(settings: dict) -> dict:
    """Settings as shown to the browser: secrets replaced by a mask the UI echoes back."""
    copy = json.loads(json.dumps(settings))
    for instance in copy.get('instances', []):
        instance['api_key'] = '********' if instance.get('api_key') else ''
    if copy.get('tmdb', {}).get('api_key'):
        copy['tmdb']['api_key'] = '********'
    return copy


# ---------------------------------------------------------------------------
# Path mapping between a Sonarr container and Unraid shares
# ---------------------------------------------------------------------------

def normalise(path: str) -> str:
    """Compare paths the way the filesystem stores them.

    Series folders routinely contain typographic apostrophes and accents. Sonarr may
    report a different Unicode normal form than the one on disk, so both sides are
    folded to NFC before comparison; without this, matching silently fails on titles
    such as "That's My Jam".
    """
    return unicodedata.normalize('NFC', os.path.normpath(str(path))).rstrip('/') or '/'






# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Retention evaluation
# ---------------------------------------------------------------------------

def import_date(episode):
    """When Sonarr imported the file, as a date."""
    stamp = episode.get('date_added')
    if stamp:
        try:
            return dt.date.fromisoformat(str(stamp)[:10])
        except ValueError:
            pass
    if episode.get('mtime'):
        return dt.datetime.fromtimestamp(episode['mtime'], dt.timezone.utc).date()
    return None


def interpolate_air_dates(episodes) -> int:
    """Estimate missing air dates from the episodes either side of them.

    Sonarr's import date is a poor substitute for an air date: re-importing an episode at
    better quality resets it, so a 2015 episode upgraded last week looks like it arrived
    last week. Its neighbours do not lie that way. An undated episode sits between the
    episodes before and after it in season and episode order, and a date interpolated
    between their air dates is both stable and close to the truth.

    Specials are left out of the ordering because they do not sit in sequence with the
    numbered episodes, and estimates are marked as such so a preview can show where a
    decision rests on one. Returns how many were filled.
    """
    ordered = sorted((episode for episode in episodes if (episode.get('season') or 0) > 0),
                     key=lambda episode: (episode.get('season') or 0, episode.get('episode') or 0))
    known = [index for index, episode in enumerate(ordered) if episode.get('air_date')]
    if not known:
        return 0
    filled = 0
    for index, episode in enumerate(ordered):
        if episode.get('air_date'):
            continue
        before = max((position for position in known if position < index), default=None)
        after = min((position for position in known if position > index), default=None)
        if before is not None and after is not None:
            start = dt.date.fromisoformat(ordered[before]['air_date'][:10])
            end = dt.date.fromisoformat(ordered[after]['air_date'][:10])
            share = (index - before) / (after - before)
            estimate = start + dt.timedelta(days=round((end - start).days * share))
        elif before is not None:
            estimate = dt.date.fromisoformat(ordered[before]['air_date'][:10])
        else:
            estimate = dt.date.fromisoformat(ordered[after]['air_date'][:10])
        episode['air_date'] = estimate.isoformat()
        episode['air_source'] = 'estimated'
        filled += 1
    return filled


def effective_date(episode, allow_import_fallback):
    """The date an episode is judged by, and where that date came from.

    In order: Sonarr's air date, then TMDB, then a date interpolated from the episodes
    either side, then the date Sonarr imported the file. The import date is genuinely last
    because re-importing an upgrade resets it, and it can be switched off entirely, in
    which case an undated episode is never deleted.
    """
    if episode.get('air_date'):
        value = episode['air_date']
        if isinstance(value, str):
            value = dt.date.fromisoformat(value[:10])
        return value, episode.get('air_source') or 'sonarr'
    if allow_import_fallback:
        added = import_date(episode)
        if added:
            return added, 'imported'
    return None, 'unknown'


def _order_key(episode, allow_import_fallback):
    date, _ = effective_date(episode, allow_import_fallback)
    return (date or dt.date.min, episode.get('season') or 0, episode.get('episode') or 0)




def evaluate(episodes, rule, settings, now=None) -> dict:
    """Decide, for one rule, which episode files to delete.

    Returns delete/keep/protected lists with a human-readable reason on every entry, so
    the preview in the UI and the audit journal show exactly why a file was chosen.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    retention = settings.get('retention') or DEFAULTS['retention']
    allow_estimates = bool(retention.get('allow_estimated_dates', True))
    # A rule may override the global specials decision; None means inherit it.
    include_specials = rule.get('include_specials')
    if include_specials is None:
        include_specials = bool(retention.get('include_specials', False))

    protected, candidates = [], []
    for episode in episodes:
        if not episode.get('path'):
            continue
        if episode.get('season') == 0 and not include_specials:
            protected.append(dict(episode, reason='Specials (season 0) are excluded'))
            continue
        candidates.append(episode)

    # Each condition votes 'keep', 'delete', or 'unknown' on every candidate.
    votes = {episode['path']: [] for episode in candidates}

    if rule.get('keep_days'):
        cutoff = (now - dt.timedelta(days=int(rule['keep_days']))).date()
        for episode in candidates:
            date, source = effective_date(episode, allow_estimates)
            if date is None:
                votes[episode['path']].append(('days', 'unknown', 'No air date available'))
            elif date >= cutoff:
                votes[episode['path']].append(('days', 'keep', f'Aired {date}, within {rule["keep_days"]} days'))
            else:
                votes[episode['path']].append(('days', 'delete', f'Aired {date} ({source}), older than {rule["keep_days"]} days'))

    if rule.get('keep_episodes'):
        ranked = sorted(candidates, key=lambda e: _order_key(e, allow_estimates), reverse=True)
        keep_set = {e['path'] for e in ranked[:int(rule['keep_episodes'])]}
        for episode in candidates:
            if episode['path'] in keep_set:
                votes[episode['path']].append(('episodes', 'keep', f'Among the newest {rule["keep_episodes"]} episodes'))
            else:
                votes[episode['path']].append(('episodes', 'delete', f'Not among the newest {rule["keep_episodes"]} episodes'))

    if rule.get('keep_seasons'):
        seasons = sorted({e.get('season') for e in candidates if e.get('season') is not None}, reverse=True)
        keep_seasons = set(seasons[:int(rule['keep_seasons'])])
        for episode in candidates:
            season = episode.get('season')
            if season in keep_seasons:
                votes[episode['path']].append(('seasons', 'keep', f'Season {season} is among the newest {rule["keep_seasons"]} seasons'))
            else:
                votes[episode['path']].append(('seasons', 'delete', f'Season {season} is older than the newest {rule["keep_seasons"]} seasons'))

    combine = rule.get('combine', 'earliest')
    delete, keep = [], []
    for episode in candidates:
        cast = votes[episode['path']]
        verdicts = [v for _, v, _ in cast]
        if not cast:
            keep.append(dict(episode, reason='No retention condition applied'))
            continue
        if combine == 'earliest':
            # Keep the most: any condition that would keep the file, or cannot judge it, wins.
            remove = all(v == 'delete' for v in verdicts)
        elif combine == 'latest':
            # Keep the least, but never act on a condition that could not be evaluated.
            remove = all(v == 'delete' for v in verdicts) and 'unknown' not in verdicts
        else:  # 'any'
            remove = 'delete' in verdicts
        detail = '; '.join(f'{name}: {reason}' for name, _, reason in cast)
        (delete if remove else keep).append(dict(episode, reason=detail))

    result = {
        'rule_id': rule.get('id'),
        'delete': delete,
        'keep': keep,
        'protected': protected,
        'considered': len(candidates),
        'blocked': None,
    }

    return result


def select_remonitor(episodes, ledger_entries, rule, settings, now=None) -> list:
    """Which previously-deleted episodes now fall back inside a widened rule.

    When a rule's keep window grows — usually because a shared preset was raised from 30
    to 90 days — episodes this plugin unmonitored may belong in the library again. They
    are judged by replaying the current rule over the episodes still on disk *plus* the
    ledger entries, so "keep the newest 20 episodes" counts the missing ones in their
    proper order rather than pretending they never existed.

    Only episodes this plugin unmonitored are ever considered; anything the operator
    unmonitored by hand is untouched, because it was never written to the ledger.
    """
    if not ledger_entries:
        return []
    ghosts = []
    for entry in ledger_entries:
        ghost = dict(entry)
        ghost['path'] = entry.get('path') or f'ledger:{entry.get("episode_id")}'
        ghost['from_ledger'] = True
        ghosts.append(ghost)
    combined = list(episodes) + ghosts
    decision = evaluate(combined, rule, settings, now=now)
    keepers = {item['path'] for item in decision['keep'] if item.get('from_ledger')}
    return [entry for entry, ghost in zip(ledger_entries, ghosts) if ghost['path'] in keepers]


MONITOR_STATUS = {
    'empty': 'Sonarr lists no episodes',
    'aligned': 'In frame monitored, outside unmonitored',
    'all_monitored': 'All episodes monitored',
    'outside_monitored': 'Episodes outside the keep frame are still monitored',
    'in_frame_unmonitored': 'Episodes inside the keep frame are unmonitored',
    'mixed': 'Monitoring does not match the keep frame',
}


def classify_monitoring(episodes, rule, settings, now=None) -> dict:
    """Compare Sonarr's monitored flags against a rule's keep frame.

    This reads Sonarr live every time, so anything monitored or unmonitored by hand shows
    up immediately — including the state a library is in before this plugin has ever run.
    Episodes that have not aired yet are always treated as inside the frame: you want the
    next episode, whatever the retention rule says about the old ones.

    Specials are left out of the comparison unless asked for. A special is not part of a
    "keep the last two seasons" decision, and counting them would make the corrective
    actions sweep every special into whatever the rest of the show is doing — which is
    rarely what anyone means.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    retention = settings.get('retention') or {}
    # One decision, shared with deletion: a special that is kept is a special that counts.
    include_specials = rule.get('include_specials')
    if include_specials is None:
        include_specials = bool(retention.get('include_specials', False))

    specials = []
    considered = []
    for episode in episodes:
        if episode.get('season') == 0 and not include_specials:
            specials.append(episode)
        else:
            considered.append(episode)

    unaired, aired = [], []
    for episode in considered:
        date, _ = effective_date(episode, allow_import_fallback=False)
        # No air date and no file means Sonarr does not know when it airs; treat it as
        # forthcoming rather than as something to strip the monitoring from.
        if date is None or date > today:
            unaired.append(episode)
        else:
            aired.append(episode)

    # The percentage guard exists to stop mass deletion, not to limit a read-only view.
    decision = evaluate(aired, rule, settings, now=now)
    out_frame = decision['delete']
    in_frame = decision['keep'] + decision['protected'] + unaired

    files_in_frame = [e for e in in_frame if e.get('has_file')]
    files_total = [e for e in considered if e.get('has_file')]
    in_frame_unmonitored = [e for e in in_frame if not e.get('monitored')]
    out_frame_monitored = [e for e in out_frame if e.get('monitored')]
    monitored = [e for e in considered if e.get('monitored')]

    if not considered:
        status = 'empty'
    elif not in_frame_unmonitored and not out_frame_monitored:
        status = 'aligned'
    elif len(monitored) == len(considered):
        status = 'all_monitored'
    elif in_frame_unmonitored and out_frame_monitored:
        status = 'mixed'
    elif out_frame_monitored:
        status = 'outside_monitored'
    else:
        status = 'in_frame_unmonitored'

    def summarise(entries):
        return [{'episode_id': e.get('episode_id'), 'season': e.get('season'),
                 'episode': e.get('episode'), 'title': e.get('title'),
                 'air_date': e.get('air_date'), 'has_file': e.get('has_file', True)}
                for e in sorted(entries, key=lambda e: (e.get('season') or 0, e.get('episode') or 0))]

    return {
        'status': status,
        'label': MONITOR_STATUS[status],
        'total': len(considered),
        'specials_ignored': len(specials),
        'monitored': len(monitored),
        'in_frame': len(in_frame),
        'out_frame': len(out_frame),
        'unaired': len(unaired),
        'files_in_frame': len(files_in_frame),
        'files_total': len(files_total),
        'in_frame_unmonitored': summarise(in_frame_unmonitored),
        'out_frame_monitored': summarise(out_frame_monitored),
    }


def describe_selectability(entry, in_use: bool) -> dict:
    """Whether a Sonarr series can be given a rule, and why not when it cannot.

    Answered entirely from what Sonarr reports. The plugin no longer resolves paths on
    this server, so there is no longer a case where a series exists in Sonarr and the
    plugin disagrees about where it lives.
    """
    if not entry.get('path'):
        return {'selectable': False, 'awaiting': False, 'reason': 'no folder configured in Sonarr'}
    if in_use:
        return {'selectable': False, 'awaiting': False, 'reason': 'already used by another rule'}
    if not entry.get('episode_file_count'):
        return {'selectable': True, 'awaiting': True, 'reason': 'no episodes imported yet'}
    return {'selectable': True, 'awaiting': False, 'reason': ''}




def rule_fingerprint(rule: dict, settings: dict) -> str:
    """Identify everything that would move a rule's keep frame.

    A cached monitoring result stays valid only while this is unchanged. It covers the
    resolved retention values — so raising a shared preset invalidates every rule using it
    — together with the global settings that shift the frame. It deliberately says nothing
    about Sonarr's own state, which cannot be checked without asking Sonarr.
    """
    active = effective_rule(rule, settings.get('profiles'))
    retention = settings.get('retention') or {}

    material = {
        'cache_schema': CACHE_SCHEMA,
        'keep_days': active.get('keep_days'),
        'keep_episodes': active.get('keep_episodes'),
        'keep_seasons': active.get('keep_seasons'),
        'combine': active.get('combine'),
        'include_specials': active.get('include_specials'),
        'path': active.get('path'),
        'series_id': active.get('series_id'),
        'global_specials': retention.get('include_specials'),
        'monitor_missing': active.get('monitor_missing'),
        'estimated_dates': retention.get('allow_estimated_dates'),

    }
    return hashlib.sha256(canonical_json(material).encode('utf-8')).hexdigest()[:16]


def describe_lifecycle(state: dict, series: dict) -> dict:
    """Whether a show has finished and its retention has run out.

    A series Sonarr marks as ended, with nothing left inside the keep frame, will never
    gain another episode and is no longer being kept for anything. That is worth saying
    plainly, because the rule will otherwise sit there for ever doing nothing.
    """
    ended = bool(series.get('ended')) or series.get('status') == 'ended'
    if not ended:
        return {'ended': False, 'retention_expired': False, 'lifecycle': ''}
    if state.get('files_total', 0) == 0:
        return {'ended': True, 'retention_expired': True, 'lifecycle': 'ended_empty'}
    if state.get('files_in_frame', 0) == 0:
        return {'ended': True, 'retention_expired': True, 'lifecycle': 'ended_expired'}
    return {'ended': True, 'retention_expired': False, 'lifecycle': 'ended'}
