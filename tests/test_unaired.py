import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, classify_monitoring, evaluate, next_episode

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


class NextEpisode(unittest.TestCase):
    """What the panel names as coming up, from episodes it already holds."""

    def test_the_next_one_due_is_the_earliest_that_has_not_aired(self):
        episodes = [ep(1, 1, 900), future(4, 2, 37), future(4, 1, 30)]
        found = next_episode(episodes, now=NOW)
        self.assertEqual((found['season'], found['episode']), (4, 1))
        self.assertEqual(found['air_date'], (NOW.date() + dt.timedelta(days=30)).isoformat())
        self.assertFalse(found['estimated'])

    def test_one_that_aired_this_morning_and_is_on_disk_is_not_next(self):
        # "Next" means the one still to come, not the most recent thing to arrive.
        episodes = [ep(1, 1, 0), future(1, 2, 7)]
        self.assertEqual(next_episode(episodes, now=NOW)['episode'], 2)

    def test_an_episode_due_today_is_still_next(self):
        episodes = [ep(1, 1, 30), ep(1, 2, 0, has_file=False)]
        self.assertEqual(next_episode(episodes, now=NOW)['episode'], 2)

    def test_a_finished_series_has_nothing_next(self):
        self.assertIsNone(next_episode([ep(1, 1, 900), ep(1, 2, 890)], now=NOW))

    def test_a_guessed_date_says_so(self):
        # An interpolated or TMDB date is not one Sonarr gave us, and the panel must not
        # print it with the same confidence.
        episode = future(2, 1, 14)
        episode['air_source'] = 'estimated'
        self.assertTrue(next_episode([episode], now=NOW)['estimated'])
