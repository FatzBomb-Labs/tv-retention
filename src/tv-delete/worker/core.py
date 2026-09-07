#!/usr/bin/env python3
"""Pure logic for TV Delete: settings validation, retention evaluation, filesystem helpers.

Nothing in this module performs network access or deletes anything on its own. The
retention decision is deliberately separated from execution so it can be unit tested
against fixtures, and so the UI preview and the scheduled run share one code path.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
import uuid
from pathlib import Path

VERSION = '2026.09.06'
SETTINGS_VERSION = 1

# Extensions treated as episode media. Anything else in a season folder is a sidecar
# candidate or is left alone entirely.
MEDIA_EXTENSIONS = ['mkv', 'mp4', 'avi', 'mov', 'm4v', 'ts', 'wmv', 'mpg', 'mpeg']
# Sidecars are removed only when they sit beside a deleted episode and share its stem.
SIDECAR_EXTENSIONS = ['jpg', 'jpeg', 'png', 'nfo', 'txt', 'srt', 'sub', 'idx', 'ass', 'ssa', 'vtt', 'sup']

COMBINE_MODES = ['earliest', 'latest', 'any']

DEFAULTS = {
    'settings_version': SETTINGS_VERSION,
    'dry_run': True,
    'schedule': {'enabled': False, 'cron': '0 4 * * *'},
    'instances': [],
    # Named retention presets. A rule either points at one, or carries its own values.
    'profiles': [],
    'rules': [],
    'tmdb': {'enabled': False, 'api_key': ''},
    'guards': {
        'max_deletes_per_run': 200,
        'max_percent_per_rule': 50,
        'min_file_age_hours': 6,
    },
    'retention': {
        'include_specials': False,
        'allow_mtime_fallback': True,
        'unmonitor_deleted': True,
        # Off by default: re-monitoring an episode invites Sonarr to download it again.
        'remonitor_widened': False,
    },
    'sidecars': {'enabled': True, 'extensions': list(SIDECAR_EXTENSIONS)},
    'delete_empty_dirs': True,
    'recycle': {'mode': 'sonarr', 'path': '', 'retention_days': 14},
    'notify': True,
    # Run journals and the TMDB cache live on the array, not on the flash device.
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
    maps = []
    for entry in raw.get('path_maps') or []:
        if not isinstance(entry, dict):
            raise Rejected('Invalid path mapping')
        source = _text(entry.get('from'), 'Mapping "Sonarr path"', 1024)
        target = _text(entry.get('to'), 'Mapping "Unraid path"', 1024)
        if not source and not target:
            continue
        maps.append({'from': validate_path(source, 'Mapping "Sonarr path"'),
                     'to': validate_path(target, 'Mapping "Unraid path"')})
    return {
        'id': identifier,
        'name': _text(raw.get('name'), 'Instance name', 80, required=True),
        'url': validate_url(raw.get('url')),
        'api_key': key,
        'enabled': _flag(raw.get('enabled', True)),
        'verify_tls': _flag(raw.get('verify_tls', True)),
        'path_maps': maps,
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
        path=validate_library_path(raw.get('path'), 'Series folder'),
        unmonitor=_flag(raw.get('unmonitor', True)),
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
    guards_raw = raw.get('guards') or {}
    retention_raw = raw.get('retention') or {}
    sidecars_raw = raw.get('sidecars') or {}
    recycle_raw = raw.get('recycle') or {}
    tmdb_raw = raw.get('tmdb') or {}

    tmdb_key = _text(tmdb_raw.get('api_key'), 'TMDB API key', 128)
    if tmdb_key == '********':
        tmdb_key = previous_tmdb
    if tmdb_key and not re.match(r'^[A-Za-z0-9._\-]{16,128}$', tmdb_key):
        raise Rejected('TMDB API key looks malformed')

    extensions = []
    for value in sidecars_raw.get('extensions', SIDECAR_EXTENSIONS):
        value = _text(value, 'Sidecar extension', 12).lower().lstrip('.')
        if not value:
            continue
        if not re.match(r'^[a-z0-9]{1,10}$', value):
            raise Rejected(f'Invalid sidecar extension "{value}"')
        if value in MEDIA_EXTENSIONS:
            raise Rejected(f'"{value}" is a video extension and cannot be a sidecar')
        extensions.append(value)

    recycle_mode = _text(recycle_raw.get('mode'), 'Recycle mode', 16) or 'sonarr'
    if recycle_mode not in ('sonarr', 'plugin', 'none'):
        raise Rejected('Recycle mode must be sonarr, plugin, or none')
    recycle_path = _text(recycle_raw.get('path'), 'Recycle folder', 1024)
    if recycle_mode == 'plugin':
        recycle_path = validate_library_path(recycle_path, 'Recycle folder')

    settings = {
        'settings_version': SETTINGS_VERSION,
        'dry_run': _flag(raw.get('dry_run', True)),
        'schedule': {
            'enabled': _flag(schedule_raw.get('enabled', False)),
            'cron': validate_cron(schedule_raw.get('cron') or DEFAULTS['schedule']['cron']),
        },
        'instances': instances,
        'profiles': profiles,
        'rules': rules,
        'tmdb': {'enabled': _flag(tmdb_raw.get('enabled', False)) and bool(tmdb_key), 'api_key': tmdb_key},
        'guards': {
            'max_deletes_per_run': _whole(guards_raw.get('max_deletes_per_run', 200), 'Max deletes per run', 1, 100000, allow_none=False),
            'max_percent_per_rule': _whole(guards_raw.get('max_percent_per_rule', 50), 'Max percent per rule', 1, 100, allow_none=False),
            'min_file_age_hours': _whole(guards_raw.get('min_file_age_hours', 6), 'Minimum file age', 0, 8760, allow_none=False),
        },
        'retention': {
            'include_specials': _flag(retention_raw.get('include_specials', False)),
            'allow_mtime_fallback': _flag(retention_raw.get('allow_mtime_fallback', True)),
            'unmonitor_deleted': _flag(retention_raw.get('unmonitor_deleted', True)),
            'remonitor_widened': _flag(retention_raw.get('remonitor_widened', False)),
        },
        'sidecars': {'enabled': _flag(sidecars_raw.get('enabled', True)), 'extensions': sorted(set(extensions))},
        'delete_empty_dirs': _flag(raw.get('delete_empty_dirs', True)),
        'recycle': {
            'mode': recycle_mode,
            'path': recycle_path,
            'retention_days': _whole(recycle_raw.get('retention_days', 14), 'Recycle retention', 1, 3650, allow_none=False),
        },
        'notify': _flag(raw.get('notify', True)),
        'state_dir': validate_library_path(raw.get('state_dir') or DEFAULTS['state_dir'], 'State folder'),
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


def map_path(path: str, path_maps) -> str:
    """Translate a Sonarr (container) path to its Unraid host path.

    The longest matching prefix wins, so a specific mapping can override a broad one.
    """
    candidate = normalise(path)
    best = None
    for entry in path_maps or []:
        source = normalise(entry['from'])
        if candidate == source or candidate.startswith(source + '/'):
            if best is None or len(source) > len(normalise(best['from'])):
                best = entry
    if not best:
        return candidate
    source = normalise(best['from'])
    remainder = candidate[len(source):].lstrip('/')
    target = normalise(best['to'])
    return normalise(os.path.join(target, remainder) if remainder else target)


def unmap_path(path: str, path_maps) -> str:
    """Translate an Unraid host path back to the path Sonarr knows."""
    inverted = [{'from': entry['to'], 'to': entry['from']} for entry in path_maps or []]
    return map_path(path, inverted)


def derive_mappings(root_folders, mounts) -> list:
    """Work out a container-to-host path mapping from Sonarr's roots and Docker's mounts.

    Sonarr reports the paths it sees inside its container ("/tv/Series"); Docker knows
    which host directory is mounted there ("/mnt/user/media/TV" at "/tv"). Pairing the two
    gives the mapping without the operator having to type either path. Only the mounts
    that actually cover a Sonarr root folder are proposed, so unrelated volumes such as
    /config are ignored.
    """
    proposals = {}
    for folder in root_folders or []:
        target = normalise(folder)
        best = None
        for mount in mounts or []:
            destination = normalise(mount.get('destination') or '')
            source = normalise(mount.get('source') or '')
            if not destination or not source or destination == '/':
                continue
            if target == destination or target.startswith(destination + '/'):
                if best is None or len(destination) > len(normalise(best['destination'])):
                    best = mount
        if best:
            proposals[normalise(best['destination'])] = normalise(best['source'])
    return [{'from': source, 'to': target} for source, target in sorted(proposals.items())]


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def is_media(path: Path) -> bool:
    return path.suffix.lower().lstrip('.') in MEDIA_EXTENSIONS


def scan_media(root) -> list:
    """Every media file below a series folder, with size and mtime.

    Used to reconcile the library against Sonarr: files Sonarr does not know about are
    reported, never deleted.
    """
    found = []
    root = Path(root)
    if not root.is_dir():
        return found
    for base, directories, files in os.walk(root, onerror=lambda error: None):
        directories.sort()
        for name in sorted(files):
            path = Path(base) / name
            if not is_media(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append({'path': normalise(path), 'size': stat.st_size, 'mtime': stat.st_mtime})
    return found


def sidecars_for(path, extensions) -> list:
    """Sidecar files sharing an episode's stem, including language-tagged subtitles.

    "Show - S01E01.mkv" also matches "Show - S01E01.en.srt" and "Show - S01E01-thumb.jpg",
    but never another episode's files.
    """
    path = Path(path)
    parent = path.parent
    stem = path.stem
    allowed = {e.lower().lstrip('.') for e in extensions or []}
    matches = []
    if not allowed or not parent.is_dir():
        return matches
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return matches
    for entry in entries:
        if entry == path or not entry.is_file():
            continue
        name = entry.name
        if not (name.startswith(stem + '.') or name.startswith(stem + '-')):
            continue
        extension = entry.suffix.lower().lstrip('.')
        if extension in allowed:
            matches.append(normalise(entry))
    return matches


def empty_directories(root) -> list:
    """Empty directories below a series folder, deepest first. The root is never listed."""
    root = Path(root)
    found = []
    if not root.is_dir():
        return found
    for base, directories, files in os.walk(root, topdown=False, onerror=lambda error: None):
        if normalise(base) == normalise(root):
            continue
        try:
            if not any(Path(base).iterdir()):
                found.append(normalise(base))
        except OSError:
            continue
    return found


# ---------------------------------------------------------------------------
# Retention evaluation
# ---------------------------------------------------------------------------

def effective_date(episode, allow_mtime_fallback):
    """The date an episode is judged by, and where that date came from.

    Sonarr's air date is authoritative; TMDB fills gaps Sonarr leaves blank; the file's
    modification time is a last resort and can be switched off entirely, in which case
    undated episodes are never deleted.
    """
    if episode.get('air_date'):
        value = episode['air_date']
        if isinstance(value, str):
            value = dt.date.fromisoformat(value[:10])
        return value, episode.get('air_source') or 'sonarr'
    if allow_mtime_fallback and episode.get('mtime'):
        return dt.datetime.fromtimestamp(episode['mtime'], dt.timezone.utc).date(), 'mtime'
    return None, 'unknown'


def _order_key(episode, allow_mtime_fallback):
    date, _ = effective_date(episode, allow_mtime_fallback)
    return (date or dt.date.min, episode.get('season') or 0, episode.get('episode') or 0)


def evaluate(episodes, rule, settings, now=None) -> dict:
    """Decide, for one rule, which episode files to delete.

    Returns delete/keep/protected lists with a human-readable reason on every entry, so
    the preview in the UI and the audit journal show exactly why a file was chosen.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    retention = settings.get('retention', DEFAULTS['retention'])
    guards = settings.get('guards', DEFAULTS['guards'])
    allow_mtime = bool(retention.get('allow_mtime_fallback', True))
    include_specials = bool(retention.get('include_specials', False))
    min_age = dt.timedelta(hours=int(guards.get('min_file_age_hours', 0)))

    protected, candidates = [], []
    for episode in episodes:
        if not episode.get('path'):
            continue
        season = episode.get('season')
        if season == 0 and not include_specials:
            protected.append(dict(episode, reason='Specials (season 0) are excluded'))
            continue
        mtime = episode.get('mtime')
        if min_age and mtime and dt.datetime.fromtimestamp(mtime, dt.timezone.utc) > now - min_age:
            protected.append(dict(episode, reason=f'Modified within the last {guards.get("min_file_age_hours")}h'))
            continue
        candidates.append(episode)

    # Each condition votes 'keep', 'delete', or 'unknown' on every candidate.
    votes = {episode['path']: [] for episode in candidates}

    if rule.get('keep_days'):
        cutoff = (now - dt.timedelta(days=int(rule['keep_days']))).date()
        for episode in candidates:
            date, source = effective_date(episode, allow_mtime)
            if date is None:
                votes[episode['path']].append(('days', 'unknown', 'No air date available'))
            elif date >= cutoff:
                votes[episode['path']].append(('days', 'keep', f'Aired {date}, within {rule["keep_days"]} days'))
            else:
                votes[episode['path']].append(('days', 'delete', f'Aired {date} ({source}), older than {rule["keep_days"]} days'))

    if rule.get('keep_episodes'):
        ranked = sorted(candidates, key=lambda e: _order_key(e, allow_mtime), reverse=True)
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

    percent_cap = int(guards.get('max_percent_per_rule', 100))
    if candidates and percent_cap < 100:
        percent = 100.0 * len(delete) / len(candidates)
        if percent > percent_cap:
            result['blocked'] = (
                f'Guard stopped this rule: it would delete {len(delete)} of {len(candidates)} '
                f'episodes ({percent:.0f}%), above the {percent_cap}% limit. '
                'Check the rule and the Sonarr match, then raise the limit if this is intended.'
            )
            result['delete'] = []
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
    # The guards protect deletions, not this read-only comparison.
    relaxed = dict(settings, guards=dict(settings.get('guards', {}), max_percent_per_rule=100))
    decision = evaluate(combined, rule, relaxed, now=now)
    keepers = {item['path'] for item in decision['keep'] if item.get('from_ledger')}
    return [entry for entry, ghost in zip(ledger_entries, ghosts) if ghost['path'] in keepers]
