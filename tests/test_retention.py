import datetime as dt
import unittest

import context  # noqa: F401
from core import (DEFAULTS, effective_date, evaluate, excluded_causes, excluded_episodes,
                  exclusion_summary)

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
        result = evaluate(episodes, {'keep_days': 180, 'combine': 'any'}, settings(), now=NOW)
        self.assertEqual(paths(result['delete']), [episodes[0]['path']])

    def test_undated_episode_survives_without_the_mtime_fallback(self):
        episodes = [episode(1, 1, days_ago=400, air_date=False)]
        document = settings(retention={'allow_estimated_dates': False})
        result = evaluate(episodes, {'keep_days': 180, 'combine': 'any'}, document, now=NOW)
        self.assertEqual(result['delete'], [])
        self.assertIn('No air date', result['keep'][0]['reason'])


class KeepEpisodes(unittest.TestCase):
    def test_newest_are_kept(self):
        episodes = [episode(1, index, days_ago=100 - index) for index in range(1, 6)]
        result = evaluate(episodes, {'keep_episodes': 2, 'combine': 'any'}, settings(), now=NOW)
        self.assertEqual(len(result['delete']), 3)
        self.assertEqual(paths(result['keep']), paths(episodes[-2:]))


class KeepSeasons(unittest.TestCase):
    def test_older_seasons_are_selected(self):
        episodes = [episode(season, 1, days_ago=1000 - season) for season in (1, 2, 3)]
        result = evaluate(episodes, {'keep_seasons': 1, 'combine': 'any'}, settings(), now=NOW)
        self.assertEqual(len(result['delete']), 2)
        self.assertEqual(result['keep'][0]['season'], 3)


class Combine(unittest.TestCase):
    def setUp(self):
        # Episode 1 is old but recent enough by episode count; episode 2 is new.
        self.episodes = [episode(1, 1, days_ago=400), episode(1, 2, days_ago=10)]
        self.rule = {'keep_days': 180, 'keep_episodes': 2}

    def test_any_keeps_when_one_condition_matches(self):
        result = evaluate(self.episodes, dict(self.rule, combine='any'), settings(), now=NOW)
        self.assertEqual(result['delete'], [])

    def test_all_deletes_on_a_single_delete_vote(self):
        result = evaluate(self.episodes, dict(self.rule, combine='all'), settings(), now=NOW)
        self.assertEqual(paths(result['delete']), [self.episodes[0]['path']])

    def test_any_deletes_when_all_conditions_agree(self):
        episodes = [episode(1, 1, days_ago=400), episode(1, 2, days_ago=300), episode(1, 3, days_ago=5)]
        rule = {'keep_days': 180, 'keep_episodes': 1, 'combine': 'any'}
        result = evaluate(episodes, rule, settings(), now=NOW)
        self.assertEqual(len(result['delete']), 2)

    def test_all_never_acts_on_an_unknown(self):
        episodes = [episode(1, 1, days_ago=400, air_date=False), episode(1, 2, days_ago=10)]
        document = settings(retention={'allow_estimated_dates': False})
        rule = {'keep_days': 180, 'keep_episodes': 1, 'combine': 'all'}
        self.assertEqual(evaluate(episodes, rule, document, now=NOW)['delete'], [])

    def test_any_also_protects_an_unknown(self):
        episodes = [episode(1, 1, days_ago=400, air_date=False), episode(1, 2, days_ago=10)]
        document = settings(retention={'allow_estimated_dates': False})
        rule = {'keep_days': 180, 'keep_episodes': 1, 'combine': 'any'}
        result = evaluate(episodes, rule, document, now=NOW)
        self.assertEqual(result['delete'], [])


class Protection(unittest.TestCase):
    def test_specials_are_excluded_by_default(self):
        episodes = [episode(0, 1, days_ago=4000), episode(1, 1, days_ago=4000)]
        result = evaluate(episodes, {'keep_days': 30, 'combine': 'any'}, settings(), now=NOW)
        self.assertEqual(len(result['protected']), 1)
        self.assertEqual(paths(result['delete']), [episodes[1]['path']])

    def test_specials_can_be_included(self):
        episodes = [episode(0, 1, days_ago=4000)]
        document = settings(automation={'exclude_specials': False})
        result = evaluate(episodes, {'keep_days': 30, 'combine': 'any'}, document, now=NOW)
        self.assertEqual(len(result['delete']), 1)



class Reasons(unittest.TestCase):
    def test_every_decision_carries_a_reason(self):
        episodes = [episode(1, 1, days_ago=4000)]
        result = evaluate(episodes, {"keep_days": 30, "combine": "any"}, settings(), now=NOW)
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

    def test_a_leading_gap_stays_unresolved(self):
        episodes = self.series([None, None, '2015-03-01'])
        self.assertEqual(self.interpolate(episodes), 0)
        self.assertIsNone(episodes[0]['air_date'])
        self.assertIsNone(episodes[1]['air_date'])

    def test_a_trailing_gap_stays_unresolved(self):
        episodes = self.series(['2015-03-01', None])
        self.assertEqual(self.interpolate(episodes), 0)
        self.assertIsNone(episodes[1]['air_date'])

    def test_a_trailing_gap_is_left_unresolved_for_a_future_episode(self):
        episodes = self.series(['2015-03-01', None, None])
        self.assertEqual(self.interpolate(episodes), 0)
        self.assertIsNone(episodes[1]['air_date'])
        self.assertIsNone(episodes[2]['air_date'])

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


class Exclusions(unittest.TestCase):
    """The one answer that is never weighed against anything else."""

    def episodes(self):
        return [
            {'episode_id': 1, 'season': 1, 'episode': 1, 'title': 'Lost pilot',
             'path': '/tv/Show/Season 01/s01e01.mkv', 'has_file': True, 'size': 10,
             'air_date': '2010-01-01', 'air_source': 'sonarr', 'monitored': True},
            {'episode_id': 2, 'season': 1, 'episode': 2, 'title': 'Ordinary',
             'path': '/tv/Show/Season 01/s01e02.mkv', 'has_file': True, 'size': 10,
             'air_date': '2010-01-08', 'air_source': 'sonarr', 'monitored': True},
            {'episode_id': 3, 'season': 2, 'episode': 1, 'title': 'Anniversary show',
             'path': '/tv/Show/Specials Extras/s02e01.mkv', 'has_file': True, 'size': 10,
             'air_date': '2011-01-01', 'air_source': 'sonarr', 'monitored': True},
        ]

    def deleted(self, rule=None, automation=None):
        conf = settings(automation=automation or {"exclude_seasons": [], "exclude_episodes": []})
        rule = dict({'keep_episodes': 1, 'combine': 'any'}, **(rule or {}))
        result = evaluate(self.episodes(), rule, conf, now=NOW)
        return sorted(item['episode_id'] for item in result['delete'])

    def test_without_exclusions_the_rule_decides(self):
        self.assertEqual(self.deleted(), [1, 2])

    def test_a_hand_picked_episode_is_never_deleted(self):
        self.assertEqual(self.deleted(rule={'exclusions': [{'season': 1, 'episode': 1}]}), [2])

    def test_an_entry_with_no_episode_number_excludes_the_season(self):
        self.assertEqual(self.deleted(rule={'exclusions': [{'season': 1, 'episode': None}]}), [])

    def test_a_whole_season_can_be_excluded_automatically(self):
        self.assertEqual(self.deleted(automation={"exclude_seasons": [1], "exclude_episodes": []}), [])

    def test_a_pattern_matches_the_title(self):
        found = self.deleted(automation={"exclude_seasons": [], "exclude_episodes": ['lost pilot']})
        self.assertEqual(found, [2])

    def test_a_pattern_matches_the_path_so_it_catches_a_folder(self):
        """Sonarr has no season names, so a folder is only reachable through the path.

        Its season object carries a number, a monitored flag and statistics — nothing that
        could be matched by name.
        """
        # And excluding one changes what the window holds: with S02E01 set aside, "keep 1"
        # keeps the newest of what is left rather than the newest overall.
        found = self.deleted(automation={"exclude_seasons": [], "exclude_episodes": ['Specials Extras']})
        self.assertEqual(found, [1])

    def test_the_reason_travels_with_the_answer(self):
        # So the preview and the journal can say why something was skipped, without anyone
        # going to read the settings to find out.
        conf = settings(automation={"exclude_seasons": [2], "exclude_episodes": []})
        rule = {'keep_episodes': 1, 'combine': 'any',
                'exclusions': [{'season': 1, 'episode': 1}]}
        result = evaluate(self.episodes(), rule, conf, now=NOW)
        reasons = {item['episode_id']: item['reason'] for item in result['protected']}
        self.assertIn('by hand', reasons[1])
        self.assertIn('whole season', reasons[3])

    def test_an_excluded_episode_is_neither_monitored_nor_unmonitored(self):
        """Set aside where specials are, and for the same reason.

        Outside the frame, and so outside everything that acts on the frame — the list
        would be worth little if a run kept switching Sonarr's flags on the things on it.
        """
        from core import classify_monitoring
        conf = settings(automation={"exclude_seasons": [], "exclude_episodes": []})
        rule = {'keep_episodes': 1, 'combine': 'any',
                'exclusions': [{'season': 1, 'episode': 1}]}
        state = classify_monitoring(self.episodes(), rule, conf, now=NOW)
        touched = {row['episode_id'] for row in state['out_frame_monitored']}
        self.assertNotIn(1, touched, 'an excluded episode must never be unmonitored either')


class ExclusionSummary(unittest.TestCase):
    """What the series pane reads out, and the rule that it cannot disagree with a run.

    Two functions answering "is this excluded" and "what excluded it" separately is how a
    pane ends up naming a pattern that caught nothing, or missing one that did. They share
    a pass, and these tests are what says so.
    """

    def episodes(self):
        return [
            {'episode_id': 1, 'season': 0, 'episode': 1, 'title': 'Christmas special',
             'path': '/tv/Show/Specials/s00e01.mkv', 'has_file': True},
            {'episode_id': 2, 'season': 1, 'episode': 1, 'title': 'Pilot',
             'path': '/tv/Show/Season 01/s01e01.mkv', 'has_file': True},
            {'episode_id': 3, 'season': 1, 'episode': 2, 'title': 'Behind the scenes',
             'path': '/tv/Show/Season 01/s01e02.mkv', 'has_file': True},
            {'episode_id': 4, 'season': 2, 'episode': 1, 'title': 'Ordinary',
             'path': '/tv/Show/Season 02/s02e01.mkv', 'has_file': True},
        ]

    def summarise(self, rule=None, **automation):
        conf = settings(automation=dict({"exclude_seasons": [], "exclude_episodes": []}, **automation))
        return exclusion_summary(self.episodes(), rule or {}, conf)

    def test_the_two_readings_name_the_same_episodes(self):
        conf = settings(automation={"exclude_seasons": [0], "exclude_episodes": ["behind the scenes"]})
        rule = {'exclusions': [{'season': 2, 'episode': 1}]}
        causes = excluded_causes(self.episodes(), rule, conf)
        self.assertEqual(set(causes), set(excluded_episodes(self.episodes(), rule, conf)))
        self.assertEqual({key: reason for key, (reason, _) in causes.items()},
                         excluded_episodes(self.episodes(), rule, conf))

    def test_each_cause_is_counted_against_the_thing_that_caused_it(self):
        found = self.summarise(rule={'exclusions': [{'season': 2, 'episode': 1}]},
                               exclude_seasons=[0], exclude_episodes=['behind the scenes'])
        self.assertEqual(found['seasons'], [{'season': 0, 'episodes': 1}])
        self.assertEqual(found['episode_patterns'], [{'pattern': 'behind the scenes', 'episodes': 1}])
        self.assertEqual(found['manual'], 1)
        self.assertEqual(found['total'], 3)

    def test_a_pattern_that_catches_nothing_here_is_not_listed(self):
        # True of the settings, not of this series. Listing it would answer a question
        # nobody standing in front of this series asked.
        found = self.summarise(exclude_episodes=['behind the scenes', 'director commentary'])
        self.assertEqual([entry['pattern'] for entry in found['episode_patterns']], ['behind the scenes'])

    def test_patterns_are_listed_in_the_order_they_were_typed(self):
        # The box they came from shows them that way. Sorted by count, the pane and the
        # setting disagree on sight.
        found = self.summarise(exclude_episodes=['behind the scenes', 'christmas'])
        self.assertEqual([entry['pattern'] for entry in found['episode_patterns']],
                         ['behind the scenes', 'christmas'])

    def test_the_phrase_is_named_back_as_it_was_typed(self):
        # Matching is case-blind; reporting is not. Lower-casing somebody's phrase in the
        # interface is a small lie about what they wrote.
        found = self.summarise(exclude_episodes=['Behind The Scenes'])
        self.assertEqual(found['episode_patterns'][0]['pattern'], 'Behind The Scenes')

    def test_a_hand_picked_episode_outranks_a_pattern_that_also_caught_it(self):
        # Manual wins in `excluded_causes`, so it must not be counted twice.
        found = self.summarise(rule={'exclusions': [{'season': 1, 'episode': 2}]},
                               exclude_episodes=['behind the scenes'])
        self.assertEqual(found['manual'], 1)
        self.assertEqual(found['episode_patterns'], [])
        self.assertEqual(found['total'], 1)

    def test_a_series_with_nothing_excluded_says_so_with_zeroes(self):
        found = self.summarise()
        self.assertEqual((found['total'], found['manual'], found['seasons'], found['episode_patterns']),
                         (0, 0, [], []))


class SpecialsAreAnExclusion(unittest.TestCase):
    """One gate, and specials go through it like everything else.

    They used to be decided twice: once on the exclusion list, and again by a `season == 0`
    branch a few lines further down in both `evaluate` and `keep_frame`. Two gates
    answering the same kind of question meant "what will this run skip" had two answers,
    and the second was invisible — nothing on screen ever said fifteen episodes had been
    set aside.
    """

    def episodes(self):
        return [
            # Filed as season 0, which is what Sonarr does by default.
            {'episode_id': 1, 'season': 0, 'episode': 1, 'title': 'Christmas',
             'path': '/tv/Show/Season 00/s00e01.mkv', 'has_file': True, 'size': 10,
             'air_date': '2010-12-25', 'air_source': 'sonarr', 'monitored': True},
            # Filed into a folder instead, which is the other thing Sonarr can be told to
            # do — and the reason the season number alone is not enough.
            {'episode_id': 2, 'season': 3, 'episode': 99, 'title': 'Making of',
             'path': '/tv/Show/Specials/s03e99.mkv', 'has_file': True, 'size': 10,
             'air_date': '2011-01-01', 'air_source': 'sonarr', 'monitored': True},
            {'episode_id': 3, 'season': 1, 'episode': 1, 'title': 'Pilot',
             'path': '/tv/Show/Season 01/s01e01.mkv', 'has_file': True, 'size': 10,
             'air_date': '2010-01-01', 'air_source': 'sonarr', 'monitored': True},
        ]

    def deleted(self, rule=None, **automation):
        conf = settings(automation=dict({'exclude_specials': True}, **automation))
        rule = dict({'keep_days': 1, 'combine': 'any'}, **(rule or {}))
        return sorted(item['episode_id']
                      for item in evaluate(self.episodes(), rule, conf, now=NOW)['delete'])

    def test_season_zero_is_excluded(self):
        self.assertNotIn(1, self.deleted())

    def test_a_specials_folder_is_excluded_even_in_a_numbered_season(self):
        # Sonarr can be told to file specials into a folder rather than as season 0, and
        # which one you get depends on a naming setting nobody remembers choosing.
        self.assertNotIn(2, self.deleted())

    def test_an_ordinary_episode_is_untouched_by_any_of_it(self):
        self.assertEqual(self.deleted(), [3])

    def test_turning_it_off_lets_specials_be_deleted(self):
        self.assertEqual(self.deleted(exclude_specials=False), [1, 2, 3])

    def test_a_series_may_opt_out_of_the_safety(self):
        # Which is exactly what its own include_specials has always meant.
        self.assertEqual(self.deleted(rule={'include_specials': True}), [1, 2, 3])

    def test_a_series_may_opt_in_where_the_setting_is_off(self):
        self.assertEqual(self.deleted(rule={'include_specials': False}, exclude_specials=False), [3])

    def test_the_reason_reaches_the_preview(self):
        conf = settings(automation={'exclude_specials': True})
        result = evaluate(self.episodes(), {'keep_days': 1, 'combine': 'any'}, conf, now=NOW)
        reasons = {row['episode_id']: row['reason'] for row in result['protected']}
        self.assertEqual(reasons[1], 'Specials are excluded automatically')
        self.assertEqual(reasons[2], 'Specials are excluded automatically')

    def test_a_folder_named_after_a_word_that_contains_specials_is_not_one(self):
        # Whole folder name, not a substring of the path: matching the lot would let this
        # catch a library root that happens to say Extras.
        episodes = [dict(self.episodes()[2], path='/tv/Extras Archive/Show/Season 01/s01e01.mkv')]
        conf = settings(automation={'exclude_specials': True})
        result = evaluate(episodes, {'keep_days': 1, 'combine': 'any'}, conf, now=NOW)
        self.assertEqual(len(result['delete']), 1)
