import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, effective_date, evaluate

NOW = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)


def settings(**overrides):
    document = {
        'retention': dict(DEFAULTS['retention']),
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in document:
            document[key].update(value)
        else:
            document[key] = value
    return document


def episode(season, number, days_ago=None, air_date=True, mtime_days=None):
    """An episode that aired days_ago, optionally with its air date withheld."""
    aired = NOW.date() - dt.timedelta(days=days_ago) if days_ago is not None else None
    mtime = (NOW - dt.timedelta(days=mtime_days if mtime_days is not None else (days_ago or 0))).timestamp()
    return {
        'episode_id': season * 1000 + number,
        'file_id': season * 1000 + number,
        'path': f'/mnt/user/media/TV/Show/Season {season:02d}/S{season:02d}E{number:02d}.mkv',
        'season': season, 'episode': number, 'title': f'E{number}',
        'air_date': aired.isoformat() if (aired and air_date) else None,
        'air_source': 'sonarr' if (aired and air_date) else '',
        'mtime': mtime, 'size': 1000,
    }


def paths(entries):
    return sorted(entry['path'] for entry in entries)


class Dates(unittest.TestCase):
    def test_sonarr_date_wins(self):
        date, source = effective_date(episode(1, 1, days_ago=10), True)
        self.assertEqual(source, 'sonarr')
        self.assertEqual(date, NOW.date() - dt.timedelta(days=10))

    def test_sonarrs_import_date_is_the_fallback(self):
        # Not the filesystem's modification time: Sonarr knows when it imported the file,
        # which a later copy or permission change cannot rewrite.
        entry = dict(episode(1, 1, days_ago=10, air_date=False), date_added='2026-08-01T10:00:00Z')
        date, source = effective_date(entry, True)
        self.assertEqual(source, 'imported')
        self.assertEqual(date, dt.date(2026, 8, 1))

    def test_fallback_can_be_switched_off(self):
        date, source = effective_date(episode(1, 1, days_ago=10, air_date=False), False)
        self.assertIsNone(date)
        self.assertEqual(source, 'unknown')


class KeepDays(unittest.TestCase):
    def test_only_older_episodes_are_selected(self):
        episodes = [episode(1, 1, days_ago=400), episode(1, 2, days_ago=100)]
        result = evaluate(episodes, {'keep_days': 180, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual(paths(result['delete']), [episodes[0]['path']])

    def test_undated_episode_survives_without_the_mtime_fallback(self):
        episodes = [episode(1, 1, days_ago=400, air_date=False)]
        document = settings(retention={'allow_estimated_dates': False})
        result = evaluate(episodes, {'keep_days': 180, 'combine': 'earliest'}, document, now=NOW)
        self.assertEqual(result['delete'], [])
        self.assertIn('No air date', result['keep'][0]['reason'])


class KeepEpisodes(unittest.TestCase):
    def test_newest_are_kept(self):
        episodes = [episode(1, index, days_ago=100 - index) for index in range(1, 6)]
        result = evaluate(episodes, {'keep_episodes': 2, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual(len(result['delete']), 3)
        self.assertEqual(paths(result['keep']), paths(episodes[-2:]))


class KeepSeasons(unittest.TestCase):
    def test_older_seasons_are_selected(self):
        episodes = [episode(season, 1, days_ago=1000 - season) for season in (1, 2, 3)]
        result = evaluate(episodes, {'keep_seasons': 1, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual(len(result['delete']), 2)
        self.assertEqual(result['keep'][0]['season'], 3)


class Combine(unittest.TestCase):
    def setUp(self):
        # Episode 1 is old but recent enough by episode count; episode 2 is new.
        self.episodes = [episode(1, 1, days_ago=400), episode(1, 2, days_ago=10)]
        self.rule = {'keep_days': 180, 'keep_episodes': 2}

    def test_earliest_keeps_the_most(self):
        result = evaluate(self.episodes, dict(self.rule, combine='earliest'), settings(), now=NOW)
        self.assertEqual(result['delete'], [])

    def test_any_deletes_on_a_single_vote(self):
        result = evaluate(self.episodes, dict(self.rule, combine='any'), settings(), now=NOW)
        self.assertEqual(paths(result['delete']), [self.episodes[0]['path']])

    def test_latest_needs_every_condition_to_agree(self):
        result = evaluate(self.episodes, dict(self.rule, combine='latest'), settings(), now=NOW)
        self.assertEqual(result['delete'], [])

    def test_latest_deletes_when_all_conditions_agree(self):
        episodes = [episode(1, 1, days_ago=400), episode(1, 2, days_ago=300), episode(1, 3, days_ago=5)]
        rule = {'keep_days': 180, 'keep_episodes': 1, 'combine': 'latest'}
        result = evaluate(episodes, rule, settings(), now=NOW)
        self.assertEqual(len(result['delete']), 2)

    def test_latest_never_acts_on_an_unknown(self):
        episodes = [episode(1, 1, days_ago=400, air_date=False), episode(1, 2, days_ago=10)]
        document = settings(retention={'allow_estimated_dates': False})
        rule = {'keep_days': 180, 'keep_episodes': 1, 'combine': 'latest'}
        self.assertEqual(evaluate(episodes, rule, document, now=NOW)['delete'], [])

    def test_any_ignores_an_unknown_and_uses_the_rest(self):
        episodes = [episode(1, 1, days_ago=400, air_date=False), episode(1, 2, days_ago=10)]
        document = settings(retention={'allow_estimated_dates': False})
        rule = {'keep_days': 180, 'keep_episodes': 1, 'combine': 'any'}
        result = evaluate(episodes, rule, document, now=NOW)
        self.assertEqual(paths(result['delete']), [episodes[0]['path']])


class Protection(unittest.TestCase):
    def test_specials_are_excluded_by_default(self):
        episodes = [episode(0, 1, days_ago=4000), episode(1, 1, days_ago=4000)]
        result = evaluate(episodes, {'keep_days': 30, 'combine': 'earliest'}, settings(), now=NOW)
        self.assertEqual(len(result['protected']), 1)
        self.assertEqual(paths(result['delete']), [episodes[1]['path']])

    def test_specials_can_be_included(self):
        episodes = [episode(0, 1, days_ago=4000)]
        document = settings(retention={'include_specials': True})
        result = evaluate(episodes, {'keep_days': 30, 'combine': 'earliest'}, document, now=NOW)
        self.assertEqual(len(result['delete']), 1)



class Reasons(unittest.TestCase):
    def test_every_decision_carries_a_reason(self):
        episodes = [episode(1, 1, days_ago=4000)]
        result = evaluate(episodes, {"keep_days": 30, "combine": "earliest"}, settings(), now=NOW)
        self.assertIn("older than 30 days", result["delete"][0]["reason"])


if __name__ == '__main__':
    unittest.main()


class InterpolatedDates(unittest.TestCase):
    """Estimating a missing air date from the episodes either side of it.

    Sonarr's import date moves when an episode is re-imported at better quality, so a 2015
    episode upgraded last week looks new. Its neighbours do not move that way.
    """

    def setUp(self):
        from core import interpolate_air_dates
        self.interpolate = interpolate_air_dates

    def series(self, dates):
        return [{'season': 3, 'episode': number, 'air_date': date, 'air_source': 'sonarr' if date else ''}
                for number, date in enumerate(dates, start=1)]

    def test_a_gap_is_filled_evenly(self):
        episodes = self.series(['2015-01-01', None, None, '2015-01-22'])
        self.assertEqual(self.interpolate(episodes), 2)
        self.assertEqual([e['air_date'] for e in episodes],
                         ['2015-01-01', '2015-01-08', '2015-01-15', '2015-01-22'])

    def test_an_estimate_says_that_it_is_one(self):
        episodes = self.series(['2015-01-01', None, '2015-01-15'])
        self.interpolate(episodes)
        self.assertEqual(episodes[1]['air_source'], 'estimated')

    def test_a_leading_gap_borrows_from_the_first_known_episode(self):
        episodes = self.series([None, None, '2015-03-01'])
        self.interpolate(episodes)
        self.assertEqual(episodes[0]['air_date'], '2015-03-01')

    def test_a_trailing_gap_borrows_from_the_last_known_episode(self):
        episodes = self.series(['2015-03-01', None])
        self.interpolate(episodes)
        self.assertEqual(episodes[1]['air_date'], '2015-03-01')

    def test_a_series_with_no_dates_at_all_is_left_alone(self):
        episodes = self.series([None, None])
        self.assertEqual(self.interpolate(episodes), 0)
        self.assertIsNone(episodes[0]['air_date'])

    def test_specials_are_not_used_as_neighbours(self):
        # A special sorts before episode one and would drag every estimate toward it.
        episodes = self.series(['2015-01-01', None, '2015-01-15'])
        episodes.append({'season': 0, 'episode': 1, 'air_date': '2001-01-01', 'air_source': 'sonarr'})
        self.interpolate(episodes)
        self.assertEqual(episodes[1]['air_date'], '2015-01-08')

    def test_an_estimate_beats_the_import_date_for_an_upgraded_episode(self):
        # The case that prompted this: a 2015 episode re-imported yesterday.
        from core import effective_date
        episode = {'season': 3, 'episode': 2, 'air_date': None,
                   'date_added': '2026-09-01T00:00:00Z'}
        episodes = [{'season': 3, 'episode': 1, 'air_date': '2015-01-01', 'air_source': 'sonarr'},
                    episode,
                    {'season': 3, 'episode': 3, 'air_date': '2015-01-15', 'air_source': 'sonarr'}]
        self.interpolate(episodes)
        date, source = effective_date(episode, True)
        self.assertEqual(date, dt.date(2015, 1, 8))
        self.assertEqual(source, 'estimated')
