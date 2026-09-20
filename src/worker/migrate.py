#!/usr/bin/env python3
"""Bring a stored settings document up to the current shape.

Settings outlive any release, so every change of shape needs a path forward for what is
already there. Migration runs on load, is idempotent, and never discards a value it does
not recognise: an unknown key is left in place rather than dropped, so downgrading and
re-upgrading cannot lose a setting.

There is a floor. Steps for versions 2 through 13 existed for documents written before
this application was ever released, and no such document survives: the only configurations
that ever reached disk were on one machine, and they are gone. Carrying twelve upgrade
paths that can never run is not caution, it is twelve untested branches in the code that
decides what a rule means. A document below `MINIMUM_VERSION` is refused by name instead,
which is a thing an operator can act on, rather than silently reshaped by code nobody has
exercised against a real file in months.

Raising the floor again is the same move: re-base the fixture, delete the step.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

SETTINGS_VERSION = 14

DEFAULT_TIMEZONE = 'Etc/UTC'

# The oldest document shape this release can still read. See the module docstring before
# lowering it — and re-base tests/fixtures/settings-v13-live.json before raising it.
MINIMUM_VERSION = 13


class UnsupportedVersion(Exception):
    """A stored document is older than this release knows how to upgrade."""


def migrate(raw: dict) -> dict:
    """Upgrade a settings document in place-safe fashion, returning the new one."""
    if not isinstance(raw, dict) or not raw:
        return {'settings_version': SETTINGS_VERSION}
    document = dict(raw)
    version = int(document.get('settings_version') or 0)

    if version and version < MINIMUM_VERSION:
        raise UnsupportedVersion(
            f'Settings are version {version}; this release reads version '
            f'{MINIMUM_VERSION} or newer. Restore a recent backup, or start from '
            f'fresh settings.')

    if version >= SETTINGS_VERSION:
        # A hand-edited or partially migrated current document may still carry the old
        # outbound-notification matrix.  Scrub it even when no version step remains, while
        # preserving unrelated forward-version keys for a later release to interpret.
        if version == SETTINGS_VERSION:
            document.pop('notifications', None)
            schedule = dict(document.get('schedule') or {})
            schedule.setdefault('timezone', 'Etc/UTC')
            document['schedule'] = schedule
        return document

    if version < 14:
        document.update(_to_v14(document))
    document['settings_version'] = SETTINGS_VERSION
    return document


def _to_v14(document: dict) -> dict:
    """Pin the zone the schedule has been running in, rather than assuming UTC.

    Before this version there was no timezone field, and the worker scheduled against
    container-local time — `datetime.now().astimezone()`, with `zone_for` falling back
    to whatever that carried. Writing `Etc/UTC` here would keep the stored hour and
    silently change the instant it means: a 01:00 run on a US Eastern container becomes
    21:00 the previous day. A migration is the one place that must not do that.

    The container's own `TZ` is the name of the zone it has been running in, so an
    upgraded schedule keeps firing when it always did. A fresh install never reaches
    this step and still starts at `Etc/UTC` through validation.
    """
    schedule = dict(document.get('schedule') or {})
    schedule.setdefault('timezone', _container_timezone())
    return {'schedule': schedule}


def _container_timezone() -> str:
    """The container's IANA zone name, or UTC when it is unset or unusable.

    A `TZ` the zone database cannot resolve must not be written into settings: it would
    migrate the document into a state validation then refuses.
    """
    name = (os.environ.get('TZ') or '').strip()
    if not name or name in ('UTC', 'GMT', 'Etc/UTC'):
        return DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except Exception:
        return DEFAULT_TIMEZONE
    return name
