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
import hashlib

ERROR = 'error'
WARNING = 'warning'
NOTICE = 'notice'
SEVERITIES = [ERROR, WARNING, NOTICE]

# What each kind of alert means, whether it stops the series from being processed, and the
# action offered against it. Blocking is a property of the kind, not a judgement made at
# the call site, so the same condition always has the same consequence.
#
KINDS = {
    'unmatched': {
        'severity': ERROR, 'blocking': True, 'scope': 'series',
        'title': 'Not matched to a Sonarr series',
        'help': 'This rule no longer resolves to exactly one series. It is skipped by every '
                'run until it does.',
        'action': '',
    },
    'air-date-unresolved': {
        'severity': ERROR, 'blocking': True, 'scope': 'series',
        'title': 'Air dates unresolved',
        'help': 'This keep-by-age rule is paused because one or more judged files have no '
                'date from Sonarr or the configured providers.',
        'action': '',
    },
    'ended': {
        'severity': NOTICE, 'blocking': False, 'scope': 'series',
        'title': 'Series has ended',
        'help': 'No further episodes are coming, so what is kept here will only shrink. '
                'This rule will be switched off automatically once nothing is left inside '
                'its keep window. Worth deciding what you want to keep while there is '
                'still something to decide about.',
        # Sonarr's own "this series ended" notification already goes out once, the first
        # time it says so. This is the standing fact rather than the news of it.
        'action': '',
    },
    'ended-expired': {
        # A notice, not a warning: nothing is wrong. The series finished, its window
        # emptied, and this application switched the rule off — which is what it should
        # do. Counting an expected event as a problem is how people learn to ignore the
        # header.
        'severity': NOTICE, 'blocking': False, 'scope': 'series',
        'title': 'Ended, and nothing is left inside the keep window',
        'help': 'This rule has been switched off: there is nothing left for it to act on, '
                'and no further episodes are coming. You can remove it, or remove the show.',
        # The series ending is worth telling someone about, and it already is, once, when
        # Sonarr first reports it. Announcing this as well would say it twice.
        'action': '',
        # The rule this is about is the rule this switched off, and `managed_only` drops
        # alerts belonging to a disabled rule. An alert about a *state* is fairly
        # suppressed when nobody is managing the series; one recording an action this
        # application took is not, or it would silence the only notice of its own doing.
        'survives_disable': True,
    },
    'sonarr-unreachable': {
        'severity': ERROR, 'blocking': True, 'scope': 'system',
        'title': 'Sonarr is unreachable',
        'help': 'Scheduled runs are held until it answers, then released automatically.',
        'action': 'test-instance',
    },
    'no-recycle-bin': {
        'severity': WARNING, 'blocking': False, 'scope': 'system',
        'title': 'Sonarr has no recycle bin',
        'help': 'Sonarr deletes files outright. Giving it a recycle bin makes every deletion '
                'recoverable for a while, including the ones this plugin asks for. It applies '
                'to everything Sonarr deletes, not only to TV Retention.',
        'action': 'enable-recycle-bin',
    },
    'state-unavailable': {
        'severity': ERROR, 'blocking': True, 'scope': 'system',
        'title': 'Application storage unavailable',
        'help': 'The config/state directory cannot be read or written. Retention is paused '
                'until the volume is available again.',
        'action': '',
    },
    'backup-unavailable': {
        'severity': WARNING, 'blocking': False, 'scope': 'system',
        'title': 'Backup destination unavailable',
        'help': 'Configure a writable backup destination under General → Backup.',
        'action': '',
    },
    'backup-failed': {
        'severity': ERROR, 'blocking': False, 'scope': 'system',
        'title': 'Last backup failed',
        'help': 'Open General → Backup and run a backup after correcting the destination.',
        'action': '',
    },
    'sync-stale': {
        'severity': WARNING, 'blocking': False, 'scope': 'system',
        'title': 'Sonarr sync is stale',
        'help': 'Refresh Sonarr before relying on the scheduled-change plan.',
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


def notifies(alert) -> bool:
    """Legacy compatibility hook; outbound notifications no longer exist."""
    return False


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
    """Counts by severity, for the badges. Never counts a series nobody is managing."""
    counts = {severity: 0 for severity in SEVERITIES}
    for alert in (a for a in alerts or [] if not a.get('unmanaged')):
        if alert.get('severity') in counts:
            counts[alert['severity']] += 1
    counts['total'] = sum(counts[severity] for severity in SEVERITIES)
    counts['blocking'] = sum(1 for alert in alerts or []
                             if alert.get('blocking') and not alert.get('unmanaged'))
    return counts


def fingerprint(alert) -> str:
    """What an acknowledgement is against.

    An alert is a fact about the present, so acknowledging one cannot mean "never tell me
    again" — that would hide a live problem indefinitely. It means "I have seen this, as it
    is". Change the detail, the count, or the data behind it and the acknowledgement no
    longer applies, because it is no longer the same fact.
    """
    material = f"{alert.get('kind')}|{alert.get('rule_id')}|{alert.get('instance_id')}" \
               f"|{alert.get('detail')}|{alert.get('count')}"
    return hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]


def survives_disable(alert) -> bool:
    """Whether this alert still counts once its series is switched off.

    True only for kinds that record something this application *did*. A state nobody is
    managing is fairly suppressed; an action taken without being asked for is not, and
    auto-disable would otherwise silence the only notice that it happened.
    """
    return bool(KINDS.get(alert.get('kind'), {}).get('survives_disable'))


def managed_only(alerts, settings: dict) -> list:
    """Alerts about series a run would actually touch.

    A rule that is switched off is not being managed, so nothing about it is a problem to
    report: it raises none while it is off, and every one it had comes back the moment it
    is switched on. Nothing is deleted — the facts stay in the health cache and go on
    being re-decided; they are simply not anybody's problem while no run will act on them.

    Not the same as muting, which is a decision about a *kind* of alert across every
    series, and which leaves a blocking alert blocking. This is a decision about one
    series, and a series that is off is not blocked from a run it is not part of.
    """
    off = {rule.get('id') for rule in settings.get('rules') or [] if not rule.get('enabled')}
    if not off:
        return list(alerts or [])
    return [alert for alert in alerts or []
            if alert.get('rule_id') not in off or survives_disable(alert)]


def annotate(alerts, settings: dict, acknowledged: dict, suppressed=None) -> list:
    """Mark acknowledgements and unmanaged series, omitting narrowly suppressed facts.

    An alert against a switched-off series is *marked* rather than dropped. It still counts
    for nothing — not in a contextual badge or notification, which is what "a series that
    is off raises nothing" was always about. But the interface can now offer to show them
    on request, and dropping them here left it with nothing to offer.
    """
    hidden = set((suppressed or {}).keys())
    off = {rule.get('id') for rule in settings.get('rules') or [] if not rule.get('enabled')}
    shown = []
    for alert in alerts or []:
        if alert.get('key') in hidden:
            continue
        seen = (acknowledged or {}).get(alert['key'])
        shown.append(dict(alert, acknowledged=bool(seen and seen == fingerprint(alert)),
                          unmanaged=alert.get('rule_id') in off and not survives_disable(alert)))
    return shown


def suppressed(alerts, suppressed_keys) -> list:
    """Active recurring alerts hidden by exact key, available for restoration."""
    hidden = set((suppressed_keys or {}).keys())
    return [dict(alert, suppressed=True) for alert in alerts or [] if alert.get('key') in hidden]


def may_acknowledge(alert) -> bool:
    """Every non-blocking warning and notice may be acknowledged."""
    return alert.get('severity') != ERROR and not alert.get('blocking')
