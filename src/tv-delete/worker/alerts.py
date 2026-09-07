#!/usr/bin/env python3
"""Things that need someone's attention, and whether they stop a series from running.

Two audiences, deliberately separated. A problem with one series belongs on that series'
card, because that is where the fix is; a problem with the whole system belongs in the
Alerts tab, because stamping "Sonarr unreachable" on thirty-six cards says nothing useful
thirty-six times.

An alert is a fact about the present, not an event log: it is keyed, so a condition that
persists across ten checks is one alert with a first-seen date, and it disappears by
itself when the condition clears.
"""
from __future__ import annotations

import datetime as dt

ERROR = 'error'
WARNING = 'warning'
NOTICE = 'notice'
SEVERITIES = [ERROR, WARNING, NOTICE]

# What each kind of alert means, whether it stops the series from being processed, and the
# action offered against it. Blocking is a property of the kind, not a judgement made at
# the call site, so the same condition always has the same consequence.
KINDS = {
    'unmatched': {
        'severity': ERROR, 'blocking': True, 'scope': 'series',
        'title': 'Not matched to a Sonarr series',
        'help': 'This rule no longer resolves to exactly one series. It is skipped by every '
                'run until it does.',
        'action': 'rematch',
    },
    'folder-missing': {
        'severity': ERROR, 'blocking': True, 'scope': 'series',
        'title': 'Folder is missing',
        'help': 'Sonarr reports files for this series, but its folder is not on this server. '
                'Check the root mapping for its instance.',
        'action': 'open-instance',
    },
    'path-changed': {
        'severity': WARNING, 'blocking': False, 'scope': 'series',
        'title': 'Sonarr has moved this series',
        'help': 'The series resolves by id, but Sonarr now reports a different folder. '
                'Accepting stores the new path.',
        'action': 'accept-path',
    },
    'monitored-outside-frame': {
        'severity': WARNING, 'blocking': False, 'scope': 'series',
        'title': 'Monitored episodes outside the keep window',
        'help': 'Sonarr may re-download what the next run deletes, which loops. Unmonitoring '
                'them stops it.',
        'action': 'unmonitor-out-frame',
    },
    'unmonitored-inside-frame': {
        'severity': WARNING, 'blocking': False, 'scope': 'series',
        'title': 'Unmonitored episodes inside the keep window',
        'help': 'Gaps inside the window will not fill while these are unmonitored.',
        'action': 'monitor-in-frame',
    },
    'unknown-files': {
        'severity': NOTICE, 'blocking': False, 'scope': 'series',
        'title': 'Files Sonarr does not know about',
        'help': 'These sit in the series folder but are not in Sonarr, so no rule will ever '
                'remove them.',
        'action': '',
    },
    'ended-expired': {
        'severity': NOTICE, 'blocking': False, 'scope': 'series',
        'title': 'Ended, and nothing is left inside the keep window',
        'help': 'This rule has nothing further to do. You can remove it, or remove the show.',
        'action': 'remove-rule',
    },
    'sonarr-unreachable': {
        'severity': ERROR, 'blocking': True, 'scope': 'system',
        'title': 'Sonarr is unreachable',
        'help': 'Scheduled runs are held until it answers, then released automatically.',
        'action': 'test-instance',
    },
    'mapping-broken': {
        'severity': ERROR, 'blocking': True, 'scope': 'system',
        'title': 'A root folder is not present on this server',
        'help': 'Every series under that root resolves to a path that does not exist. Fix the '
                'root mapping before running anything.',
        'action': 'open-instance',
    },
    'no-recycle-bin': {
        'severity': WARNING, 'blocking': False, 'scope': 'system',
        'title': 'Sonarr has no recycle bin',
        'help': 'Sonarr deletes files outright. Giving it a recycle bin makes every deletion '
                'recoverable for a while, including the ones this plugin asks for. It applies '
                'to everything Sonarr deletes, not only to TV Delete.',
        'action': 'enable-recycle-bin',
    },
    'run-aborted': {
        'severity': ERROR, 'blocking': False, 'scope': 'system',
        'title': 'A run was stopped by a guard',
        'help': 'The plan exceeded a safety limit and nothing was deleted.',
        'action': '',
    },
}


def make(kind: str, *, rule_id: str = '', instance_id: str = '', detail: str = '',
         count: int = 0, data=None) -> dict:
    """One alert. `kind` carries the severity and whether it blocks, so callers cannot
    disagree with each other about what the same condition means."""
    if kind not in KINDS:
        raise KeyError(f'Unknown alert kind {kind}')
    template = KINDS[kind]
    return {
        'key': f'{kind}:{rule_id or instance_id or "system"}',
        'kind': kind,
        'severity': template['severity'],
        'blocking': template['blocking'],
        'scope': template['scope'],
        'title': template['title'],
        'help': template['help'],
        'action': template['action'],
        'rule_id': rule_id,
        'instance_id': instance_id,
        'detail': detail,
        'count': count,
        'data': data or {},
    }


def merge(existing, current) -> list:
    """Carry first-seen dates across a re-check, and drop what has cleared.

    An alert that has been there since Tuesday should still say Tuesday after Wednesday's
    check, and one whose condition has gone should simply not be in the list any more.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    previous = {alert['key']: alert for alert in existing or []}
    merged = []
    for alert in current:
        was = previous.get(alert['key'])
        alert = dict(alert)
        alert['first_seen'] = was.get('first_seen', now) if was else now
        alert['last_seen'] = now
        merged.append(alert)
    return merged


def resolved(existing, current) -> list:
    """Alerts present before and gone now, so their clearing can be logged and notified."""
    live = {alert['key'] for alert in current}
    return [alert for alert in existing or [] if alert['key'] not in live]


def for_series(alerts, rule_id: str) -> list:
    return [alert for alert in alerts or [] if alert.get('rule_id') == rule_id]


def blocking(alerts) -> bool:
    return any(alert.get('blocking') for alert in alerts or [])


def summarise(alerts) -> dict:
    """Counts by severity, for the badges."""
    counts = {severity: 0 for severity in SEVERITIES}
    for alert in alerts or []:
        if alert.get('severity') in counts:
            counts[alert['severity']] += 1
    counts['total'] = sum(counts[severity] for severity in SEVERITIES)
    counts['blocking'] = sum(1 for alert in alerts or [] if alert.get('blocking'))
    return counts
