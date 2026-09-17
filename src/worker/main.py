#!/usr/bin/env python3
"""TV Retention worker: RPC bridge for the web UI, and the scheduled retention run.

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
import ssl
import sys
import urllib.error
import urllib.request
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core import (DEFAULTS, air_watermark, keep_frame, REMOVAL_ACTIONS, VERSION, Rejected, atomic_json,
                  canonical_json, classify_monitoring, describe_lifecycle, describe_selectability,
                  effective_rule, evaluate, air_date_gaps, interpolate_air_dates, new_id, normalise, redact,
                  rule_fingerprint, specials_included, validate_settings)
import alerts
from migrate import migrate
from sonarr import Sonarr, SonarrError, match_rule
import schedules
from store import (CONFIG, NAME, RUNTIME, SCHEMA,
                   age_seconds, archive_intent, cache_path, clear_progress, episode_cache, forget_episodes,
                   invalidate_catalogue, job_state, journal, load_health, load_settings,
                   load_intent, load_state, log_line, now_iso, read_cache, read_log, read_progress,
                   save_job_state, save_settings, save_state, set_progress, state_dir,
                   save_intent, load_removal_ledger, save_removal_ledger,
                   store_episodes, trim_health, write_cache)
from tmdb import TMDB, TMDBError, fill_air_dates
from tvmaze import TVMaze, TVMazeError
from anilist import AniList, AniListError

OPTIONAL_PROVIDER_KINDS = ('tmdb', 'tvmaze', 'anilist', 'plex', 'jellyfin')


class _MediaDateProvider:
    """A narrowly scoped Plex/Jellyfin connection.

    These integrations are intentionally not media-library adapters.  They only verify
    the configured endpoint for the Connections page; no watched state or filesystem data
    is consumed, and their date lookup is a no-op until an endpoint exposes a meaningful
    episode-date contract.
    """
    def __init__(self, kind, connection):
        self.kind = kind
        self.connection = connection

    def check(self):
        url = (self.connection.get('url') or '').rstrip('/')
        if not url:
            raise Rejected(f'{self.kind.title()} connection has no URL')
        path = '/identity' if self.kind == 'plex' else '/System/Info'
        request = urllib.request.Request(url + path, headers={'Accept': 'application/json'})
        credential = self.connection.get('token') or self.connection.get('api_key') or ''
        if credential:
            request.add_header('X-Emby-Token' if self.kind == 'jellyfin' else 'X-Plex-Token', credential)
        try:
            context = None if self.connection.get('verify_tls', True) else ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=20, context=context):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
            raise Rejected(f'{self.kind.title()} request failed ({error})') from error

    def fill(self, episodes, _identity):
        return 0


def optional_provider(kind, connection, settings):
    """Build one configured provider; callers decide whether it is enabled."""
    cache = state_dir(settings) / f'{kind}-air-date-cache.json'
    if kind == 'tmdb':
        key = (connection or {}).get('api_key') or ''
        return TMDB(key, cache_path=state_dir(settings) / 'tmdb-cache.json') if key else None
    if kind == 'tvmaze':
        return TVMaze(cache_path=cache)
    if kind == 'anilist':
        return AniList(cache_path=cache)
    if kind in ('plex', 'jellyfin'):
        return _MediaDateProvider(kind, connection or {})
    return None

# Sonarr is confirmed reachable this often, and before anything that needs it.
CONNECTIVITY_SECONDS = 300
# The resident loop's heartbeat. The plugin published a one-minute cron entry and let the
# worker decide what was due; a process that stays alive just sleeps. The reasons the tick
# existed — catching up a run missed while the machine was off, holding one until Sonarr
# answers — are now ordinary rather than carefully arranged.
TICK_SECONDS = 30
# The resident worker refreshes Sonarr regularly even when no browser is open. A page
# opening or returning after an idle spell uses the shorter threshold below, but both go
# through the same lock so several tabs still cause one read.
SYNC_FRESH_SECONDS = 3600
PAGE_REFRESH_SECONDS = 300
PROCESS_STARTED = time.time()




# ---------------------------------------------------------------------------
# Environment guards
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def run_lock(blocking: bool = False):
    """One run at a time. A scheduled run never overlaps a manual one."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME / 'run.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Rejected('A TV Retention run is already in progress.')
        yield


def notify(*_args, **_kwargs) -> None:
    """Compatibility no-op for callers from older integrations.

    Outbound notifications and webhooks were removed in settings version 13.  Keeping a
    tiny no-op symbol avoids breaking an external import during a rolling upgrade while
    guaranteeing that no request can leave the container.
    """
    return None



# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

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
                'slug': found.get('slug') or '',
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


def fill_from_history(client, series_id: int, episodes) -> int:
    """Date undated episodes from when Sonarr first acquired them.

    Reached only when a series has no dated episode at all, so interpolation had nothing
    to work from. Unlike the file's dateAdded, the oldest history record survives an
    upgrade, which is the flaw that made dateAdded a poor last resort.
    """
    missing = [episode for episode in episodes
               if not episode.get('air_date') and episode.get('has_file') and episode.get('episode_id')]
    if not missing:
        return 0
    try:
        earliest = client.first_acquired(series_id)
    except SonarrError:
        return 0
    filled = 0
    for episode in missing:
        stamp = earliest.get(episode['episode_id'])
        if not stamp:
            continue
        episode['air_date'] = str(stamp)[:10]
        episode['air_source'] = 'acquired'
        filled += 1
    return filled


def tmdb_provider(settings: dict):
    """The TMDB client for a run, or None when no key is configured.

    An API key is the switch: nobody enters one they do not want used. There is no
    `enabled` flag to check here — `validate_settings` never keeps one (migration
    deletes it, and `test_migration.py` asserts it is gone) — so a run that gated on it
    could never build a client no matter what key was entered, while "Test TMDB" kept
    reporting success because it builds its own client directly from the key alone.
    """
    connections = settings.get('connections') or {}
    tmdb_cfg = connections.get('tmdb') if isinstance(connections, dict) else None
    api_key = (tmdb_cfg or {}).get('api_key') or (settings.get('tmdb') or {}).get('api_key')
    if not api_key:
        return None
    return TMDB(api_key, cache_path=state_dir(settings) / 'tmdb-cache.json')


def provider_chain(settings):
    """Configured air-date providers in their stored priority order."""
    air = settings.get('air_dates')
    air = air if isinstance(air, dict) else {}
    connections = settings.get('connections')
    connections = connections if isinstance(connections, dict) else {}
    providers = air['providers'] if 'providers' in air else DEFAULTS['air_dates']['providers']
    requested = air['enabled'] if 'enabled' in air else DEFAULTS['air_dates']['enabled']
    enabled = set(requested or [])
    chain = []
    for kind in providers or OPTIONAL_PROVIDER_KINDS:
        if kind not in enabled or kind not in OPTIONAL_PROVIDER_KINDS:
            continue
        connection = connections.get(kind) or {}
        if kind == 'tmdb' and not connection:
            # A v12 or older caller may still provide only the legacy top-level key.
            connection = {'api_key': (settings.get('tmdb') or {}).get('api_key', '')}
        # TMDB is enabled by the presence of its key for backwards compatibility; the
        # explicit enabled list still controls provider order.
        if kind == 'tmdb' and not ((connection or {}).get('api_key') or
                                   (settings.get('tmdb') or {}).get('api_key')):
            continue
        if kind in ('plex', 'jellyfin') and not (connection or {}).get('enabled'):
            continue
        provider = optional_provider(kind, connection or {}, settings)
        if provider:
            chain.append((kind, provider))
    return chain


def enrich_air_dates(settings: dict, rule: dict, episodes, provider_override=None, client=None) -> dict:
    """Fill missing dates through the configured chain and mark unresolved files safely."""
    filled = 0
    chain = provider_chain(settings)
    # ``collect_episodes`` historically accepted a TMDB client from its caller. Keep that
    # seam for tests and rolling upgrades, while still applying the configured chain for
    # every normal read. The explicit client replaces only TMDB; later providers remain
    # available in their configured order.
    if provider_override is not None:
        chain = [('tmdb', provider_override)] + [(kind, provider) for kind, provider in chain
                                                  if kind != 'tmdb']
    for kind, provider in chain:
        try:
            if kind == 'tmdb':
                filled += fill_air_dates(episodes, provider, rule.get('tvdb_id'))
            elif kind == 'tvmaze':
                filled += provider.fill(episodes, rule.get('tvdb_id'))
            elif kind == 'anilist':
                filled += provider.fill(episodes, rule.get('series_title') or rule.get('slug'))
            else:
                # Plex/Jellyfin are connectivity checks until they expose a stable
                # per-episode date contract; they stay in the chain without guessing.
                continue
        except (TMDBError, TVMazeError, AniListError, Rejected):
            continue
    estimate = (settings.get('air_dates') or {}).get('unresolved', 'estimate') == 'estimate'
    if estimate:
        filled += interpolate_air_dates(episodes)
        # History is a last-resort estimate for files with no neighbouring dates.
        if not any(episode.get('air_date') for episode in episodes):
            with contextlib.suppress(SonarrError):
                history_client = client or client_for(settings, rule['instance_id'])
                filled += fill_from_history(history_client, rule['series_id'], episodes)
    else:
        # Do not make a missing date look like an old file. The core invariant decides
        # whether this is a blocking error or an automatic exclusion.
        for episode in episodes:
            if not episode.get('air_date') and episode.get('has_file'):
                episode['air_source'] = 'unresolved'
    gaps = air_date_gaps(episodes, rule, settings)
    if gaps and (settings.get('air_dates') or {}).get('still_unresolved', 'exclude') == 'exclude':
        for episode in gaps:
            episode['air_source'] = 'unresolved'
    return {'filled': filled, 'unresolved': gaps}


def collect_episodes(settings: dict, rule: dict, client: Sonarr, tmdb=None) -> list:
    """Sonarr's episode files for one rule.

    Everything the retention pass needs — sizes, air dates, import dates, monitoring —
    arrives with the episodes. Sonarr owns the filesystem; the plugin only asks it what it
    has and tells it what to remove.

    Fetched with `files_only=False`: the request to Sonarr is identical either way — the
    flag only filters the mapped result — and a run also reconciles monitoring for
    episodes with no file, which `files_only=True` would silently drop. Callers that only
    want what could actually be deleted filter to `has_file` themselves, the same way
    `monitoring_for` already does.
    """
    episodes = client.episodes(rule['series_id'], files_only=False)
    # `tmdb` is retained as an optional argument for callers from older releases; the
    # configured provider chain now owns enrichment and applies the air-date invariant.
    enrich_air_dates(settings, rule, episodes, provider_override=tmdb, client=client)
    return episodes


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------





def planned_deletion(episode: dict) -> dict:
    """Describe a candidate deletion; only the checkpointed executor may send it."""
    return {
        'episode_id': episode.get('episode_id'),
        'file_id': episode.get('file_id'),
        'path': episode['path'],
        'season': episode.get('season'),
        'episode': episode.get('episode'),
        'title': episode.get('title'),
        'air_date': episode.get('air_date'),
        'air_source': episode.get('air_source'),
        'size': episode.get('size'),
        'reason': episode.get('reason'),
        'dry_run': True,
        'ok': True,
        'error': '',
    }


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

def newly_scoped_rows(settings: dict, rule: dict, episodes: list, previous_scope) -> list:
    """Unmonitored episodes a save brought into the keep window that were outside it.

    A rule widened from thirty days to ninety did not ask for the ninety days it always
    had; it asked for the sixty it just gained. The two windows are compared rather than a
    list of ids being kept, because episodes move, and one that arrives belongs to
    whichever window it lands in.

    A series just added has no old window at all, so everything inside its keep window is
    newly scoped, which is what adding a series means.
    """
    frame = keep_frame(episodes, effective_rule(rule, settings.get('profiles')), settings)
    inside = [episode for episode in frame['in_frame'] if not episode.get('monitored')]
    if not previous_scope:
        return inside
    previous = dict(effective_rule(rule, settings.get('profiles')))
    previous.update(previous_scope)
    was = {episode.get('episode_id')
           for episode in keep_frame(episodes, previous, settings)['in_frame']}
    return [row for row in inside if row.get('episode_id') not in was]


def scope_pass(settings: dict, rule: dict, monitor_new: bool = False,
               unmonitor_outside: bool = False, previous_scope=None) -> dict:
    """Two one-time corrections, applied now rather than at the next run.

    Both are asked for explicitly, on a save, and neither deletes anything — they move
    Sonarr's monitored flags, which is reversible in a click and is the least destructive
    thing this plugin does.

    Waiting for the run is what makes the second one pointless. Every run already
    unmonitors what falls outside the window; the harm this prevents happens *between* the
    save and the run, when Sonarr is still fetching episodes the next run would delete. A
    queued version of it would arrive exactly too late to matter.

    Decided from the stored reading, so it costs one write to Sonarr and no reads.
    """
    episodes, series, read_at, from_cache = episodes_for(settings, rule, offline=True)
    active = effective_rule(rule, settings.get('profiles'))
    frame = keep_frame(episodes, active, settings)
    result = {'monitored': [], 'unmonitored': [], 'read_at': read_at}

    if monitor_new:
        result['monitored'] = newly_scoped_rows(settings, rule, episodes, previous_scope)
    if unmonitor_outside:
        result['unmonitored'] = [episode for episode in frame['out_frame'] if episode.get('monitored')]

    client = client_for(settings, rule['instance_id'])
    for key, wanted in (('monitored', True), ('unmonitored', False)):
        ids = [row['episode_id'] for row in result[key] if row.get('episode_id')]
        if ids:
            client.set_monitored(ids, wanted)
    log_line(settings, 'info',
             f'{rule.get("series_title") or rule["path"]}: one-time pass monitored '
             f'{len(result["monitored"])}, unmonitored {len(result["unmonitored"])}')
    return {'monitored': len(result['monitored']), 'unmonitored': len(result['unmonitored'])}


def monitoring_targets(settings: dict, state: dict, rule: dict) -> dict:
    """Which episodes a run will switch.

    A run only ever unmonitors. Unmonitoring is protection — it stops a download and
    nothing else — so what leaves the keep window is unmonitored whether or not it has a
    file, and the fileless half matters most: an episode with a file is unmonitored when
    the file is deleted, but a missing one is never deleted, so nothing else would ever
    reach it, and Sonarr would go on fetching what the next run deletes.

    Monitoring is intent, and it costs downloads, so it is never something a run decides.
    It happens once, when someone asks for it on a rule they just widened.
    """
    outside = state.get('out_frame_monitored') or []
    return {
        'monitor': [],
        'unmonitor': [row['episode_id'] for row in outside if row.get('episode_id')],
        'monitor_list': [],
        'unmonitor_missing': len([row for row in outside if not row.get('has_file')]),
    }


def planned_monitoring(settings: dict, rule: dict, state: dict) -> dict:
    """Describe the monitoring changes required by a series' keep window.

    Unmonitoring everything outside the window is unconditional, including the episodes
    with no file: those are never deleted, so nothing else would ever unmonitor them, and
    Sonarr would go on fetching what the next run removes.

    A run never monitors. The one-time pass a save can ask for is applied at that moment,
    not here — waiting for a run is what made its other half useless.
    """
    targets = monitoring_targets(settings, state, rule)
    monitor = list(targets['monitor'])
    monitor_list = list(targets['monitor_list'])
    result = {'monitored': len(monitor), 'unmonitored': len(targets['unmonitor']),
              'searched': 0, 'error': '', 'newly_scoped': 0,
              'monitor_list': monitor_list,
              'unmonitor_list': list(state.get('out_frame_monitored') or [])}
    return result


def process_rule(settings: dict, rule: dict, tmdb, dry_run: bool, remember: bool = True) -> dict:
    """Plan one rule without external mutations; reject obsolete execution callers."""
    if not dry_run:
        raise Rejected('The rule planner cannot execute; use run() for checkpointed execution.')
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
        'monitor_list': [],
        'unmonitor_list': [],
        'searched': 0,
        'newly_scoped': 0,
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
        episodes = collect_episodes(settings, rule, client, tmdb)
    except SonarrError as error:
        outcome.update(ok=False, error=str(error))
        return outcome
    gaps = air_date_gaps(episodes, rule, settings)
    if gaps and (settings.get('air_dates') or {}).get('still_unresolved', 'exclude') == 'disable':
        outcome.update(ok=False, error=f'{len(gaps)} episode(s) have no resolvable air date',
                       blocked='Keep-by-age is blocked until every judged episode has an air date.')
        return outcome

    # Plan monitoring alongside deletions; staging orders writes before file removal.
    # A run reads Sonarr for itself, but only once: `episodes` above is that reading, full
    # and fresh, so it is handed in here rather than asked for a second time. A stored
    # reading is still the one thing that must not stand behind a write, which is what
    # `preloaded` preserves — it is stored exactly as a fetch here would be.
    reconciled = planned_monitoring(
        settings, rule, monitoring_for(settings, rule, force=True, preloaded=episodes,
                                      persist=remember))
    outcome['monitored'] = reconciled['monitored']
    outcome['unmonitored_frame'] = reconciled['unmonitored']
    outcome['searched'] = reconciled['searched']
    outcome['newly_scoped'] = reconciled.get('newly_scoped', 0)
    # The episodes themselves, so a detail view can list every change rather than only
    # the deletions.
    outcome['monitor_list'] = reconciled.get('monitor_list') or []
    outcome['unmonitor_list'] = reconciled.get('unmonitor_list') or []
    if reconciled['error']:
        outcome['error'] = reconciled['error']

    # Deletion only ever looks at what is actually on disk: `episodes` above also carries
    # the fileless ones monitoring needed, and each of those has a synthetic path
    # (`sonarr:episode:<id>`) that would otherwise be a real, if empty, delete candidate.
    present = [episode for episode in episodes if episode.get('has_file')]
    decision = evaluate(present, active, settings)
    outcome['considered'] = decision['considered']
    outcome['kept'] = len(decision['keep'])
    outcome['protected'] = len(decision['protected'])
    if decision['blocked']:
        outcome['blocked'] = decision['blocked']
        return outcome

    for episode in decision['delete']:
        outcome['deleted'].append(planned_deletion(episode))
        outcome['freed_bytes'] += int(episode.get('size') or 0)

    return outcome


def _run_error(rule: dict, error: Exception) -> dict:
    return {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
            'path': rule['path'], 'ok': False, 'error': str(error), 'deleted': [],
            'considered': 0, 'kept': 0, 'protected': 0, 'blocked': None,
            'freed_bytes': 0, 'unmonitored': 0, 'preset': '', 'note': ''}


def _operation(kind: str, rule: dict, **detail) -> dict:
    if detail.get('removal_action'):
        detail['request_id'] = ((rule.get('queue') or {}).get('removal') or {}).get('request_id')
    return dict({'id': new_id(), 'kind': kind, 'rule_id': rule['id'],
                 'instance_id': rule['instance_id'], 'series_id': rule['series_id'],
                 'status': 'pending', 'attempts': 0, 'error': ''}, **detail)


def _removal_stage(settings: dict, rules: list, tmdb, enrich: bool = True) -> tuple[list, list]:
    """Read each queued removal now; execution happens only after every decision is saved."""
    records, operations = [], []
    for rule in rules:
        queued = (rule.get('queue') or {}).get('removal')
        if not queued:
            continue
        action = queued['action']
        record = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                  'action': action, 'label': REMOVAL_ACTIONS.get(action, action),
                  'queued_at': queued.get('created_at'), 'dry_run': False, 'ok': True, 'error': '',
                  'request_id': queued.get('request_id'), 'instance_id': rule.get('instance_id'),
                  'series_id': rule.get('series_id')}
        try:
            client = client_for(settings, rule['instance_id'])
            if action in ('monitor-all', 'unmonitor-all'):
                ids = [row['episode_id'] for row in client.episodes(rule['series_id'], files_only=False)
                       if row.get('episode_id')]
                operations.append(_operation('set-monitored', rule, episode_ids=ids,
                                             monitored=(action == 'monitor-all'), removal_action=action))
            elif action == 'monitor-in-frame':
                episodes = collect_episodes(settings, rule, client, tmdb) if enrich \
                    else client.episodes(rule['series_id'], files_only=False)
                frame = keep_frame(episodes, effective_rule(rule, settings.get('profiles')), settings)
                ids = [row['episode_id'] for row in frame['in_frame']
                       if not row.get('monitored') and row.get('episode_id')]
                operations.append(_operation('set-monitored', rule, episode_ids=ids, monitored=True,
                                             removal_action=action))
            elif action in ('delete-series', 'delete-series-files'):
                # A current series lookup is the final identity check before its deletion.
                client.series_one(rule['series_id'])
                operations.append(_operation('delete-series', rule,
                                             delete_files=(action == 'delete-series-files'),
                                             removal_action=action))
            else:
                operations.append(_operation('remove-rule', rule, removal_action=action))
        except (Rejected, SonarrError) as error:
            record.update(ok=False, error=str(error))
        records.append(record)
    return records, operations


def _stage_intent(settings: dict, selected: list, queued_rules: list, tmdb,
                  remember: bool = True) -> dict:
    """Read every affected show and freeze one complete, auditable decision."""
    results, operations = [], []
    removal_ids = {rule['id'] for rule in queued_rules}
    removals, removal_operations = _removal_stage(settings, queued_rules, tmdb, enrich=remember)
    operations.extend(removal_operations)
    for rule in selected:
        if rule['id'] in removal_ids:
            continue
        try:
            result = process_rule(settings, rule, tmdb, dry_run=True, remember=remember)
        except Rejected as error:
            result = _run_error(rule, error)
        results.append(result)
        if not result.get('ok') or result.get('blocked'):
            continue
        ids = [row.get('episode_id') for row in result.get('unmonitor_list') or [] if row.get('episode_id')]
        if ids:
            operations.append(_operation('set-monitored', rule, episode_ids=ids, monitored=False,
                                         outcome='unmonitor'))
        for index, episode in enumerate(result.get('deleted') or []):
            operations.append(_operation('delete-episode-file', rule, file_id=episode.get('file_id'),
                                         episode_id=episode.get('episode_id'), outcome_index=index))
    return {'version': 1, 'id': new_id(), 'status': 'staged', 'started': now_iso(),
            'rules': results, 'removals': removals, 'operations': operations}


def _execute_operation(settings: dict, intent: dict, operation: dict, checkpoint=None) -> None:
    """Checkpoint before dispatch and after acknowledgement; disk failures propagate."""
    persist = checkpoint or save_intent
    operation['status'] = 'in-progress'
    operation['attempts'] = int(operation.get('attempts') or 0) + 1
    operation['started_at'] = now_iso()
    persist(settings, intent)
    try:
        if operation.get('removal_action'):
            _validate_removal_request(load_settings(), operation)
        kind = operation['kind']
        if kind != 'remove-rule':
            client = client_for(settings, operation['instance_id'])
            if kind == 'set-monitored':
                client.set_monitored(operation.get('episode_ids') or [], operation.get('monitored', False))
            elif kind == 'delete-episode-file':
                client.delete_episode_file(operation['file_id'])
            elif kind == 'delete-series':
                client.delete_series(operation['series_id'], delete_files=operation.get('delete_files', False))
                invalidate_catalogue(settings, operation['instance_id'])
            else:
                raise Rejected(f'Unknown operation kind: {kind}')
    except (Rejected, SonarrError, ValueError) as error:
        operation.update(status='failed', error=str(error), finished_at=now_iso())
    else:
        operation.update(status='done', error='', finished_at=now_iso())
    persist(settings, intent)


def _validate_removal_request(settings: dict, operation: dict) -> None:
    """A saved one-time operation may only use the same current queue request.

    Legacy operations without an ID cannot prove continuity and are refused. This is
    not a transaction lock or proof of remote target identity.
    """
    rule = next((r for r in settings.get('rules') or [] if r['id'] == operation.get('rule_id')), {})
    queued = (rule.get('queue') or {}).get('removal') or {}
    if (not operation.get('request_id')
            or queued.get('request_id') != operation['request_id']
            or queued.get('action') != operation.get('removal_action')):
        raise Rejected('Saved removal was changed or canceled, or lacks request identity; recovery requires review.')


def _resume_intent(settings: dict, intent: dict, checkpoint=None) -> None:
    """Refresh only unfinished targets and recognize a write completed before a crash.

    A network acknowledgement can be lost after Sonarr has accepted a request.  Replaying
    that request without looking is exactly what the ledger prevents: a fresh read lets us
    retire a delete that already happened and trim monitoring work that is already true.
    """
    # A saved removal is not permission to resurrect an operator's canceled queue.
    # Check the whole unfinished removal set before reconciliation changes the ledger.
    for operation in intent.get('operations') or []:
        if operation.get('status') == 'done' or not operation.get('removal_action'):
            continue
        _validate_removal_request(settings, operation)
    fresh = {}
    for operation in intent.get('operations') or []:
        if operation.get('status') == 'done' or operation.get('kind') == 'remove-rule':
            continue
        if operation.get('kind') == 'delete-series':
            try:
                client_for(settings, operation['instance_id']).series_one(operation['series_id'])
            except SonarrError as error:
                if error.status_code == 404:
                    operation.update(status='done', error='', recovered_at=now_iso())
                else:
                    raise
            continue
        key = (operation['instance_id'], operation['series_id'])
        if key in fresh:
            continue
        # Uncertain reads must abort recovery, not fall through to replaying writes.
        fresh[key] = client_for(settings, key[0]).episodes(key[1], files_only=False)
    for operation in intent.get('operations') or []:
        if operation.get('status') == 'done':
            continue
        rows = fresh.get((operation['instance_id'], operation['series_id']))
        if rows is None:
            continue
        by_id = {row.get('episode_id'): row for row in rows}
        if operation['kind'] == 'delete-episode-file':
            row = by_id.get(operation.get('episode_id'))
            if not row or not row.get('has_file') or row.get('file_id') != operation.get('file_id'):
                operation.update(status='done', error='', recovered_at=now_iso())
        elif operation['kind'] == 'set-monitored':
            wanted = bool(operation.get('monitored'))
            operation['episode_ids'] = [episode_id for episode_id in operation.get('episode_ids') or []
                                        if by_id.get(episode_id)
                                        and bool(by_id[episode_id].get('monitored')) != wanted]
            if not operation['episode_ids']:
                operation.update(status='done', error='', recovered_at=now_iso())
    intent['resumed_at'] = now_iso()
    (checkpoint or save_intent)(settings, intent)


def _intent_summary(intent: dict, preview: bool, test_mode: bool, scheduled: bool, started: str) -> dict:
    """Render the durable decision as the established run-result contract."""
    rules = json.loads(json.dumps(intent.get('rules') or []))
    removals = json.loads(json.dumps((intent.get('removals') or [])
                                    + (intent.get('recovery_removals') or [])))
    by_rule = {row.get('rule_id'): row for row in rules}
    dry_run = preview or test_mode
    for operation in intent.get('operations') or []:
        # Pending is expected in a preview, but never counts as applied work in a run.
        unfinished = not dry_run and operation.get('status') != 'done'
        error = (operation.get('error') or 'A staged operation was not completed') if unfinished else ''
        if operation.get('removal_action') and unfinished:
            for record in removals:
                if record.get('rule_id') == operation.get('rule_id'):
                    record.update(ok=False, error=error)
        outcome = by_rule.get(operation.get('rule_id'))
        if not outcome:
            continue
        if unfinished:
            outcome['ok'] = False
            outcome['error'] = outcome.get('error') or error
        if operation.get('kind') == 'delete-episode-file':
            index = operation.get('outcome_index')
            if isinstance(index, int) and 0 <= index < len(outcome.get('deleted') or []):
                outcome['deleted'][index].update(ok=not dry_run and operation.get('status') == 'done',
                                                  dry_run=dry_run, error=error)
    for outcome in rules:
        outcome['freed_bytes'] = sum(int(row.get('size') or 0) for row in outcome.get('deleted') or []
                                      if row.get('ok'))
    deleted = sum(len([row for row in outcome.get('deleted') or [] if row.get('ok')]) for outcome in rules)
    return {'id': intent['id'], 'started': started, 'finished': now_iso(), 'scheduled': scheduled,
            'preview': preview, 'test_mode': test_mode, 'dry_run': dry_run,
            'status': 'preview' if dry_run else intent.get('status', 'incomplete'),
            'rules': rules, 'planned': sum(len(row.get('deleted') or []) for row in rules),
            'removals': removals, 'deleted': deleted,
            'freed_bytes': sum(row.get('freed_bytes') or 0 for row in rules),
            'errors': [row['error'] for row in rules if row.get('error')]
            + [row['error'] for row in removals if row.get('error')],
            'blocked': [row['blocked'] for row in rules if row.get('blocked')], 'duration_seconds': 0}


def _finish_removals(settings: dict, intent: dict) -> None:
    """Finalize only the current request, without saving the run's stale settings.

    This read/merge/write still needs transaction coordination against concurrent saves.
    A completed external action is history, not permission to remove a replacement rule.
    """
    operations = [op for op in intent.get('operations') or [] if op.get('removal_action')]
    if not any(op.get('status') == 'done' for op in operations):
        return
    current = load_settings()
    completed = set()
    for rule in current.get('rules') or []:
        queued = (rule.get('queue') or {}).get('removal') or {}
        request_id = queued.get('request_id')
        matching = [op for op in operations
                    if request_id and op.get('request_id') == request_id
                    and op.get('rule_id') == rule['id']
                    and op.get('removal_action') == queued.get('action')
                    and op.get('instance_id') == rule.get('instance_id')
                    and op.get('series_id') == rule.get('series_id')]
        if matching and all(op.get('status') == 'done' for op in matching):
            completed.add(rule['id'])
    if not completed:
        return
    current['rules'] = [rule for rule in current.get('rules', []) if rule['id'] not in completed]
    save_settings(current)
    health = load_health(current)
    for rule_id in completed:
        health['rules'].pop(rule_id, None)
    health['alerts'] = [a for a in (health.get('alerts') or []) if a.get('rule_id') not in completed]
    write_cache(current, 'health.json', health)


def _separate_removal_recovery(settings: dict, stored: dict, ledger: dict,
                               rule_ids=None, tmdb=None) -> tuple[list, set, set]:
    """Hand off removal checkpoints before replacing ordinary work; isolate failures.

    An exact archive precedes the ledger handoff. The source run ID makes that handoff
    idempotent after interruption. Only the ledger owns migrated removal checkpoints;
    new run summaries carry their readings, never executable copies of those operations.
    """
    if stored:
        archive_intent(settings, stored)
        if stored.get('removals') or any(
                op.get('removal_action') for op in stored.get('operations') or []):
            if not any(batch['id'] == stored['id'] for batch in ledger['batches']):
                ledger['batches'].append(json.loads(json.dumps({
                    'id': stored['id'], 'removals': stored.get('removals') or [],
                    'operations': [op for op in stored.get('operations') or []
                                   if op.get('removal_action')]})))
                save_removal_ledger(settings, ledger)

    owners = {}
    for batch in ledger['batches']:
        for op in batch['operations']:
            key = (op.get('rule_id'), op.get('request_id'))
            if key in owners and owners[key] != batch['id']:
                raise Rejected('Removal request has overlapping recovery batches; refusing ambiguous ownership.')
            owners[key] = batch['id']
    # Restaging runs before the ordinary eligibility filter. Precompute target owners
    # across all batches so a replacement cannot bypass a later batch's hold.
    target_owners = {}
    for batch in ledger['batches']:
        for op in batch['operations']:
            if op.get('status') != 'done':
                target = (op.get('instance_id'), op.get('series_id'))
                target_owners.setdefault(target, set()).add(op.get('rule_id'))
    records, held, targets = [], set(), set()
    scope = set(rule_ids) if rule_ids else None
    for batch in ledger['batches']:
        operations = batch['operations']
        # Operations are authority; display records are optional historical metadata.
        batch_records = list(batch['removals'])
        for op in operations:
            if not any(record.get('rule_id') == op.get('rule_id')
                       and record.get('action') == op.get('removal_action') for record in batch_records):
                batch_records.append({'rule_id': op.get('rule_id'), 'action': op['removal_action'],
                                      'ok': True, 'error': ''})
        for record in batch_records:
            rule_id = record.get('rule_id')
            matching = [op for op in operations if op.get('rule_id') == rule_id
                        and op.get('removal_action') == record.get('action')]
            if not matching and record.get('retired_without_operation'):
                continue
            # An acknowledged request still present needs cleanup, not restaging.
            queued = next((r for r in settings.get('rules') or [] if r['id'] == rule_id), {})
            request_id = ((queued.get('queue') or {}).get('removal') or {}).get('request_id')
            pending_cleanup = any(op.get('request_id') == request_id and request_id
                                  for op in matching)
            if matching and all(op.get('status') == 'done' for op in matching) and not pending_cleanup:
                continue
            held.add(rule_id)
            targets.update((op.get('instance_id'), op.get('series_id')) for op in matching)
            if scope is not None and rule_id not in scope:
                continue
            report = dict(record)
            freshly_staged = False
            if not matching:
                # No operation ever existed: there is no uncertain external effect to
                # reconcile. Only the live queue can authorize a fresh decision.
                if not (queued.get('queue') or {}).get('removal'):
                    record['retired_without_operation'] = True
                    save_removal_ledger(settings, ledger)
                    if not any(key[0] == rule_id for key in owners):
                        held.discard(rule_id)
                    report.update(ok=False, error=(record.get('error') or 'Removal was not staged')
                                  + '; request canceled or rule removed before staging')
                    records.append(report)
                    continue
                if any(key[0] == rule_id for key in owners):
                    report.update(ok=False, error='Another removal checkpoint owns this rule; recovery requires review.')
                    records.append(report)
                    continue
                bind_rules(settings, force=True)
                target = (queued.get('instance_id'), queued.get('series_id'))
                if target_owners.get(target, set()) - {rule_id}:
                    report.update(ok=False, error='Target held by another removal checkpoint')
                    records.append(report)
                    continue
                if (queued.get('match_status') != 'matched'
                        or not record.get('request_id')
                        or target != (record.get('instance_id'), record.get('series_id'))):
                    report.update(ok=False, error='Unstaged removal target cannot be verified; recovery requires review.')
                    records.append(report)
                    continue
                staged_records, matching = _removal_stage(settings, [queued], tmdb)
                if not matching:
                    report.update(staged_records[0])
                    records.append(report)
                    continue
                record.update(staged_records[0])
                report = dict(record)
                operations.extend(matching)
                for op in matching:
                    owners[(rule_id, op.get('request_id'))] = batch['id']
                    target_owners.setdefault(target, set()).add(rule_id)
                    targets.add((op.get('instance_id'), op.get('series_id')))
                save_removal_ledger(settings, ledger)
                freshly_staged = True
            view = {'id': batch['id'], 'operations': matching, 'removals': [record]}

            def checkpoint(_settings, _intent):
                nonlocal settings, ledger
                save_removal_ledger(settings, ledger)

            try:
                # Validate target continuity before even read-only reconciliation.
                for op in matching:
                    _validate_removal_request(settings, op)
                    if (op.get('instance_id'), op.get('series_id')) != (
                            queued.get('instance_id'), queued.get('series_id')):
                        raise Rejected('Saved removal target changed; recovery requires review.')
                if not freshly_staged:
                    _resume_intent(settings, view, checkpoint=checkpoint)
            except (Rejected, SonarrError) as error:
                report.update(ok=False, error=str(error))
            else:
                for op in matching:
                    if op.get('status') != 'done':
                        _execute_operation(settings, view, op, checkpoint=checkpoint)
                        if op.get('status') != 'done':
                            break
                _finish_removals(settings, view)
                done = all(op.get('status') == 'done' for op in matching)
                report.update(ok=done, error='' if done else next(
                    (op.get('error') for op in matching if op.get('error')),
                    'A staged removal was not completed'))
            records.append(report)
    return records, held, targets


def run(preview: bool = False, rule_ids=None, scheduled: bool = False) -> dict:
    """Read the whole run, save its intent, then perform only those saved writes."""
    settings = load_settings()
    test_mode = bool((settings.get('schedule') or {}).get('test_mode', True))
    dry_run = preview or test_mode
    started, clock = now_iso(), time.monotonic()

    # Test Mode is literal: it does not bind, cache, journal, or create an intent. It
    # still reads and decides the full run so its report is useful. The scheduler writes
    # one separate operational log line after a scheduled test pass: it records only
    # that the pass happened and its plan count, never a retained run outcome.
    if not dry_run:
        log_line(settings, 'info', ('scheduled ' if scheduled else '') + 'run started')
        bind_rules(settings)
    tmdb = tmdb_provider(settings)
    selected = [rule for rule in settings.get('rules', []) if rule.get('enabled')]
    if rule_ids:
        selected = [rule for rule in selected if rule['id'] in set(rule_ids)]
    queued = [rule for rule in settings.get('rules', []) if (rule.get('queue') or {}).get('removal')]
    if rule_ids:
        queued = [rule for rule in queued if rule['id'] in set(rule_ids)]

    if dry_run:
        intent = _stage_intent(settings, selected, queued, tmdb, remember=False)
        summary = _intent_summary(intent, preview, test_mode, scheduled, started)
        summary['duration_seconds'] = round(time.monotonic() - clock, 1)
        return summary

    stored = load_intent(settings)
    # Completed removals are historical outcomes, not permission to replay ordinary
    # work. Outstanding one-time requests have the same ledger owner regardless of
    # whether the original run also contained ordinary retention or was marked done.
    removal_operations = [op for op in (stored or {}).get('operations') or []
                          if op.get('removal_action')]
    completed_removals = {(op.get('rule_id'), op['removal_action'])
                          for op in removal_operations if op.get('status') == 'done'}
    unresolved_removals = any(op.get('status') != 'done' for op in removal_operations) or any(
        not record.get('ok') or (record.get('rule_id'), record.get('action')) not in completed_removals
        for record in (stored or {}).get('removals') or [])
    # An acknowledged operation whose queue is still present needs local finalization,
    # not a freshly staged copy of the same external action (e.g. crash before cleanup).
    current_requests = {(rule['id'], removal.get('request_id'), removal.get('action'))
                        for rule in settings.get('rules') or []
                        if (removal := (rule.get('queue') or {}).get('removal'))}
    awaiting_finalization = any(
        (op.get('rule_id'), op.get('request_id'), op.get('removal_action')) in current_requests
        for op in removal_operations if op.get('status') == 'done')
    ledger = load_removal_ledger(settings)
    if (stored or {}).get('separate_removals') and not ledger['batches']:
        raise Rejected('Removal recovery ledger is missing; refusing to restage handed-off requests.')
    separate = bool(ledger['batches']) or bool(
        unresolved_removals or awaiting_finalization)
    if separate:
        reports, held, targets = _separate_removal_recovery(settings, stored, ledger, rule_ids, tmdb)
        settings = load_settings()
        bind_rules(settings, force=True)
        eligible = [rule for rule in settings.get('rules') or []
                    if rule['id'] not in held
                    and (rule.get('instance_id'), rule.get('series_id')) not in targets
                    and (not rule_ids or rule['id'] in rule_ids)]
        selected = [rule for rule in eligible if rule.get('enabled')]
        queued = [rule for rule in eligible if (rule.get('queue') or {}).get('removal')]
        for rule in settings.get('rules') or []:
            if (rule['id'] not in held and (rule.get('instance_id'), rule.get('series_id')) in targets
                    and (not rule_ids or rule['id'] in rule_ids)):
                reports.append({'rule_id': rule['id'], 'ok': False,
                                'error': 'Target held by unresolved removal recovery'})
        intent = _stage_intent(settings, selected, queued, tmdb)
        # Only reports are copied. Ledger operations never return to run-intent.json.
        intent['recovery_removals'] = reports
        intent['separate_removals'] = True
        save_intent(settings, intent)
    else:
        if stored and stored.get('status') != 'complete':
            archive_intent(settings, stored)
            # Catalogue caching must not lend stale identity to a fresh retry plan.
            bind_rules(settings, force=True)
        intent = _stage_intent(settings, selected, queued, tmdb)
        save_intent(settings, intent)
    for operation in intent.get('operations') or []:
        if operation.get('status') in ('done',):
            continue
        _execute_operation(settings, intent, operation)
        if operation.get('status') != 'done':
            # Conservative until explicit dependencies land: a failed unmonitor must
            # never be followed by a deletion, including after an uncertain response.
            break
    _finish_removals(settings, intent)
    intent['status'] = 'complete' if (
        all(op.get('status') == 'done' for op in intent.get('operations') or [])
        and all(record.get('ok') for record in (intent.get('removals') or [])
                + (intent.get('recovery_removals') or []))) else 'incomplete'
    intent['finished'] = now_iso()
    save_intent(settings, intent)
    summary = _intent_summary(intent, preview, test_mode, scheduled, started)
    summary['duration_seconds'] = round(time.monotonic() - clock, 1)
    state = load_state(settings)
    state['runs'] = state.get('runs', []) + [{
        'id': summary['id'], 'started': summary['started'], 'finished': summary['finished'],
        'scheduled': scheduled, 'dry_run': False, 'planned': summary['planned'],
        'deleted': summary['deleted'], 'freed_bytes': summary['freed_bytes'], 'errors': summary['errors'][:10]}]
    state['last_run'] = summary
    save_state(settings, state)
    journal(settings, summary)
    log_line(settings, 'warning', f'run finished: {summary["planned"]} planned, {summary["deleted"]} deleted, '
             f'{summary["freed_bytes"] // 1024 // 1024} MiB')
    if summary['errors']:
        log_line(settings, 'error', 'run errors: ' + '; '.join(summary['errors'])[:600])
    return summary


def catalogue_for(settings: dict, instance_id: str, force: bool = False) -> list:
    """The instance's series list, cached on disk.

    Served from the store, never on a timer. The sync is what refreshes it, so browsing
    three thousand series costs a file read and the interface never waits on Sonarr. It is
    fetched here only when there is nothing stored at all — a first run, or a mapping
    change that retired what was — because an empty picker is worse than a pause.

    The store holds *mapped* series, so it is keyed by the mapping's shape: a field added
    without that key moving reads as absent everywhere until the entry is replaced. That
    is exactly how the ended pill stayed blank once before.
    """
    cache = read_cache(settings, 'catalogue.json')
    entry = cache.get(instance_id) or {}
    stored = entry.get('series') if entry.get('schema') == SCHEMA else None
    if stored and not force:
        return stored
    series = client_for(settings, instance_id).series()
    cache[instance_id] = {'schema': SCHEMA, 'fetched_at': now_iso(), 'series': series}
    write_cache(settings, 'catalogue.json', cache)
    return series


def series_record(settings: dict, rule: dict) -> dict:
    """The rule's series, from the store, or read on its own if it is not there yet."""
    entry = read_cache(settings, 'catalogue.json').get(rule.get('instance_id')) or {}
    if entry.get('schema') == SCHEMA:
        for series in entry.get('series') or []:
            if series.get('series_id') == rule.get('series_id'):
                return series
    # Not in the store: a series added since the last sync, being read on demand.
    try:
        return client_for(settings, rule['instance_id']).series_one(rule['series_id'])
    except Rejected:
        return {}


def episodes_for(settings: dict, rule: dict, force: bool = False, offline: bool = False,
                 preloaded: list = None, persist: bool = True) -> tuple:
    """One rule's reading of Sonarr: its episodes, its series record, and when it was read.

    Fetches when asked to, when nothing is stored, or when what is stored has aged past
    the health TTL. Otherwise the stored reading is returned with its true age, so a plan
    computed from it can be shown as what it is: current arithmetic over a known-old read.

    The series record is stored with the episodes rather than read separately, or serving
    a cached reading would still cost a call — a small one, but one per series per check.

    `preloaded`, when given, is a fresh full reading of the same series the caller already
    holds — `process_rule`'s own fetch for the same rule, moments earlier in the same
    run — so this stores it and returns it exactly as a fetch here would, without asking
    Sonarr for the same series twice.

    One difference is worth knowing rather than fixing: `preloaded` comes from
    `collect_episodes`, which fills air dates from TMDB when a key is configured; the
    fetch below never does. A `process_rule` run therefore stores a TMDB-filled reading,
    which a later, unrelated `monitoring_for(force=True)` call without a preloaded
    reading can overwrite with a plain one. Nothing acts on the stale write: the
    run that produced it already used the richer data in memory for its own decisions,
    and the next `process_rule` pass fills the cache in again. What can flicker is only
    what the interface shows from the cache in between, never what gets deleted.
    """
    if preloaded is not None:
        series = series_record(settings, rule)
        read_at = store_episodes(settings, rule, preloaded, series) if persist else now_iso()
        return preloaded, series, read_at, False
    cached, series, fetched_at = episode_cache(settings, rule)
    ttl = int((settings.get('health') or {}).get('ttl_hours', 24)) * 3600
    age = age_seconds(fetched_at)
    if not force and cached and age is not None and age < ttl:
        return cached, series, fetched_at, True
    if offline:
        # The TTL governs when to fetch, not what may be used: an old reading shown with
        # its true age beats no reading at all, and the caller promised not to go out.
        if not cached:
            raise Rejected('Nothing has been read for this series yet.')
        return cached, series, fetched_at, True
    client = client_for(settings, rule['instance_id'])
    episodes = client.episodes(rule['series_id'], files_only=False)
    enrich_air_dates(settings, rule, episodes, client=client)
    series = series_record(settings, rule)
    read_at = store_episodes(settings, rule, episodes, series) if persist else now_iso()
    return episodes, series, read_at, False


def reenable_returning_rules(settings: dict, before: dict, after: dict) -> list:
    """Re-enable armed, disabled rules when an ended series becomes active again.

    Two triggers, because they catch different moments and neither covers the other.

    **Status** is the slow case: Sonarr marks a show continuing when a return is
    announced, often months before any date exists.

    **The air-date watermark** is the fast case, and the one a status check alone cannot
    see. A streaming service drops a whole season at once, so Sonarr un-ends the series
    and re-ends it within a refresh interval, so `before` and `after` can both say
    ended and the transition is never observed. A status flag is edge-triggered on a value
    that resets itself. A watermark only moves forward, so it cannot be missed however the
    syncs happen to fall.
    """
    changed = []
    for rule in settings.get('rules', []):
        if rule.get('enabled') or not rule.get('auto_reenable') \
                or rule.get('match_status') != 'matched':
            continue
        key = (rule.get('instance_id'), rule.get('series_id'))
        was, series = before.get(key), after.get(key)
        if not was or not series:
            continue
        resumed = bool(was.get('ended')) and not bool(series.get('ended'))
        reason = 'the series resumed' if resumed else ''
        if not reason:
            mark = rule.get('auto_reenable_after') or ''
            latest = latest_air_date(settings, rule)
            # Only ever forward: Sonarr revising dates on a refresh can move one backwards,
            # and that is not news of anything.
            if mark and latest and latest > mark:
                reason = 'a newer episode has aired or been scheduled'
        if not reason:
            continue
        rule['enabled'] = True
        # One-shot: a later manual disable must stay disabled unless this is explicitly
        # armed again. The watermark goes with it, so re-arming takes a fresh reading
        # rather than resurrecting one from a situation nobody is in any more.
        rule['auto_reenable'] = False
        rule['auto_reenable_after'] = ''
        changed.append((series.get('title') or rule.get('series_title') or rule['path'], reason))
    return changed


def latest_air_date(settings: dict, rule: dict) -> str:
    """This rule's air-date watermark, from the stored episodes. No network."""
    cached, _, _ = episode_cache(settings, rule)
    return air_watermark(cached or [], settings, rule)


def refresh_armed_episodes(settings: dict, report: dict) -> None:
    """Re-read episodes for disabled rules waiting to be re-enabled.

    The sync's own episode pass covers enabled rules only, and runs after the re-enable
    check besides — so an armed rule's stored episodes would never move and its watermark
    could never rise. One call each, and only for rules somebody explicitly armed.
    """
    for rule in settings.get('rules', []):
        if rule.get('enabled') or not rule.get('auto_reenable') \
                or not rule.get('auto_reenable_after') or rule.get('match_status') != 'matched':
            continue
        try:
            client = client_for(settings, rule['instance_id'])
            episodes = client.episodes(rule['series_id'], files_only=False)
            enrich_air_dates(settings, rule, episodes, client=client)
        except (Rejected, SonarrError) as error:
            report['errors'].append(f'{rule.get("series_title") or rule["path"]}: {error}')
            continue
        store_episodes(settings, rule, episodes, series_record(settings, rule))


def sync_from_sonarr(settings: dict, reason: str = 'scheduled') -> dict:
    """Read Sonarr once, store what it says, and report what moved.

    This is the only place the plugin reads Sonarr without being asked to. Everything the
    interface does — browsing series, widening a keep window, deciding what a run would
    delete — is answered from what this leaves behind, so the page never waits on Sonarr
    and a rule edited at midnight costs nothing at all.

    Series come at series level for the whole library, because that payload already
    carries seasons, episode counts, sizes and air dates: everything a list or a grid
    wants. Episodes come only for series under a rule, because only a retention decision
    needs them, and any other series can be read on demand in about thirty milliseconds.

    Nothing is written unless it differs. The comparison saves no network — a change to a
    monitored flag is invisible until the episodes are read — but it says what changed
    since yesterday, which is worth more than the writes it avoids.
    """
    started = now_iso()
    report = {'started': started, 'reason': reason, 'series_added': [], 'series_removed': 0,
              'series_changed': 0, 'series_reenabled': [], 'episodes_changed': [], 'errors': []}
    catalogue = read_cache(settings, 'catalogue.json')
    before, after = {}, {}

    for instance in settings.get('instances', []):
        if not instance.get('enabled', True):
            continue
        try:
            fresh = Sonarr(instance).series()
        except Rejected as error:
            report['errors'].append(f'{instance["name"]}: {error}')
            continue
        entry = catalogue.get(instance['id']) or {}
        previous = {series['series_id']: series for series in (entry.get('series') or [])
                    if entry.get('schema') == SCHEMA}
        before.update({(instance['id'], series_id): series for series_id, series in previous.items()})
        for series in fresh:
            after[(instance['id'], series['series_id'])] = series
            was = previous.get(series['series_id'])
            if was is None:
                # Only news on a library we have seen before; the first sync is the
                # baseline, and announcing three thousand series is true and useless.
                if previous:
                    report['series_added'].append(series['title'])
            elif canonical_json(was) != canonical_json(series):
                report['series_changed'] += 1
        report['series_removed'] += len(set(previous) - {s['series_id'] for s in fresh})
        catalogue[instance['id']] = {'schema': SCHEMA, 'fetched_at': now_iso(), 'series': fresh}

    write_cache(settings, 'catalogue.json', catalogue)
    refresh_armed_episodes(settings, report)
    reenabled = reenable_returning_rules(settings, before, after)
    if reenabled:
        report['series_reenabled'] = [title for title, _ in reenabled]
        save_settings(settings)
        for title, reason_text in reenabled:
            log_line(settings, 'info', f'{title}: automatically re-enabled because {reason_text}')

    for rule in settings.get('rules', []):
        if not rule.get('enabled') or rule.get('match_status') != 'matched':
            continue
        try:
            client = client_for(settings, rule['instance_id'])
            episodes = client.episodes(rule['series_id'], files_only=False)
            enrich_air_dates(settings, rule, episodes, client=client)
        except Rejected as error:
            report['errors'].append(f'{rule.get("series_title") or rule["path"]}: {error}')
            continue
        stored, _, _ = episode_cache(settings, rule)
        if stored is None or canonical_json(stored) != canonical_json(episodes):
            report['episodes_changed'].append(rule.get('series_title') or rule['path'])
        store_episodes(settings, rule, episodes, series_record(settings, rule))
        with contextlib.suppress(Rejected, SonarrError):
            check_one_rule(settings, rule)

    mark_synced(settings, report)
    if report['series_added']:
        names = ', '.join(report['series_added'][:8])
        log_line(settings, 'info', f'Sonarr added {len(report["series_added"])} series: {names}')
    log_line(settings, 'info',
             f'sync ({reason}): {report["series_changed"]} series changed, '
             f'{len(report["series_added"])} added, {report["series_removed"]} removed, '
             f'{len(report["series_reenabled"])} re-enabled, '
             f'{len(report["episodes_changed"])} of the managed series moved')
    return report


def mark_synced(settings: dict, report: dict) -> None:
    write_cache(settings, 'sync.json', dict(report, synced_at=now_iso()))


def last_sync(settings: dict) -> dict:
    return read_cache(settings, 'sync.json')


def sync_is_due(settings: dict, max_age_seconds: int = SYNC_FRESH_SECONDS) -> bool:
    """True when the stored reading has aged past the interval, or there is none."""
    age = age_seconds(last_sync(settings).get('synced_at'))
    return age is None or age > max_age_seconds


def status_snapshot(settings: dict, health=None) -> dict:
    """Read-only operational status for System → Status and its sidebar badge."""
    health = health if health is not None else load_health(settings)
    state_path = Path(settings.get('state_dir') or '')
    config_readable = (not CONFIG.exists() or os.access(CONFIG, os.R_OK))
    config_writable = CONFIG.parent.exists() and os.access(CONFIG.parent, os.W_OK)
    config_ok = config_readable and config_writable
    state_ok = state_path.exists() and os.access(state_path, os.R_OK | os.W_OK)
    backup_cfg = settings.get('backup') or {}
    backup_path = Path(backup_cfg.get('path')) if backup_cfg.get('path') else None
    backup_ok = bool(backup_path and backup_path.exists() and
                     os.access(backup_path, os.R_OK | os.W_OK))
    status_alerts = []
    if not config_ok or not state_ok:
        status_alerts.append(alerts.make('state-unavailable', detail=str(state_path)))
    if backup_cfg.get('last_error'):
        status_alerts.append(alerts.make('backup-failed', detail=backup_cfg['last_error']))
    elif backup_cfg.get('path') and not backup_ok:
        status_alerts.append(alerts.make('backup-unavailable', detail=str(backup_path)))
    sync = last_sync(settings)
    sync_age = age_seconds(sync.get('synced_at'))
    if sync_age is None or sync_age > SYNC_FRESH_SECONDS * 2:
        status_alerts.append(alerts.make('sync-stale', detail='No recent Sonarr library sync'))
    current = list(health.get('alerts') or [])
    # Avoid adding an installation alert twice when a caller has already refreshed health.
    known = {alert.get('key') for alert in current}
    status_alerts = [alert for alert in status_alerts if alert.get('key') not in known]
    progress = read_progress(settings)
    state = load_state(settings)
    jobs = job_state(settings)
    recent = sorted((health.get('alerts') or []),
                    key=lambda alert: alert.get('last_seen') or alert.get('first_seen') or '',
                    reverse=True)[:20]
    return {
        'version': VERSION,
        'uptime_seconds': max(0, int(time.time() - PROCESS_STARTED)),
        'test_mode': bool((settings.get('schedule') or {}).get('test_mode', True)),
        'schedule': dict((settings.get('schedule') or {})),
        'sync': dict(sync, age_seconds=sync_age, running=bool(progress.get('running'))),
        'last_run': state.get('last_run'),
        'last_scheduled_run': jobs.get('last_run'),
        'pending_run': state.get('pending_run') or jobs.get('pending_run'),
        'progress': progress,
        'instances': dict(health.get('instances') or {}),
        'storage': {'config': str(CONFIG), 'config_readable': config_readable,
                    'config_writable': config_writable, 'state_dir': str(state_path),
                    'state_readable': state_path.exists() and os.access(state_path, os.R_OK),
                    'state_writable': state_ok,
                    'backup_path': str(backup_path or ''), 'backup_valid': backup_ok},
        'api_key': {key: value for key, value in (settings.get('api_key') or {}).items()
                    if key != 'hash'},
        'alerts': status_alerts,
        'recent': recent,
    }


def recompute_plans(settings: dict, health: dict) -> int:
    """Re-decide every rule from the reading already stored. No network at all.

    Time moves a keep window on its own: an episode inside it this morning is outside it
    tonight, and a page left open would otherwise go on showing the plan it was handed
    when it loaded. Deciding is arithmetic — the whole library in a few hundredths of a
    second — so the page can afford to do it on every heartbeat. Nothing is written unless
    a rule's result actually differs, or an open page would rewrite the cache every
    fifteen seconds to say nothing had changed.
    """
    changed = 0
    for rule in settings.get('rules', []):
        if not rule.get('enabled'):
            continue
        try:
            fresh = monitoring_for(settings, rule, offline=True)
        except Rejected:
            continue          # never read; the check queue will fetch it
        if not fresh.get('ok'):
            continue
        fresh['fingerprint'] = rule_fingerprint(rule, settings)
        previous = (health.get('rules') or {}).get(rule['id']) or {}
        # Compared with the old timestamp in place, so "nothing changed" is about the
        # result and not about when it was worked out.
        fresh['checked_at'] = previous.get('checked_at') or now_iso()
        if canonical_json(fresh) == canonical_json(previous):
            continue
        fresh['checked_at'] = now_iso()
        health.setdefault('rules', {})[rule['id']] = fresh
        others = [alert for alert in (health.get('alerts') or []) if alert.get('rule_id') != rule['id']]
        health['alerts'] = alerts.merge(health.get('alerts') or [],
                                        others + alerts_for_rule(settings, rule, fresh))
        changed += 1
    return changed

def log_settings_change(settings: dict, previous: dict, updated: dict) -> None:
    """Say what a save actually changed, so the log answers "when did this become true?"."""
    for key in ('schedule', 'retention', 'logging', 'health', 'alerts', 'state_dir',
                'log_retention_runs', 'instances', 'profiles', 'rules', 'tmdb',
                'connections', 'api_key', 'backup', 'air_dates'):
        if canonical_json(previous.get(key)) == canonical_json(updated.get(key)):
            continue
        if key == 'schedule':
            log_line(settings, 'info', 'schedule saved: '
                     + (schedules.describe(updated.get('schedule') or {})
                        if (updated.get('schedule') or {}).get('enabled') else 'disabled'))
        elif key in ('instances', 'profiles', 'rules'):
            log_line(settings, 'info', f'{key} changed: {len(previous.get(key) or [])} '
                                       f'-> {len(updated.get(key) or [])}')
        else:
            log_line(settings, 'info', f'{key} settings saved')


def announce_alerts(settings: dict, previous: list, current: list) -> list:
    """Notify about problems that were not there before.

    Alerts are keyed and carry their own first-seen date, so "new" is a question about the
    keys, not about the check that happened to find them: a condition that has been true
    since Tuesday is not announced again on Wednesday.

    What is worth announcing is a property of the alert's kind: something structurally
    wrong — a series that cannot be found, a binding that moved, a Sonarr that will not
    answer. Never the retention itself, which is the job, not the news.
    """
    suppressed = set((load_health(settings).get('suppressed') or {}).keys())
    known = {alert['key'] for alert in previous or []}
    fresh = [alert for alert in current or []
             if alert['key'] not in known and alert.get('key') not in suppressed]
    for alert in fresh:
        log_line(settings, 'warning', f'{alert["title"]} — {alert.get("detail", "")}')
    return fresh



def stale_rule_ids(settings: dict, health: dict) -> list:
    """The enabled rules whose cached result is missing, outdated, or no longer applicable.

    Only these are re-read when the page opens. Everything else is served from the cache,
    which is the whole point of keeping one.
    """
    ttl = int(settings.get('health', {}).get('ttl_hours', 24)) * 3600
    dirty = set(health.get('dirty') or [])
    stale = []
    for rule in settings.get('rules', []):
        if not rule.get('enabled'):
            continue
        # Sonarr reported a change to this series: nothing cached for it can be trusted,
        # whatever its age says.
        if rule['id'] in dirty:
            stale.append(rule['id'])
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


def disable_expired_rule(settings: dict, rule: dict, state: dict) -> bool:
    """Switch off a rule whose series has ended with nothing left inside its window.

    No further episodes are coming and nothing remains for a run to act on, so the rule
    can only sit there being evaluated for ever. A series whose remaining files are all
    excluded arrives here too, and correctly: exclusions are set aside before the keep
    frame is computed, so a series holding nothing else has an empty frame.

    Nothing is deleted and the rule is not removed — switching back on is one click, and
    the notice that says this happened is the one alert kind that survives the disable.
    """
    if not rule.get('enabled') or state.get('lifecycle') not in ('ended_expired', 'ended_empty'):
        return False
    title = state.get('series_title') or rule.get('series_title') or rule['path']
    if bool((settings.get('schedule') or {}).get('test_mode', True)):
        log_line(settings, 'info',
                 f'{title}: would be switched off — ended with nothing inside the keep '
                 f'window. Test Mode, so nothing was changed.')
        return False
    stored = load_settings()
    target = next((r for r in stored.get('rules') or [] if r['id'] == rule['id']), None)
    if not target or not target.get('enabled'):
        return False
    target['enabled'] = False
    rule['enabled'] = False
    save_settings(stored)
    settings['rules'] = stored['rules']
    log_line(settings, 'info',
             f'{title}: switched off — the series has ended and nothing remains inside '
             f'its keep window')
    return True


def check_one_rule(settings: dict, rule: dict, instance_state: dict = None, force: bool = False) -> dict:
    """Verify one rule, cache the result, and refresh that rule's alerts.

    Only this rule's alerts are replaced; everything else in the set is left alone, so a
    single-series recheck cannot clear a problem it never looked at.
    """
    health = load_health(settings)
    dirty = list(health.get('dirty') or [])
    if instance_state and not instance_state.get('ok', True):
        state = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'],
                 'ok': False, 'status': 'unmatched', 'label': 'Sonarr unavailable',
                 'error': instance_state.get('error', ''), 'checked_at': now_iso(),
                 'fingerprint': rule_fingerprint(rule, settings)}
    else:
        # A series Sonarr says changed is re-read whatever its age; everything else is
        # re-decided from what is already stored, which costs no call at all.
        state = monitoring_for(settings, rule, force=force or rule['id'] in dirty)
        state['checked_at'] = now_iso()
        state['fingerprint'] = rule_fingerprint(rule, settings)
    if rule['id'] in dirty and not state.get('from_cache') and state.get('ok'):
        health['dirty'] = [known for known in dirty if known != rule['id']]
    # The running commentary a person reads while something is happening. A read from
    # Sonarr is worth a line; one served from the store is detail.
    plan = state.get('plan') or {}
    summary = (f'{plan.get("delete", 0)} to delete, {plan.get("monitor", 0)} to monitor, '
               f'{plan.get("unmonitor", 0)} to unmonitor')
    log_line(settings, 'verbose' if state.get('from_cache') else 'info',
             f'checked {state.get("series_title")}: {summary}'
             + (' (from the stored reading)' if state.get('from_cache') else ' (read from Sonarr)'))
    was = (health.get('rules') or {}).get(rule['id']) or {}
    # Said once, when Sonarr first reports it, rather than on every check thereafter.
    if state.get('ended') and not was.get('ended'):
        log_line(settings, 'warning', f'{state.get("series_title")} has ended in Sonarr')
    disable_expired_rule(settings, rule, state)
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
    if state.get('status') == 'air-date-unresolved':
        return [alerts.make('air-date-unresolved', rule_id=rule['id'],
                            detail=state.get('error') or 'A judged episode has no air date')]
    if not state.get('ok'):
        return [alerts.make('unmatched', rule_id=rule['id'],
                            detail=state.get('error') or state.get('label') or 'Could not read Sonarr')]
    if state.get('lifecycle') in ('ended_expired', 'ended_empty'):
        return [alerts.make('ended-expired', rule_id=rule['id'],
                            detail='Sonarr reports this series as ended and nothing remains '
                                   'inside the keep window')]
    if state.get('lifecycle') == 'ended':
        return [alerts.make('ended', rule_id=rule['id'],
                            detail=f'Sonarr reports this series as ended. '
                                   f'{state.get("files_in_frame", 0)} of '
                                   f'{state.get("files_total", 0)} episodes are still inside '
                                   f'the keep window')]
    return []


def system_alerts(settings: dict, health: dict) -> list:
    """Alerts about the installation rather than about one series."""
    found = []
    for instance in settings.get('instances', []):
        if not instance.get('enabled', True):
            continue
        state = (health.get('instances') or {}).get(instance['id']) or {}
        # Absence is not a failed check. Never invent a recycle-bin warning before this
        # instance has actually answered the question once.
        if not state.get('checked_at'):
            continue
        if state.get('reachable') is False:
            found.append(alerts.make('sonarr-unreachable', instance_id=instance['id'],
                                     detail=f'{instance["name"]}: {state.get("error", "no answer")}'))
            continue
        if not state.get('recycle_bin'):
            found.append(alerts.make('no-recycle-bin', instance_id=instance['id'],
                                     detail=f'{instance["name"]} deletes files outright; nothing is recoverable'))
    return found


def refresh_instance_health(settings: dict, instance_ids=None, force: bool = False) -> dict:
    """Refresh only connection health and merge its system alerts into the health cache."""
    wanted = set(instance_ids or [])
    health = load_health(settings)
    instances = dict(health.get('instances') or {})
    live = {instance['id'] for instance in settings.get('instances', [])}
    for gone in set(instances) - live:
        instances.pop(gone, None)
    for instance in settings.get('instances', []):
        if wanted and instance['id'] not in wanted:
            continue
        instances[instance['id']] = check_instance(settings, instance, force=force)
    health['instances'] = instances
    previous = health.get('alerts') or []
    series = [alert for alert in previous if alert.get('scope') != 'system']
    health['alerts'] = alerts.merge(previous, series + system_alerts(settings, health))
    write_cache(settings, 'health.json', health)
    return health


def run_health_check(scheduled: bool = False, force: bool = True) -> dict:
    """Verify instances, matches, folders, and monitoring, and cache the results.

    Read-only against both Sonarr and the filesystem: it never deletes and never changes a
    monitored flag. Progress is published rule by rule, so an interface open while this
    runs can show each show updating instead of waiting for the whole sweep.
    """
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
            # The scheduled sweep is the backstop: it reads every series outright, which
            # is what catches monitoring toggled by hand in Sonarr — the one change no
            # cheap signal reports. Fourteen megabytes and about a second, once a day.
            state = check_one_rule(settings, rule, instances.get(rule['instance_id']), force=force)
            checked += 1
            found.extend(alerts_for_rule(settings, rule, state))

        health = load_health(settings)
        # Results for rules that no longer exist would otherwise accumulate for ever and
        # make the counts disagree with the list on screen.
        live = {rule['id'] for rule in settings.get('rules', [])}
        for gone in set(health.get('rules') or {}) - live:
            forget_episodes(settings, gone)
        health['rules'] = {rid: entry for rid, entry in (health.get('rules') or {}).items() if rid in live}
        health['dirty'] = [rid for rid in (health.get('dirty') or []) if rid in live]
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
    reportable = alerts.managed_only(health['alerts'], settings)
    summary = alerts.summarise(reportable)
    # One notification per problem, the first time it appears. The summary this replaced
    # was re-sent by every sweep for as long as the problem stayed true, so a Sonarr that
    # had been unreachable since Tuesday said so again every day — which teaches people to
    # ignore the notification that matters.
    announce_alerts(settings, alerts.managed_only(previous, settings), reportable)
    log_line(settings, 'warning' if summary['error'] else 'verbose',
             f'series match check: {checked} rule(s), {summary["error"]} error(s), '
             f'{summary["warning"]} warning(s)')
    return health




def tick() -> int:
    """Decide what is due and do it. Invoked every minute by the plugin's cron entry.

    Nothing here is expensive unless something is actually due: the common case is reading
    two small files and comparing timestamps.
    """
    settings = load_settings()
    # Schedule fields describe the container's local civil time. Timestamps retained in
    # state stay UTC, but `is_due` compares aware datetimes correctly across the offset.
    # Calling `now(...UTC)` here made TZ affect the log stamp but not "daily at 1am".
    now = dt.datetime.now().astimezone()
    state = job_state(settings)
    actions = []

    connectivity = CONNECTIVITY_SECONDS
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

    # Refresh regularly even with no page open. Browsing and editing still use the stored
    # reading; only this resident path and explicit background requests contact Sonarr.
    if sync_is_due(settings):
        with contextlib.suppress(Rejected, SonarrError):
            with run_lock():
                report = sync_from_sonarr(settings, reason='due')
            actions.append(f'synced with Sonarr: {report["series_changed"]} series changed')

    if schedules.is_due(settings.get('schedule') or {}, now, state.get('last_run')):
        if not sonarr_reachable(settings):
            # Queued rather than skipped: exactly one pending run, so an outage over a
            # weekend produces one catch-up rather than a backlog.
            state['pending_run'] = now_iso()
            actions.append('run queued: Sonarr is unreachable')
        else:
            # Reconcile immediately before acting. The stored reading is what the plan was
            # built from, and a run is the one moment that must not act on it blind.
            with contextlib.suppress(Rejected, SonarrError):
                sync_from_sonarr(settings, reason='before the run')
            state['last_run'] = now_iso()
            save_job_state(settings, state)
            scheduled_test = bool((settings.get('schedule') or {}).get('test_mode', True))
            if scheduled_test:
                log_line(settings, 'info',
                         'scheduled test run started; Test Mode is on and nothing will change')
            try:
                with run_lock():
                    summary = run(preview=False, scheduled=True)
                    if summary.get('test_mode'):
                        log_line(settings, 'info',
                                 f'scheduled test run: {summary["planned"]} planned across '
                                 f'{len(summary["rules"])} rule(s); nothing changed')
                    else:
                        actions.append('scheduled run')
            except Rejected as error:
                log_line(settings, 'warning', f'scheduled run did not complete: {error}')
            state = job_state(settings)
            state['last_run'] = now_iso()

    save_job_state(settings, state)
    for message in actions:
        log_line(settings, 'verbose', f'tick: {message}')
    if not actions:
        log_line(settings, 'verbose', 'tick: nothing due')
    return 0



def monitoring_for(settings: dict, rule: dict, force: bool = False, offline: bool = False,
                   preloaded: list = None, persist: bool = True) -> dict:
    """Everything one check knows about a series: monitoring, lifecycle, and the plan.

    Entirely from Sonarr. Sizes, air dates, import dates, monitoring and season numbers all
    arrive with the episodes, so this touches no filesystem and needs no path mapping.

    The episodes come from the store unless `force` is set or they have aged out, so
    re-deciding what a rule would do costs nothing. Everything below the fetch is
    arithmetic, and it is redone every time.

    `preloaded` passes a fresh reading straight through to `episodes_for`; see there.
    """
    base = {'rule_id': rule['id'], 'series_title': rule.get('series_title') or rule['path'], 'ok': True}
    if rule.get('match_status') != 'matched':
        return dict(base, ok=False, error=rule.get('match_error') or 'Rule is not matched to a Sonarr series',
                    status='unmatched', label='Not matched to Sonarr')
    try:
        active = effective_rule(rule, settings.get('profiles'))
        episodes, series, read_at, from_cache = episodes_for(settings, rule, force=force, offline=offline,
                                                              preloaded=preloaded, persist=persist)
    except Rejected as error:
        return dict(base, ok=False, error=str(error), status='unmatched', label='Could not read Sonarr')

    state = dict(base, **classify_monitoring(episodes, active, settings))
    # The age shown is the age of the reading, not of the arithmetic over it: the plan is
    # always current, and saying so about the data behind it would be a lie.
    state['read_at'] = read_at
    state['from_cache'] = from_cache
    if series:
        state.update(describe_lifecycle(state, series))

    gaps = air_date_gaps(episodes, active, settings)
    if gaps and (settings.get('air_dates') or {}).get('still_unresolved', 'exclude') == 'disable':
        state.update(ok=False, air_date_unresolved=len(gaps),
                     error=f'{len(gaps)} episode(s) have no resolvable air date',
                     status='air-date-unresolved', label='Air dates unresolved')
    elif gaps:
        state['air_date_unresolved'] = len(gaps)

    if not state.get('ok'):
        state['plan'] = {'delete': 0, 'delete_bytes': 0, 'unmonitor': 0,
                         'unmonitor_missing': 0, 'monitor': 0,
                         'blocked': state.get('error') or 'Blocked', 'computed_at': now_iso()}
        return state
    present = [episode for episode in episodes if episode.get('has_file')]
    decision = evaluate(present, active, settings)
    would_delete = decision['delete']
    deleting = {item.get('episode_id') for item in would_delete}
    outside = state.get('out_frame_monitored') or []
    targets = monitoring_targets(settings, state, rule)
    # Deleting always unmonitors, and the run also unmonitors everything else outside the
    # window; both are counted here because both are changes someone would want to see.
    unmonitor = {row.get('episode_id') for row in outside} | deleting
    monitor = {row['episode_id'] for row in targets['monitor_list'] if row.get('episode_id')}
    state['plan'] = {
        'delete': len(would_delete),
        'delete_bytes': sum(int(item.get('size') or 0) for item in would_delete),
        'blocked': decision['blocked'] or '',
        'unmonitor': len([row for row in outside if row.get('episode_id') in unmonitor]),
        'unmonitor_missing': targets['unmonitor_missing'],
        'monitor': len(monitor),
        'computed_at': now_iso(),
    }
    return state



def check_recycle_bin(settings: dict, instance: dict) -> str:
    """Sonarr's recycle bin path, or '' when it has none. Cached with the instance state."""
    try:
        media = Sonarr(instance).media_management()
        return str(media.get('recycleBin') or '')
    except (SonarrError, Rejected):
        return ''


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli() -> int:
    parser = argparse.ArgumentParser(description='TV Retention worker')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('rpc', help='read one JSON request on stdin, write one JSON reply')
    run_parser = sub.add_parser('run', help='evaluate rules and delete')
    run_parser.add_argument('--scheduled', action='store_true')
    run_parser.add_argument('--preview', action='store_true')
    sub.add_parser('preview', help='evaluate rules without deleting')
    check_parser = sub.add_parser('check', help='verify Sonarr, matches, folders and monitoring')
    check_parser.add_argument('--scheduled', action='store_true')
    sub.add_parser('tick', help='decide what is due and run it, once')
    sub.add_parser('serve', help='stay alive: decide what is due, sleep, decide again')
    args = parser.parse_args()

    if args.command == 'rpc':
        try:
            request = json.loads(sys.stdin.read() or '{}')
        except json.JSONDecodeError:
            print(json.dumps({'ok': False, 'error': 'Malformed request'}))
            return 0
        # Imported here, not at the top: actions imports this module, and dispatching is
        # the only thing that needs it, so the dependency stays one way.
        from actions import dispatch
        print(json.dumps(dispatch(request if isinstance(request, dict) else {}), ensure_ascii=False))
        return 0
    if args.command in ('run', 'preview'):
        preview = args.command == 'preview' or getattr(args, 'preview', False)
        try:
            with run_lock(blocking=False):
                result = run(preview=preview, scheduled=getattr(args, 'scheduled', False))
        except Rejected as error:
            print(f'TV Retention: {error}', file=sys.stderr)
            return 1
        verb = 'would delete' if result['dry_run'] else 'deleted'
        print(f'TV Retention {verb} {result["planned"] if result["dry_run"] else result["deleted"]} files '
              f'across {len(result["rules"])} rules in {result["duration_seconds"]}s')
        for message in result['errors'] + result['blocked']:
            print(f'  ! {message}')
        return 0
    if args.command == 'serve':
        return serve_forever()
    if args.command == 'tick':
        return tick()
    if args.command == 'check':
        try:
            with run_lock(blocking=False):
                health = run_health_check(scheduled=getattr(args, 'scheduled', False))
        except Rejected as error:
            print(f'TV Retention: {error}', file=sys.stderr)
            return 1
        summary = alerts.summarise(health.get('alerts'))
        print(f'TV Retention checked {health.get("rules_checked", 0)} rule(s) in '
              f'{health.get("duration_seconds", 0)}s; {summary["error"]} error(s), '
              f'{summary["warning"]} warning(s)')
        for alert in health.get('alerts') or []:
            if alert['severity'] != alerts.NOTICE:
                print(f'  {alert["severity"]}: {alert["title"]} — {alert["detail"]}')
        return 0
    return 0


def serve_forever() -> int:
    """The process that stays alive: decide what is due, sleep, decide again.

    This is the whole of what a container buys. The plugin published a one-minute cron
    entry because it had no process of its own, and everything that made the tick clever —
    catching up a run missed while the machine was off, holding one until Sonarr answers —
    was arranging around that absence. Here it is a loop.

    Nothing is expensive unless something is due: the common case is reading two small
    files and comparing timestamps.
    """
    log_line(load_settings(), 'info', 'worker started')
    while True:
        try:
            tick()
        except Exception as error:  # noqa: BLE001 - a bad tick must never stop the loop
            with contextlib.suppress(Exception):
                log_line(load_settings(), 'error', f'tick failed: {error}')
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    sys.exit(cli())
