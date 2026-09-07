import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, classify_monitoring, evaluate

NOW = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)
S = {'retention': dict(DEFAULTS['retention']),
     'guards': {'max_percent_per_rule': 100, 'min_file_age_hours': 0}}

def ep(season, number, days_ago, has_file=True, monitored=True):
    aired = (NOW - dt.timedelta(days=days_ago)).date()
    return {'episode_id': season * 100 + number, 'season': season, 'episode': number,
            'title': f'S{season}E{number}', 'has_file': has_file, 'monitored': monitored,
            'path': f'/tv/Show/S{season:02d}E{number:02d}.mkv' if has_file else f'sonarr:episode:{season}{number}',
            'air_date': aired.isoformat(), 'air_source': 'sonarr',
            'mtime': (NOW - dt.timedelta(days=days_ago)).timestamp(), 'size': 10}

def future(season, number, days_ahead):
    e = ep(season, number, 0, has_file=False)
    e['air_date'] = (NOW.date() + dt.timedelta(days=days_ahead)).isoformat()
    return e

class UnairedSeasons(unittest.TestCase):
    def test_deletion_ranks_only_seasons_that_have_files(self):
        # Season 4 announced but unaired: it has no files, so it must not consume a
        # "keep 2 seasons" slot and push season 2 out.
        episodes = [ep(1, 1, 900), ep(2, 1, 600), ep(3, 1, 300)]
        result = evaluate(episodes, {'keep_seasons': 2, 'combine': 'earliest'}, S, now=NOW)
        kept = sorted({e['season'] for e in result['keep']})
        self.assertEqual(kept, [2, 3])

    def test_monitoring_ignores_a_season_with_nothing_aired(self):
        episodes = [ep(1, 1, 900), ep(2, 1, 600), ep(3, 1, 300), future(4, 1, 30), future(4, 2, 37)]
        state = classify_monitoring(episodes, {'keep_seasons': 2, 'combine': 'earliest'}, S, now=NOW)
        # Season 4 is unaired: inside the frame, and never counted as one of the newest two.
        self.assertEqual(state['unaired'], 2)
        out = sorted({row['season'] for row in state['out_frame_monitored']})
        self.assertEqual(out, [1], 'only season 1 should fall outside a two-season frame')

    def test_a_part_aired_season_still_counts(self):
        episodes = [ep(1, 1, 900), ep(2, 1, 600), ep(3, 1, 5), future(3, 2, 9)]
        state = classify_monitoring(episodes, {'keep_seasons': 2, 'combine': 'earliest'}, S, now=NOW)
        out = sorted({row['season'] for row in state['out_frame_monitored']})
        self.assertEqual(out, [1])

    def test_keep_days_never_selects_an_unaired_episode(self):
        episodes = [future(9, 1, 60)]
        result = evaluate(episodes, {'keep_days': 30, 'combine': 'earliest'}, S, now=NOW)
        self.assertEqual(result['delete'], [], 'a future air date is inside any keep window')

if __name__ == '__main__':
    unittest.main()
