import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, select_remonitor

NOW = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)


def settings():
    return {'retention': dict(DEFAULTS['retention']),
            'guards': {'max_deletes_per_run': 1000, 'max_percent_per_rule': 50, 'min_file_age_hours': 0}}


def episode(number, days_ago):
    aired = (NOW - dt.timedelta(days=days_ago)).date()
    return {'episode_id': number, 'season': 1, 'episode': number, 'title': f'E{number}',
            'path': f'/mnt/user/media/TV/Show/Season 01/S01E{number:02d}.mkv',
            'air_date': aired.isoformat(), 'air_source': 'sonarr',
            'mtime': (NOW - dt.timedelta(days=days_ago)).timestamp(), 'size': 100}


def ledger(number, days_ago):
    entry = episode(number, days_ago)
    entry['unmonitored_at'] = '2026-01-01T00:00:00+00:00'
    return entry


class Remonitor(unittest.TestCase):
    def test_widening_the_window_puts_an_episode_back(self):
        # Deleted under a 30-day rule; the rule now keeps 180 days.
        chosen = select_remonitor([episode(9, 2)], [ledger(1, 100)], {'keep_days': 180, 'combine': 'earliest'},
                                  settings(), now=NOW)
        self.assertEqual([entry['episode_id'] for entry in chosen], [1])

    def test_an_episode_still_outside_the_window_stays_gone(self):
        chosen = select_remonitor([episode(9, 2)], [ledger(1, 400)], {'keep_days': 180, 'combine': 'earliest'},
                                  settings(), now=NOW)
        self.assertEqual(chosen, [])

    def test_nothing_happens_without_a_ledger(self):
        self.assertEqual(select_remonitor([episode(9, 2)], [], {'keep_days': 180, 'combine': 'earliest'},
                                          settings(), now=NOW), [])

    def test_episode_counts_include_the_missing_ones(self):
        # Keeping the newest 3 of five episodes: the two on disk plus the newest ledger entry.
        present = [episode(5, 5), episode(4, 10)]
        gone = [ledger(3, 20), ledger(2, 30), ledger(1, 40)]
        chosen = select_remonitor(present, gone, {'keep_episodes': 3, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual([entry['episode_id'] for entry in chosen], [3])

    def test_the_per_rule_guard_does_not_suppress_re_monitoring(self):
        # A guard exists to stop mass deletion; it must not stop a read-only comparison.
        present = [episode(9, 1)]
        gone = [ledger(index, 10 + index) for index in range(1, 9)]
        chosen = select_remonitor(present, gone, {'keep_days': 180, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual(len(chosen), 8)

    def test_seasons_are_honoured_too(self):
        present = [dict(episode(1, 5), season=3)]
        gone = [dict(ledger(2, 500), season=2), dict(ledger(3, 900), season=1)]
        chosen = select_remonitor(present, gone, {'keep_seasons': 2, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual([entry['season'] for entry in chosen], [2])




if __name__ == '__main__':
    unittest.main()
