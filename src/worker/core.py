#!/usr/bin/env python3
"""Pure logic for TV Retention: settings validation, retention evaluation, filesystem helpers.

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
import urllib.parse
import uuid
from pathlib import Path

import schedules

VERSION = '0.3.0'
# One number, owned by the module that knows the steps. Held separately once, and the two
# disagreed: the migration ran and then validation stamped the document back to the older
# version, so every load migrated it again.
from migrate import SETTINGS_VERSION  # noqa: E402
# Bumped whenever anything cached changes shape — a health result, or the mapped series in
# the catalogue. Both caches store mapped objects, so a change to the mapping must retire
# them; otherwise a new field reads as absent until the cache happens to expire.
CACHE_SCHEMA = 8

# Extensions treated as episode media. Anything else in a season folder is a sidecar
# candidate or is left alone entirely.
COMBINE_MODES = ['any', 'all']

DEFAULTS = {
    'settings_version': SETTINGS_VERSION,
    'settings_revision': 0,
    'schedule': {'enabled': False,
                 # On by default, so enabling a schedule cannot delete anything until the
                 # operator has watched a run go through and switched this off.
                 'test_mode': True,
                 'frequency': 'daily', 'minute': 0, 'hour': 4,
                 'weekday': 0, 'monthly_mode': 'day', 'monthly_day': 1,
                 'monthly_weekday': '', 'cron': '0 4 * * *'},
    # How old a reading may get before it is read again. The only knob here: the sweep
    # that honours it is spread across the ticks, and confirming Sonarr answers happens on
    # its own fixed interval and before anything that needs it — neither was ever a
    # decision anyone could make better than the plugin can.
    'health': {'ttl_hours': 24},
    'catalogue_ttl_minutes': 60,
    'instances': [],
    # Named retention presets. A rule either points at one, or carries its own values.
    'profiles': [],
    'rules': [],
    # An API key is the switch: nobody enters one they do not want used.
    'tmdb': {'api_key': ''},
    # Optional enrichment connections.  Sonarr remains the required source of truth;
    # these entries only fill missing metadata and are inert until enabled.
    'connections': {
        'tmdb': {'name': 'TMDB', 'url': 'https://api.themoviedb.org/3',
                 'api_key': '', 'enabled': False, 'verify_tls': True, 'verified_at': ''},
        'plex': {'name': 'Plex', 'url': '', 'token': '', 'enabled': False,
                 'verify_tls': True, 'verified_at': ''},
        'jellyfin': {'name': 'Jellyfin', 'url': '', 'api_key': '', 'enabled': False,
                     'verify_tls': True, 'verified_at': ''},
    },
    # Metadata only. The full secret is returned once by the create action and is never
    # persisted or included in a snapshot.
    'api_key': {'status': 'not_created', 'hash': '', 'prefix': '',
                'created_at': '', 'revoked_at': ''},
    'backup': {'path': '', 'keep': 5, 'last': '', 'last_error': ''},
    # What a run does without being asked. Every one of these is a decision that used to
    # be taken silently by the code, which meant the only way to find out what it was was
    # to read the code.
    'automation': {
        # Monitoring an episode does not fetch it until Sonarr's next RSS pass. Searching
        # closes that gap, and can turn a metadata change into a great many downloads.
        'search_after_monitor': False,
        # Exclusions. One gate, evaluated once, and everything that decides what a run may
        # not touch goes through it — including specials, which had a branch of their own
        # and so were a second gate saying the same kind of thing in a different place.
        #
        # Excluding specials is the safety it always was, and now it is a visible one: it
        # appears on the series card beside every other cause rather than quietly removing
        # fifteen episodes from consideration. Any series can still opt out, which is what
        # its own `include_specials` now means.
        'exclude_specials': True,
        # Whole seasons, by number.
        'exclude_seasons': [],
        # Two lists, because they are two questions. A season folder is a container and an
        # episode is a thing in it, and one box matching both cannot express "exclude the
        # Extras folder but not an episode whose title happens to say extras".
        'exclude_folders': [],
        'exclude_episodes': [],
    },
    # Where an air date comes from when Sonarr has none, and what happens when nothing can
    # supply one. It matters more than its size suggests: a series with unresolvable dates
    # cannot be kept by age at all, so this is the difference between a rule that works and
    # one that silently processes nothing.
    #
    # TVDB is deliberately absent. It is Sonarr's own metadata source, so if Sonarr has no
    # air date then TVDB has none either, and asking again costs a paid v4 key to learn
    # nothing.
    'air_dates': {
        # Order is priority: the first enabled provider that answers, wins.
        'providers': ['tmdb', 'tvmaze', 'anilist', 'imdb', 'plex', 'jellyfin'],
        'enabled': ['tmdb'],
        'unresolved': 'estimate',        # estimate | leave
        'still_unresolved': 'exclude',   # exclude | disable
    },
    'retention': {
        # Sonarr's air date, then a provider, then a date estimated from the episodes
        # either side. With this off, an episode none of those can date is never deleted.
        # This is `air_dates.unresolved` said in the shape `evaluate` reads.
        'allow_estimated_dates': True,
    },
    'alerts': {},
    'logging': {'level': 'info', 'max_bytes': 2 * 1024 * 1024},
    # Run journals, logs and caches live on the array, not on the flash device.
    'state_dir': '/config/state',
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
    temp = path.with_suffix(path.suffix + f'.tmp{os.getpid()}.{uuid.uuid4().hex}')
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


def validate_text(value, field, limit=512, required=False) -> str:
    """`_text`, named for callers outside this module.

    Every other validator here is reached the same way, by a `validate_` name; a caller
    in `actions.py` reaching for `_text` directly was the one exception, into a helper
    this module never promised to keep in that exact shape.
    """
    return _text(value, field, limit, required)


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


def _retention_days(value, field):
    """Normalise a retention age written as days or as a d/w/m/y duration."""
    if value in (None, '', 'null'):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Rejected(f'{field} must be a whole number of days, or use d, w, m, or y')
    match = re.fullmatch(r'([1-9]\d*)\s*([dwmy]?)', str(value).strip(), re.IGNORECASE)
    if not match:
        raise Rejected(f'{field} must be a whole number of days, or use d, w, m, or y')
    amount = int(match.group(1))
    days = amount * {'': 1, 'd': 1, 'w': 7, 'm': 30, 'y': 365}[match.group(2).lower()]
    if days > 36500:
        raise Rejected(f'{field} must not exceed 36500 days')
    return days


def _flag(value) -> bool:
    return value in (True, 'true', 'True', 1, '1', 'on', 'yes')


def _choice(value, allowed, field, allow_blank=False) -> str:
    value = _text(value, field, 32)
    if not value and allow_blank:
        return ''
    if value not in allowed:
        raise Rejected(f'{field} must be one of {", ".join(allowed)}')
    return value


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


def validate_optional_url(value, field='Connection URL') -> str:
    """An optional provider URL: blank is allowed until that provider is enabled."""
    url = _text(value, field, 512)
    if not url:
        return ''
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise Rejected(f'{field} must be an http:// or https:// URL')
    return url.rstrip('/')


OPTIONAL_CONNECTIONS = {
    'tmdb': {'name': 'TMDB', 'credential': 'api_key',
             'default_url': 'https://api.themoviedb.org/3'},
    'plex': {'name': 'Plex', 'credential': 'token', 'default_url': ''},
    'jellyfin': {'name': 'Jellyfin', 'credential': 'api_key', 'default_url': ''},
}
API_KEY_STATES = ('not_created', 'created', 'revoked')


def _connection_map(raw) -> dict:
    """Accept the keyed map used by the app and a list from early API clients."""
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, list):
        result = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get('kind') or entry.get('type') or entry.get('id') or '')
            if kind:
                result[kind] = entry
        return result
    return {}


def validate_connection(kind: str, raw, previous=None) -> dict:
    """Validate one optional provider without ever requiring a credential for a disabled one."""
    meta = OPTIONAL_CONNECTIONS[kind]
    raw = raw if isinstance(raw, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    credential = meta['credential']
    value = _text(raw.get(credential), f'{meta["name"]} credential', 512)
    if value in ('********', '••••••••'):
        value = _text(previous.get(credential), f'{meta["name"]} credential', 512)
    enabled = _flag(raw.get('enabled', bool(value)))
    url = validate_optional_url(raw.get('url') or meta['default_url'], f'{meta["name"]} URL')
    if enabled and not value:
        raise Rejected(f'{meta["name"]} needs a credential before it can be enabled')
    if enabled and kind != 'tmdb' and not url:
        raise Rejected(f'{meta["name"]} needs a URL before it can be enabled')
    if value and len(value) > 512:
        raise Rejected(f'{meta["name"]} credential is too long')
    result = {
        'name': meta['name'], 'url': url, credential: value,
        'enabled': enabled, 'verify_tls': _flag(raw.get('verify_tls', True)),
        'verified_at': _text(raw.get('verified_at'), f'{meta["name"]} verified at', 40),
    }
    # Preserve non-secret provider metadata so a future provider can add fields without
    # making the current normalizer throw it away.
    if kind == 'plex':
        result['server_name'] = _text(raw.get('server_name'), 'Plex server name', 120)
    return result


def validate_api_key(raw, previous=None) -> dict:
    """Normalize persistent API-key metadata; the secret itself is never a setting value."""
    raw = raw if isinstance(raw, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    status = _text(raw.get('status') or previous.get('status') or 'not_created',
                   'API key status', 20)
    if status not in API_KEY_STATES:
        raise Rejected('API key status is invalid')
    digest = _text(raw.get('hash'), 'API key hash', 128)
    if not digest:
        digest = _text(previous.get('hash'), 'API key hash', 128)
    prefix = _text(raw.get('prefix') or previous.get('prefix'), 'API key prefix', 32)
    created = _text(raw.get('created_at') or previous.get('created_at'), 'API key created at', 40)
    revoked = _text(raw.get('revoked_at') or previous.get('revoked_at'), 'API key revoked at', 40)
    if status == 'created' and (not digest or not prefix or not created):
        # A redacted browser document may omit the hash; the previous value above is the
        # authoritative copy. A genuinely incomplete stored document is safer as revoked.
        if previous.get('status') == 'created' and previous.get('hash'):
            digest = digest or previous['hash']
            prefix = prefix or previous.get('prefix', '')
            created = created or previous.get('created_at', '')
        else:
            status = 'revoked'
    return {'status': status, 'hash': digest, 'prefix': prefix,
            'created_at': created, 'revoked_at': revoked}


def validate_backup(raw, state_path='') -> dict:
    """Normalize backup configuration; path safety is checked again at execution time."""
    raw = raw if isinstance(raw, dict) else {}
    path = _text(raw.get('path'), 'Backup destination', 1024)
    if path:
        if not path.startswith('/'):
            raise Rejected('Backup destination must be an absolute path')
        if '..' in Path(path).parts:
            raise Rejected('Backup destination may not contain ".."')
        path = os.path.normpath(path)
        if state_path and (path == state_path or Path(path).is_relative_to(Path(state_path)) or
                           Path(state_path).is_relative_to(Path(path))):
            raise Rejected('Backup destination must not contain the active state directory')
    keep = _whole(raw.get('keep', 5), 'Backup retention', 1, 100, allow_none=False)
    return {'path': path, 'keep': keep,
            'last': _text(raw.get('last'), 'Last backup', 40),
            'last_error': _text(raw.get('last_error'), 'Last backup error', 500)}


def validate_state_dir(value, field='App storage folder') -> str:
    """Somewhere absolute to keep caches and the journal.

    The plugin required `/mnt/<share>/<folder>` because on Unraid that was the difference
    between a user share and the flash device, and writing caches to flash wears it out.
    A container is handed its volume, so the only thing left to insist on is that the path
    is absolute — `validate_path` already refuses anything that could climb out of it.
    """
    path = validate_path(value, field)
    if len(Path(path).parts) < 2:
        raise Rejected(f'{field} must be a path, not the filesystem root')
    return path


LOG_LEVELS = ['minimal', 'error', 'warning', 'info', 'verbose']

# Kept here rather than imported from alerts, which imports nothing and is imported by
# everything. The blocking ones cannot be muted: hiding "this series will not run" does
# not stop it being true, it only stops you finding out why.
BLOCKING_KINDS = ['unmatched', 'sonarr-unreachable']


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
    keep_days = _retention_days(raw.get('keep_days'), f'{field_prefix}Age'.strip())
    keep_episodes = _whole(raw.get('keep_episodes'), f'{field_prefix}Keep episodes'.strip(), 1, 100000)
    keep_seasons = _whole(raw.get('keep_seasons'), f'{field_prefix}Keep seasons'.strip(), 1, 1000)
    combine = _text(raw.get('combine'), 'Keep mode', 16) or 'any'
    if combine not in COMBINE_MODES:
        raise Rejected('Keep mode must be Any or All')
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
def specials_included(settings: dict, rule: dict) -> bool:
    """Resolve the per-series specials choice against the global safety default."""
    override = rule.get('include_specials')
    automation = settings.get('automation') or DEFAULTS['automation']
    return bool(override) if override is not None \
        else not bool(automation.get('exclude_specials'))


def air_watermark(episodes, settings: dict, rule: dict) -> str:
    """The latest air date that could matter to this rule, as a high-water mark.

    Derived per rule rather than taken from Sonarr's series-level `previousAiring`, which
    knows nothing about the specials policy: a Christmas special on a show that genuinely
    finished would move that field and re-arm a rule whose owner excludes season 0.
    Computing it here inherits the per-series override for free.

    Unaired episodes count. A date that is scheduled is the better moment to notice a
    series has resumed — retention should be live before the episodes land, not after.
    """
    include_specials = specials_included(settings, rule)
    found = [episode.get('air_date') or '' for episode in episodes or []
             if include_specials or (episode.get('season') or 0) != 0]
    return max((date for date in found if date), default='')


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


def removal_target(instance: dict, rule: dict) -> dict:
    """Canonical target evidence; absent identity cannot authorize external removal."""
    series_id = _whole(rule.get('series_id'), 'Removal series ID', 1, 2 ** 31 - 1)
    tvdb_id = _whole(rule.get('tvdb_id'), 'Removal TVDB ID', 1, 2 ** 31 - 1)
    if not series_id or not tvdb_id or not instance.get('id'):
        raise Rejected('Removal target lacks identity; refresh the rule and requeue.')
    return {'instance_id': _text(instance['id'], 'Removal instance ID', 64, required=True),
            'url': validate_url(instance.get('url')),
            'series_id': series_id, 'tvdb_id': tvdb_id,
            'path': normalise(validate_path(rule.get('path'), 'Removal target path'))}


def validate_queue(raw) -> dict:
    """A rule's pending intent. Nothing here has happened yet; a run is what applies it."""
    raw = raw if isinstance(raw, dict) else {}
    # The one-time monitoring passes are not queued: they are applied when the save asks
    # for them. Waiting for a run made the unmonitor half useless, because a run already
    # unmonitors what is outside the window — the harm it prevents happens before then.
    queue = {'removal': None, 'fixes': []}
    removal = raw.get('removal')
    if isinstance(removal, dict) and removal.get('action'):
        action = _text(removal.get('action'), 'Removal action', 32)
        if action not in REMOVAL_ACTIONS:
            raise Rejected(f'Unknown removal action "{action}"')
        queue['removal'] = {'action': action,
                            'request_id': _text(removal.get('request_id'), 'Removal request ID', 64) or new_id(),
                            'created_at': _text(removal.get('created_at'), 'Queued at', 40) or ''}
        target = removal.get('target')
        if target is not None:
            if not isinstance(target, dict):
                raise Rejected('Invalid removal target snapshot')
            queue['removal']['target'] = removal_target(
                {'id': target.get('instance_id'), 'url': target.get('url')}, target)
    for entry in raw.get('fixes') or []:
        kind = _text((entry or {}).get('kind'), 'Queued fix', 32)
        if kind not in QUEUED_FIXES:
            raise Rejected(f'Unknown queued fix "{kind}"')
        if any(existing['kind'] == kind for existing in queue['fixes']):
            continue
        queue['fixes'].append({'kind': kind,
                               'created_at': _text((entry or {}).get('created_at'), 'Queued at', 40) or ''})
    return queue


def _phrases(raw, field) -> list:
    """A typed list, in the order it was typed, without the blanks or the repeats."""
    found = []
    for value in raw or []:
        text = _text(value, field, 200)
        if text and text not in found:
            found.append(text)
    return found


def validate_automation(raw) -> dict:
    """What a run decides without being asked, and what goes on an exclusion list.

    Every question here has a stated answer with a default, rather than behaviour buried
    in whichever function happened to be doing the work.
    """
    raw = raw or {}
    seasons = set()
    for value in raw.get('exclude_seasons') or []:
        seasons.add(_whole(value, 'Excluded season', 0, 999, allow_none=False))
    return {
        'search_after_monitor': _flag(raw.get('search_after_monitor', False)),
        'exclude_specials': _flag(raw.get('exclude_specials', True)),
        'exclude_seasons': sorted(seasons),
        'exclude_folders': _phrases(raw.get('exclude_folders'), 'Excluded season folder'),
        'exclude_episodes': _phrases(raw.get('exclude_episodes'), 'Excluded episode'),
    }


# Every service that could answer "when did this air", why it might not be available, and
# whether this can talk to it yet. Listed rather than hidden, because a provider missing
# from the page is indistinguishable from one nobody thought of.
AIR_DATE_PROVIDERS = {
    'tmdb': {'name': 'TMDB', 'needs': 'an API key, under Connections', 'built': True},
    'tvmaze': {'name': 'TVMaze', 'needs': '', 'built': True},
    'anilist': {'name': 'AniList', 'needs': '', 'built': True},
    'imdb': {'name': 'IMDB', 'needs': 'no public API exists', 'built': False},
    'plex': {'name': 'Plex', 'needs': 'a Plex connection', 'built': False},
    'jellyfin': {'name': 'Jellyfin', 'needs': 'a Jellyfin connection', 'built': False},
}
AIR_DATE_ANSWERS = {
    'unresolved': {
        'estimate': 'Estimate the air date from neighbouring episodes or position',
        'leave': 'Leave it unresolved',
    },
    'still_unresolved': {
        'exclude': 'Add the episode to the exclusion list',
        'disable': 'Disable the series and raise an error',
    },
}


def validate_air_dates(raw) -> dict:
    """Which services may be asked, in what order, and what happens when none can answer.

    The order is the setting: the first enabled provider that answers wins, so moving a
    row is the whole of how somebody expresses "ask Plex before TMDB". Unknown names are
    dropped rather than refused — a provider removed in a later version should not stop a
    settings document loading.
    """
    raw = raw if isinstance(raw, dict) else {}
    order = [name for name in (raw.get('providers') or []) if name in AIR_DATE_PROVIDERS]
    # Anything this version knows about and the document did not mention goes on the end,
    # so a new provider appears rather than being silently absent.
    order += [name for name in AIR_DATE_PROVIDERS if name not in order]
    # A document from before the provider page had no `enabled` list.  Keep the safe
    # default (TMDB, when a key is present) rather than silently disabling enrichment on
    # first save.  An explicitly supplied empty list still means "disable them all".
    requested = raw['enabled'] if 'enabled' in raw else DEFAULTS['air_dates']['enabled']
    enabled = [name for name in order if name in set(requested or [])]
    return {
        'providers': order,
        'enabled': enabled,
        'unresolved': _choice(raw.get('unresolved') or 'estimate',
                              tuple(AIR_DATE_ANSWERS['unresolved']), 'Unresolved air dates'),
        'still_unresolved': _choice(raw.get('still_unresolved') or 'exclude',
                                    tuple(AIR_DATE_ANSWERS['still_unresolved']),
                                    'Still-unresolved air dates'),
    }


# Said in the preview and in the journal, so "why was this skipped" never needs anyone to
# go and read the settings to find out.
EXCLUSION_REASONS = {
    'manual': 'Excluded from this series by hand',
    'specials': 'Specials are excluded automatically',
    'season': 'This whole season is excluded automatically',
    'folder': 'Excluded automatically: its season folder matches',
    'episode': 'Excluded automatically: the episode matches',
    'air-date': 'Excluded automatically: no air date could be resolved',
}

# Sonarr can be told to file specials as season 0 or into a folder of their own, and which
# one you get depends on a naming setting nobody remembers choosing. Both are specials, so
# both are caught: the season number where there is one, the folder where there is not.
SPECIAL_FOLDERS = ('specials', 'extras', 'featurettes')


def _folder_of(path: str) -> str:
    """The season folder an episode's file sits in, lower-cased.

    The last directory rather than the whole path: matching the lot would let a pattern
    written for a folder catch a library root that happens to contain the word.
    """
    parts = [part for part in (path or '').replace('\\', '/').split('/') if part]
    return parts[-2].lower() if len(parts) >= 2 else ''


def excluded_episodes(episodes, rule, settings) -> dict:
    """Every episode this series will never touch, and why it is on the list.

    Manual first, because a person ticking a box outranks a pattern. The reason travels
    with the answer so the interface can say which is which — red for a rule that put it
    there, orange for a person — and so the journal can say why something was skipped.
    """
    return {key: reason for key, (reason, _) in excluded_causes(episodes, rule, settings).items()}


def excluded_causes(episodes, rule, settings) -> dict:
    """The same answer, carrying *which* season or *which* phrase did it.

    One pass decides both, so the list the series pane shows and the list a run acts on
    cannot disagree: `excluded_episodes` is this with the detail dropped, and
    `exclusion_summary` is this counted. Answering the two questions separately is how a
    pane ends up naming a pattern that excluded nothing.
    """
    automation = settings.get('automation') or DEFAULTS['automation']
    seasons = set(automation.get('exclude_seasons') or [])
    # Kept beside the text as typed, because the summary names it back to the person who
    # wrote it and lower-casing their phrase in the interface would be a small lie.
    folders = [(text, text.lower()) for text in automation.get('exclude_folders') or []]
    titles = [(text, text.lower()) for text in automation.get('exclude_episodes') or []]
    manual = {(entry['season'], entry['episode']) for entry in rule.get('exclusions') or []}
    whole_seasons = {season for season, episode in manual if episode is None}
    # The series' own answer first, then the global one. A series that says "include
    # specials" is opting out of the safety, which is what that setting has always meant.
    specials = not specials_included(settings, rule)

    found = {}
    for episode in episodes:
        season = episode.get('season')
        number = episode.get('episode')
        key = episode.get('episode_id')
        if key is None:
            continue
        folder = _folder_of(episode.get('path') or '')
        if (season, number) in manual or season in whole_seasons:
            found[key] = ('manual', None)
        elif specials and (season == 0 or folder in SPECIAL_FOLDERS):
            found[key] = ('specials', None)
        elif season in seasons:
            found[key] = ('season', season)
        else:
            for shown, needle in folders:
                if needle in folder:
                    found[key] = ('folder', shown)
                    break
            else:
                haystack = f'{episode.get("title") or ""}\n{episode.get("path") or ""}'.lower()
                for shown, needle in titles:
                    if needle in haystack:
                        found[key] = ('episode', shown)
                        break
                else:
                    # The worker marks unresolved dates only after all configured
                    # providers and estimates have had a chance.  This is an automatic
                    # exclusion, never a hand-edited rule entry.
                    if episode.get('air_source') == 'unresolved':
                        found[key] = ('air-date', None)
    return found


def exclusion_summary(episodes, rule, settings) -> dict:
    """What this series' exclusion list is made of, for the series pane to read out.

    Only causes that actually caught something appear. A season excluded globally that
    this series does not have, or a phrase nothing matches, is true of the settings rather
    than of this series, and listing it here would answer a question nobody asked.
    """
    automation = settings.get('automation') or DEFAULTS['automation']
    causes = excluded_causes(episodes, rule, settings)
    seasons, folders, titles = {}, {}, {}
    manual = specials = air_date = 0
    for reason, detail in causes.values():
        if reason == 'manual':
            manual += 1
        elif reason == 'specials':
            specials += 1
        elif reason == 'air-date':
            air_date += 1
        elif reason == 'season':
            seasons[detail] = seasons.get(detail, 0) + 1
        elif reason == 'folder':
            folders[detail] = folders.get(detail, 0) + 1
        else:
            titles[detail] = titles.get(detail, 0) + 1
    # In the order they were typed, which is the order the boxes they came from show them
    # in. Sorted by count, the pane and the setting would disagree on sight. Written out
    # twice rather than through a closure: the name checker cannot see into one, and a
    # helper that has to be exempted from a guard costs more than the line it saved.
    return {
        'total': len(causes),
        'episodes': len(episodes),
        'manual': manual,
        'specials': specials,
        'air_date': air_date,
        'seasons': [{'season': season, 'episodes': count}
                    for season, count in sorted(seasons.items())],
        'folders': [{'pattern': text, 'episodes': folders[text]}
                    for text in automation.get('exclude_folders') or [] if text in folders],
        'episode_patterns': [{'pattern': text, 'episodes': titles[text]}
                             for text in automation.get('exclude_episodes') or [] if text in titles],
    }


def validate_exclusions(raw, field='Exclusions') -> list:
    """Episodes this series never touches, by season and episode number.

    Numbers rather than Sonarr's episode ids, because ids do not survive a series being
    removed and added back and the whole point of this list is that it outlives ordinary
    events. An entry with no episode number excludes the whole season.

    Sorted and de-duplicated on the way in, so the stored document does not change shape
    according to the order somebody happened to tick things.
    """
    found = set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            raise Rejected(f'{field} must be a list of season and episode numbers')
        season = _whole(entry.get('season'), f'{field} season', 0, 999, allow_none=False)
        episode = _whole(entry.get('episode'), f'{field} episode', 0, 9999)
        found.add((season, episode))
    return [{'season': season, 'episode': episode} for season, episode in sorted(
        found, key=lambda pair: (pair[0], -1 if pair[1] is None else pair[1]))]


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
        auto_reenable=_flag(raw.get('auto_reenable', False)),
        # The air date this rule was armed at. Anything later means new material, however
        # the daily syncs happen to fall around a series that un-ends and re-ends in hours.
        auto_reenable_after=_text(raw.get('auto_reenable_after'), 'Re-enable watermark', 40),
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
        queue=validate_queue(raw.get('queue')),
        # Never monitored by us, never unmonitored by us, never deleted. The one list that
        # outranks every rule, including this rule's own.
        exclusions=validate_exclusions(raw.get('exclusions')),
        # Match state is owned by the backend; the UI cannot assert a rule is matched.
        match_status='matched' if series_id else 'unmatched',
        match_error=_text(raw.get('match_error'), 'Match error', 500),
        matched_at=_text(raw.get('matched_at'), 'Matched at', 40),
    )
    if profile_id:
        # Preset-driven rules store no numbers of their own, so there is one source of truth.
        rule.update({'keep_days': None, 'keep_episodes': None, 'keep_seasons': None, 'combine': 'any'})
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
    previous_connections = _connection_map(previous.get('connections'))
    # Older settings kept TMDB at the top level. Treat it as the same connection while
    # normalizing, so saving a redacted document cannot accidentally clear the key.
    if 'tmdb' not in previous_connections and previous_tmdb:
        previous_connections['tmdb'] = {'api_key': previous_tmdb}

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
    # A rule binds to one Sonarr series, so the binding is what must be unique. Keyed on
    # the folder instead, a rule stored before Sonarr moved the series and one added
    # afterwards carried different paths, bound to the same series, and were both
    # processed — two keep windows deleting each other's episodes. The folder key was also
    # instance-blind, so two Sonarrs sharing a root could not both be managed.
    bound = [(r['instance_id'], r['series_id']) for r in rules if r.get('series_id')]
    if len(set(bound)) != len(bound):
        raise Rejected('Two rules point at the same Sonarr series; merge them instead')
    # A rule that has never matched has no series id to be unique by, so where it points
    # is still the only thing identifying it.
    folders = [(r['instance_id'], r['path']) for r in rules if not r.get('series_id')]
    if len(set(folders)) != len(folders):
        raise Rejected('Two rules point at the same folder; merge them instead')

    schedule_raw = raw.get('schedule') or {}
    health_raw = raw.get('health') or {}
    retention_raw = raw.get('retention') or {}
    tmdb_raw = raw.get('tmdb') or {}
    logging_raw = raw.get('logging') or {}

    tmdb_key = _text(tmdb_raw.get('api_key'), 'TMDB API key', 128)
    if tmdb_key == '********':
        tmdb_key = previous_tmdb
    if tmdb_key and not re.match(r'^[A-Za-z0-9._\-]{16,128}$', tmdb_key):
        raise Rejected('TMDB API key looks malformed')

    connection_raw = _connection_map(raw.get('connections'))
    legacy_tmdb = raw.get('tmdb') or {}
    if 'tmdb' not in connection_raw and isinstance(legacy_tmdb, dict):
        connection_raw['tmdb'] = legacy_tmdb
    connections = {kind: validate_connection(kind, connection_raw.get(kind),
                                              previous_connections.get(kind))
                   for kind in OPTIONAL_CONNECTIONS}
    tmdb_key = connections['tmdb']['api_key']
    if tmdb_key and not re.match(r'^[A-Za-z0-9._\-]{16,128}$', tmdb_key):
        raise Rejected('TMDB API key looks malformed')

    air_dates = validate_air_dates(raw.get('air_dates'))
    # A document written before the Safety page existed carries the answer in the old
    # place; the migration moves it, and this catches anything the migration did not see.
    if 'air_dates' not in raw and 'allow_estimated_dates' in retention_raw:
        air_dates['unresolved'] = 'estimate' if _flag(retention_raw['allow_estimated_dates']) else 'leave'

    level = _text(logging_raw.get('level'), 'Log level', 16) or 'info'
    if level not in LOG_LEVELS:
        raise Rejected(f'Log level must be one of: {", ".join(LOG_LEVELS)}')

    api_key = validate_api_key(raw.get('api_key'), previous.get('api_key'))
    state_path = validate_state_dir(raw.get('state_dir') or DEFAULTS['state_dir'])
    settings = {
        'settings_version': SETTINGS_VERSION,
        'settings_revision': _whole(raw.get('settings_revision', 0), 'Settings revision', 0,
                                    2 ** 63 - 1, allow_none=False),
        'schedule': dict(validate_schedule(schedule_raw, 'Schedule'),
                         test_mode=_flag(schedule_raw.get('test_mode', True))),
        'health': {
            'ttl_hours': _whole(health_raw.get('ttl_hours', 24), 'Reading lifetime', 1, 720, allow_none=False),
        },
        'catalogue_ttl_minutes': _whole(raw.get('catalogue_ttl_minutes', 60), 'Series list cache', 1, 10080, allow_none=False),
        'instances': instances,
        'profiles': profiles,
        'rules': rules,
        'tmdb': {'api_key': tmdb_key},
        'connections': connections,
        'api_key': api_key,
        'backup': validate_backup(raw.get('backup'), state_path),
        'automation': validate_automation(raw.get('automation')),
        'air_dates': air_dates,
        'retention': {
            # One decision, stored twice on purpose: the radio on the Safety page is what
            # somebody set, and this is the shape `evaluate` reads. Derived here so the two
            # cannot drift.
            'allow_estimated_dates': air_dates['unresolved'] == 'estimate',
        },
        # Acknowledgement and suppression are per-alert state, not global settings.
        # Keep the empty group so older documents shed their display preferences while
        # the top-level settings shape remains stable.
        'alerts': {},
        'logging': {
            'level': level,
            'max_bytes': _whole(logging_raw.get('max_bytes', 2 * 1024 * 1024), 'Log size', 65536, 64 * 1024 * 1024, allow_none=False),
        },
        'state_dir': state_path,
        'log_retention_runs': _whole(raw.get('log_retention_runs', 50), 'History size', 1, 500, allow_none=False),
    }
    return settings


def redact(settings: dict) -> dict:
    """Settings as shown to the browser: every provider secret is replaced by a mask."""
    copy = json.loads(json.dumps(settings))
    for instance in copy.get('instances', []):
        instance['api_key'] = '********' if instance.get('api_key') else ''
    if copy.get('tmdb', {}).get('api_key'):
        copy['tmdb']['api_key'] = '********'
    for connection in (copy.get('connections') or {}).values():
        for field in ('api_key', 'token', 'password', 'secret'):
            if field in connection:
                connection[field] = '********' if connection.get(field) else ''
    if 'api_key' in copy:
        # A digest is not useful to the browser and exposing it makes the lifecycle state
        # look like an API credential.  The action returns only status/prefix metadata.
        copy['api_key'] = {key: value for key, value in copy['api_key'].items()
                           if key != 'hash'}
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


def air_date_gaps(episodes, rule, settings) -> list:
    """Episodes a keep-by-age rule would judge without a trustworthy air date.

    Provider and interpolation enrichment happens in the worker.  This pure predicate is
    the invariant used by the editor, health check, preview, and run so none of those
    paths can quietly make a different decision about an undated file.
    """
    if not rule.get('keep_days'):
        return []
    # An ``air-date`` cause is the *result* of this predicate when the operator chose
    # automatic exclusion.  Do not let a cached unresolved marker make the predicate
    # disappear on the next pass, especially when the configured outcome is ``disable``.
    # Manual and other global exclusions still remove an episode from the judged set.
    excluded = {key for key, (reason, _) in excluded_causes(episodes, rule, settings).items()
                if reason != 'air-date'}
    return [episode for episode in episodes or []
            if episode.get('path') and episode.get('episode_id') not in excluded
            and episode.get('has_file') and
            effective_date(episode, allow_import_fallback=False)[0] is None]


def next_episode(episodes, now=None):
    """The next episode due, named rather than dated.

    Sonarr's own `nextAiring` is a timestamp and nothing else, so a panel wanting to say
    "S03E04: Title" would have to ask again for what the reading already holds. Episodes
    with a file are skipped: one that aired this morning and is already on disk is not
    what "next" means to anyone.
    """
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    best = None
    for episode in episodes:
        if episode.get('has_file'):
            continue
        date, source = effective_date(episode, allow_import_fallback=False)
        if not date or date < today:
            continue
        key = (date, episode.get('season') or 0, episode.get('episode') or 0)
        if best is None or key < best[0]:
            best = (key, episode, source)
    if best is None:
        return None
    (date, _, _), episode, source = best
    return {'season': episode.get('season'), 'episode': episode.get('episode'),
            'title': episode.get('title') or '', 'air_date': date.isoformat(),
            # An interpolated or TMDB date is a guess, and the panel says so rather than
            # printing it with the same confidence as one Sonarr gave us.
            'estimated': source not in ('sonarr', '')}


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

    # The exclusion list outranks every condition, including this rule's own. It is the one
    # answer that is never weighed against anything — and it is the *only* one: specials
    # used to be a second gate here, saying the same kind of thing a few lines further
    # down, which meant "what will this run skip" had two places to be answered from.
    excluded = excluded_episodes(episodes, rule, settings)
    # Sonarr removes a whole file. Use its latest episode for the cutoff, but
    # let an exclusion on any member protect it. Keep episode rows for the UI
    # and monitoring; the worker deduplicates physical deletions.
    def file_key(episode):
        return episode.get('file_id') or episode['path']

    protected_files = {file_key(e): excluded[e.get('episode_id')]
                       for e in episodes if e.get('path') and e.get('episode_id') in excluded}
    protected, candidates = [], []
    for episode in episodes:
        if not episode.get('path'):
            continue
        why = protected_files.get(file_key(episode))
        if why:
            protected.append(dict(episode, reason=EXCLUSION_REASONS[why]))
            continue
        candidates.append(episode)

    latest = {}
    for episode in sorted(candidates, key=lambda e: _order_key(e, allow_estimates)):
        latest[file_key(episode)] = episode
    votes = {key: [] for key in latest}
    # A shared file must not hide an unknown or forthcoming sibling behind the
    # dated episode used as its cutoff. Single-episode behavior is unchanged.
    for key in latest:
        members = [e for e in candidates if file_key(e) == key]
        if len(members) > 1 and any(
                effective_date(e, allow_estimates)[0] is None
                or effective_date(e, allow_estimates)[0] > now.date() for e in members):
            votes[key].append(('file', 'unknown', 'Shared file has an undated or unaired episode'))

    if rule.get('keep_days'):
        cutoff = (now - dt.timedelta(days=int(rule['keep_days']))).date()
        for key, episode in latest.items():
            date, source = effective_date(episode, allow_estimates)
            if date is None:
                votes[key].append(('days', 'unknown', 'No air date available'))
            elif date >= cutoff:
                votes[key].append(('days', 'keep', f'Aired {date}, within {rule["keep_days"]} days'))
            else:
                votes[key].append(('days', 'delete', f'Aired {date} ({source}), older than {rule["keep_days"]} days'))

    if rule.get('keep_episodes'):
        ranked = sorted(candidates, key=lambda e: _order_key(e, allow_estimates), reverse=True)
        keep_set = {file_key(e) for e in ranked[:int(rule['keep_episodes'])]}
        for key in latest:
            if key in keep_set:
                votes[key].append(('episodes', 'keep', f'Among the newest {rule["keep_episodes"]} episodes'))
            else:
                votes[key].append(('episodes', 'delete', f'Not among the newest {rule["keep_episodes"]} episodes'))

    if rule.get('keep_seasons'):
        seasons = sorted({e.get('season') for e in candidates if e.get('season') is not None}, reverse=True)
        keep_seasons = set(seasons[:int(rule['keep_seasons'])])
        for key, episode in latest.items():
            season = episode.get('season')
            if season in keep_seasons:
                votes[key].append(('seasons', 'keep', f'Season {season} is among the newest {rule["keep_seasons"]} seasons'))
            else:
                votes[key].append(('seasons', 'delete', f'Season {season} is older than the newest {rule["keep_seasons"]} seasons'))

    combine = rule.get('combine', 'any')
    delete, keep = [], []
    for episode in candidates:
        cast = votes[file_key(episode)]
        verdicts = [v for _, v, _ in cast]
        if not cast:
            keep.append(dict(episode, reason='No retention condition applied'))
            continue
        if combine == 'any':
            # Keep when any condition says keep. An unknown also protects the file: no
            # retention mode may turn missing information into permission to delete.
            remove = all(v == 'delete' for v in verdicts)
        else:  # 'all'
            # Keep only when every enabled condition says keep. This is more aggressive,
            # but an unknown still prevents deletion rather than being treated as a vote.
            remove = 'delete' in verdicts and 'unknown' not in verdicts
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


MONITOR_STATUS = {
    'empty': 'Sonarr lists no episodes',
    'aligned': 'In frame monitored, outside unmonitored',
    'all_monitored': 'All episodes monitored',
    'outside_monitored': 'Episodes outside the keep frame are still monitored',
    'in_frame_unmonitored': 'Episodes inside the keep frame are unmonitored',
    'mixed': 'Monitoring does not match the keep frame',
}


def keep_frame(episodes, rule, settings, now=None) -> dict:
    """Split a series' episodes into what a rule keeps and what it does not.

    The keep window as a set of episodes, which is what both the monitoring view and the
    one-time pass over newly scoped episodes need. Specials are excluded unless asked for,
    and anything that has not aired is inside the frame whatever the rule says: you want
    the next episode regardless of what retention thinks of the old ones.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    retention = settings.get('retention') or {}

    # Set aside what is excluded, and for the same reason deletion does: outside the frame,
    # and so outside everything that acts on the frame. Never monitored by us, never
    # unmonitored. Specials arrive here through the exclusion list like everything else, so
    # a special that is kept is still a special that counts.
    excluded = excluded_episodes(episodes, rule, settings)
    specials, considered = [], []
    for episode in episodes:
        if excluded.get(episode.get('episode_id')):
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
    return {'considered': considered, 'specials': specials, 'unaired': unaired,
            'in_frame': decision['keep'] + decision['protected'] + unaired,
            'out_frame': decision['delete']}


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
    frame = keep_frame(episodes, rule, settings, now=now)
    considered, specials = frame['considered'], frame['specials']
    in_frame, out_frame = frame['in_frame'], frame['out_frame']

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
        'unaired': len(frame['unaired']),
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
        'global_specials': (settings.get('automation') or {}).get('exclude_specials'),
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
