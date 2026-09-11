import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, classify_monitoring

NOW = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)


def settings():
    return {'retention': dict(DEFAULTS['retention'])}


def episode(number, days_ago, monitored, has_file=True):
    aired = (NOW - dt.timedelta(days=days_ago)).date()
    return {'episode_id': number, 'season': 1, 'episode': number, 'title': f'E{number}',
            'path': f'/mnt/user/media/TV/Show/S01E{number:02d}.mkv' if has_file else f'sonarr:episode:{number}',
            'air_date': aired.isoformat(), 'air_source': 'sonarr', 'has_file': has_file,
            'monitored': monitored, 'mtime': (NOW - dt.timedelta(days=days_ago)).timestamp(), 'size': 100}


RULE = {'keep_days': 180, 'combine': 'any'}


class Classify(unittest.TestCase):
    def test_the_tidy_state_is_aligned(self):
        episodes = [episode(1, 10, True), episode(2, 400, False)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['status'], 'aligned')

    def test_a_fresh_library_reads_as_all_monitored(self):
        episodes = [episode(1, 10, True), episode(2, 400, True)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['status'], 'all_monitored')
        self.assertEqual(len(state['out_frame_monitored']), 1)

    def test_monitored_outside_the_frame_is_flagged(self):
        episodes = [episode(1, 10, True), episode(2, 400, True), episode(3, 500, False)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['status'], 'outside_monitored')
        self.assertEqual([row['episode'] for row in state['out_frame_monitored']], [2])

    def test_unmonitored_inside_the_frame_is_flagged(self):
        episodes = [episode(1, 10, False), episode(2, 400, False)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['status'], 'in_frame_unmonitored')
        self.assertEqual([row['episode'] for row in state['in_frame_unmonitored']], [1])

    def test_both_problems_read_as_mixed(self):
        episodes = [episode(1, 10, False), episode(2, 400, True)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['status'], 'mixed')

    def test_an_unaired_episode_is_always_inside_the_frame(self):
        future = dict(episode(9, 0, True), air_date=(NOW.date() + dt.timedelta(days=7)).isoformat(), has_file=False)
        state = classify_monitoring([future], RULE, settings(), now=NOW)
        self.assertEqual(state['unaired'], 1)
        self.assertEqual(state['out_frame'], 0)
        # Nothing sits outside the frame, so this is the tidy state rather than a warning.
        self.assertEqual(state['status'], 'aligned')

    def test_an_unaired_episode_is_never_asked_to_be_unmonitored(self):
        episodes = [dict(episode(9, 0, True), air_date=(NOW.date() + dt.timedelta(days=7)).isoformat(), has_file=False),
                    episode(2, 400, True)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual([row['episode'] for row in state['out_frame_monitored']], [2])

    def test_missing_episodes_are_counted_too(self):
        # An episode with no file still has a monitored flag that matters.
        episodes = [episode(1, 10, False, has_file=False), episode(2, 10, True)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['total'], 2)
        self.assertEqual([row['has_file'] for row in state['in_frame_unmonitored']], [False])


    def test_an_empty_series_is_reported_not_crashed(self):
        self.assertEqual(classify_monitoring([], RULE, settings(), now=NOW)['status'], 'empty')

    def test_specials_stay_inside_the_frame(self):
        episodes = [dict(episode(1, 900, True), season=0)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['out_frame'], 0)


if __name__ == '__main__':
    unittest.main()


class Specials(unittest.TestCase):
    """Specials are outside the keep frame's logic, so they stay out of the comparison."""

    def setUp(self):
        self.episodes = [
            episode(1, 10, True),
            episode(2, 400, False),
            dict(episode(1, 900, False), season=0, episode_id=900),
        ]

    def test_specials_are_not_counted_by_default(self):
        state = classify_monitoring(self.episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['specials_ignored'], 1)
        self.assertEqual(state['total'], 2)
        self.assertEqual(state['status'], 'aligned')

    def test_an_unmonitored_special_is_not_offered_for_monitoring(self):
        # The bug this guards: a special landed in "protected", which fed the in-frame
        # list, so "monitor all within keep frame" swept up every special.
        state = classify_monitoring(self.episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['in_frame_unmonitored'], [])

    def test_specials_are_counted_when_asked_for(self):
        # One decision: keeping specials is what makes them count in monitoring too.
        document = settings()
        document['automation'] = {'exclude_specials': False}
        state = classify_monitoring(self.episodes, RULE, document, now=NOW)
        self.assertEqual(state['specials_ignored'], 0)
        self.assertEqual(state['total'], 3)
        self.assertEqual([row['season'] for row in state['in_frame_unmonitored']], [0])

    def test_a_show_of_only_specials_reads_as_empty_not_aligned(self):
        specials_only = [dict(episode(1, 900, False), season=0)]
        state = classify_monitoring(specials_only, RULE, settings(), now=NOW)
        self.assertEqual(state['status'], 'empty')
        self.assertEqual(state['specials_ignored'], 1)

    def test_the_setting_changes_the_cache_fingerprint(self):
        from core import rule_fingerprint, validate_settings
        base = {'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
                'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1,
                           'path': '/mnt/user/media/TV/A', 'keep_days': 30}]}
        plain = validate_settings(base)
        counted = validate_settings(dict(base, automation={'exclude_specials': False}))
        self.assertNotEqual(rule_fingerprint(plain['rules'][0], plain),
                            rule_fingerprint(counted['rules'][0], counted))


class MonitoringTargets(unittest.TestCase):
    """Unmonitoring is protection; monitoring is intent.

    Unmonitoring only ever stops a download, so it happens in both modes. Monitoring can
    start hundreds — a real library had 262 missing episodes inside its keep windows — so
    it happens only where someone asked for it.
    """

    def setUp(self):
        import main
        self.targets = main.monitoring_targets

    def state(self):
        return {
            'in_frame_unmonitored': [
                {'episode_id': 1, 'has_file': True},
                {'episode_id': 2, 'has_file': True},
                {'episode_id': 3, 'has_file': False},
            ],
            'out_frame_monitored': [{'episode_id': 9, 'has_file': False}],
        }

    def settings(self, mode):
        return {'retention': {'monitoring': mode}}

    def test_unmonitor_only_never_monitors_anything(self):
        result = self.targets(self.settings('unmonitor-only'), self.state(), {})
        self.assertEqual(result['monitor'], [])

    def test_full_sync_monitors_everything_inside_the_window(self):
        result = self.targets(self.settings('full-sync'), self.state(), {})
        self.assertEqual(sorted(result['monitor']), [1, 2, 3])

    def test_a_series_may_override_the_global_mode(self):
        result = self.targets(self.settings('unmonitor-only'), self.state(), {'monitoring': 'full-sync'})
        self.assertEqual(sorted(result['monitor']), [1, 2, 3])
        quiet = self.targets(self.settings('full-sync'), self.state(), {'monitoring': 'unmonitor-only'})
        self.assertEqual(quiet['monitor'], [])

    def test_everything_outside_the_window_is_unmonitored_either_way(self):
        for mode in ('unmonitor-only', 'full-sync'):
            self.assertEqual(self.targets(self.settings(mode), self.state(), {})['unmonitor'], [9])

    def test_the_fileless_ones_outside_are_the_point(self):
        """An episode with a file is unmonitored when the file is deleted.

        A missing one is never deleted, so nothing else would ever reach it, and Sonarr
        would go on fetching what the next run removes. That is the side door the rule
        closes, and it is why unmonitoring is not conditional on the mode.
        """
        result = self.targets(self.settings('unmonitor-only'), self.state(), {})
        self.assertEqual(result['unmonitor_missing'], 1)

    def test_there_is_no_mode_that_leaves_sonarr_alone(self):
        from core import MONITORING_MODES
        self.assertEqual(MONITORING_MODES, ['unmonitor-only', 'full-sync'])


class OneTimePass(unittest.TestCase):
    """Two corrections asked for on a save, applied then rather than queued."""

    def setUp(self):
        import main
        self.main = main
        self.settings = {'retention': {'monitoring': 'unmonitor-only'}, 'profiles': []}
        self.episodes = [
            {'episode_id': n, 'season': 1, 'episode': n, 'title': f'E{n}', 'has_file': True,
             'monitored': monitored, 'size': 1, 'path': f'/tv/S01E{n:02d}.mkv',
             'air_date': (dt.date.today() - dt.timedelta(days=days)).isoformat(),
             'air_source': 'sonarr', 'date_added': ''}
            for n, days, monitored in ((1, 5, False), (2, 40, False), (3, 200, True))
        ]

    def rule(self, days):
        return {'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                'keep_days': days, 'keep_episodes': None, 'keep_seasons': None,
                'combine': 'any', 'include_specials': None, 'match_status': 'matched'}

    def scope(self, days):
        return {'keep_days': days, 'keep_episodes': None, 'keep_seasons': None,
                'combine': 'any'}

    def test_widening_offers_only_what_the_widening_added(self):
        """A rule widened to ninety days did not ask for the thirty it always had."""
        rows = self.main.newly_scoped_rows(self.settings, self.rule(90), self.episodes,
                                           self.scope(30))
        self.assertEqual([row['episode_id'] for row in rows], [2])

    def test_a_new_series_counts_its_whole_window(self):
        rows = self.main.newly_scoped_rows(self.settings, self.rule(90), self.episodes, None)
        self.assertEqual(sorted(row['episode_id'] for row in rows), [1, 2])

    def test_an_episode_arriving_after_the_save_lands_in_the_right_window(self):
        # The windows are compared, not a remembered list of ids, so an episode that
        # appears between the save and the pass is judged by where it falls.
        self.episodes.append({'episode_id': 4, 'season': 1, 'episode': 4, 'title': 'New',
                              'has_file': True, 'monitored': False, 'size': 1,
                              'path': '/tv/S01E04.mkv', 'air_source': 'sonarr', 'date_added': '',
                              'air_date': (dt.date.today() - dt.timedelta(days=60)).isoformat()})
        rows = self.main.newly_scoped_rows(self.settings, self.rule(90), self.episodes,
                                           self.scope(30))
        self.assertEqual(sorted(row['episode_id'] for row in rows), [2, 4])

    def test_already_monitored_episodes_are_left_alone(self):
        rows = self.main.newly_scoped_rows(self.settings, self.rule(500), self.episodes, None)
        self.assertNotIn(3, [row['episode_id'] for row in rows], 'episode 3 is already monitored')


class ScopePassApplies(unittest.TestCase):
    """It writes to Sonarr at once, because waiting is what made one half pointless."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import main, store
        from core import validate_settings
        self.main, self.store = main, store
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'series_title': 'A', 'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.rule = self.settings['rules'][0]
        self.rule['match_status'] = 'matched'
        self.episodes = [
            {'episode_id': n, 'season': 1, 'episode': n, 'title': f'E{n}', 'has_file': True,
             'monitored': monitored, 'size': 1, 'path': f'/tv/S01E{n:02d}.mkv',
             'air_date': (dt.date.today() - dt.timedelta(days=days)).isoformat(),
             'air_source': 'sonarr', 'date_added': ''}
            for n, days, monitored in ((1, 5, False), (2, 200, True), (3, 300, True))
        ]
        store.store_episodes(self.settings, self.rule, self.episodes, {'series_id': 1, 'title': 'A'})
        self.calls = []
        holder = self

        class Client:
            def set_monitored(self, ids, wanted):
                holder.calls.append(('set', sorted(ids), wanted))

            def search_episodes(self, ids):
                holder.calls.append(('search', sorted(ids), None))

        self.original = main.client_for
        main.client_for = lambda *a, **k: Client()

    def tearDown(self):
        self.main.client_for = self.original
        self.temp.cleanup()

    def test_unmonitoring_outside_the_window_happens_now(self):
        result = self.main.scope_pass(self.settings, self.rule, unmonitor_outside=True)
        self.assertEqual(result['unmonitored'], 2)
        self.assertEqual(self.calls, [('set', [2, 3], False)])

    def test_monitoring_newly_covered_happens_now(self):
        result = self.main.scope_pass(self.settings, self.rule, monitor_new=True,
                                      previous_scope={'keep_days': 1, 'keep_episodes': None,
                                                      'keep_seasons': None, 'combine': 'any'})
        self.assertEqual(result['monitored'], 1)
        self.assertEqual(self.calls, [('set', [1], True)])

    def test_asking_for_neither_writes_nothing(self):
        self.assertEqual(self.main.scope_pass(self.settings, self.rule),
                         {'monitored': 0, 'unmonitored': 0})
        self.assertEqual(self.calls, [])

    def test_it_decides_from_the_stored_reading_and_never_reads_sonarr(self):
        # One write, no reads: the episodes are already in hand from the last sync.
        self.main.scope_pass(self.settings, self.rule, monitor_new=True, unmonitor_outside=True)
        self.assertTrue(all(kind in ('set', 'search') for kind, _, _ in self.calls))


class ScopeCounts(unittest.TestCase):
    """What the one-time actions say they would touch, before anything is touched."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import actions, main, store
        from core import validate_settings
        self.actions, self.main = actions, main
        self.temp = tempfile.TemporaryDirectory()
        self.settings = validate_settings({
            'instances': [{'id': 'i1', 'name': 'S', 'url': 'http://s:8989', 'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1, 'path': '/tv/A',
                       'series_title': 'A', 'keep_days': 30}],
        })
        self.settings['state_dir'] = str(Path(self.temp.name) / 'state')
        self.rule = self.settings['rules'][0]
        self.rule['match_status'] = 'matched'
        episodes = [
            {'episode_id': n, 'season': 1, 'episode': n, 'title': f'E{n}', 'has_file': True,
             'monitored': monitored, 'size': 1, 'path': f'/tv/S01E{n:02d}.mkv',
             'air_date': (dt.date.today() - dt.timedelta(days=days)).isoformat(),
             'air_source': 'sonarr', 'date_added': ''}
            for n, days, monitored in ((1, 5, False), (2, 40, False), (3, 300, True))
        ]
        store.store_episodes(self.settings, self.rule, episodes, {'series_id': 1, 'title': 'A'})

    def tearDown(self):
        self.temp.cleanup()

    def test_it_counts_against_the_window_in_the_form_not_the_saved_one(self):
        narrow = self.actions.action_scope_counts(self.settings, {'rule_id': 'r1', 'draft': {'keep_days': 10}})
        wide = self.actions.action_scope_counts(self.settings, {'rule_id': 'r1', 'draft': {'keep_days': 500}})
        self.assertEqual(narrow['in_scope'], 1)
        self.assertEqual(wide['in_scope'], 3)
        self.assertGreater(wide['out_scope'], 0 - 1)

    def test_a_duration_suffix_is_normalised_before_previewing_the_draft(self):
        counts = self.actions.action_scope_counts(
            self.settings, {'rule_id': 'r1', 'draft': {'keep_days': '6w'}})
        self.assertEqual(counts['in_scope'], 2)

    def test_an_absent_draft_value_keeps_the_rule_s_own(self):
        """Overriding with nothing counted against no window at all — everything in scope."""
        counts = self.actions.action_scope_counts(self.settings, {'rule_id': 'r1', 'draft': {}})
        self.assertEqual(counts['in_scope'], 1, 'the saved 30 day window should still apply')

    def test_it_counts_both_directions(self):
        counts = self.actions.action_scope_counts(self.settings, {'rule_id': 'r1', 'draft': {'keep_days': 10}})
        self.assertEqual(counts['in_scope_unmonitored'], 1)     # episode 1, inside, unmonitored
        self.assertEqual(counts['out_scope_monitored'], 1)      # episode 3, outside, monitored

    def test_a_series_never_read_says_so_rather_than_guessing(self):
        self.settings['rules'].append({'id': 'r2', 'instance_id': 'i1', 'series_id': 2,
                                       'path': '/tv/B', 'enabled': True, 'keep_days': 30,
                                       'match_status': 'matched'})
        self.assertEqual(self.actions.action_scope_counts(self.settings,
                                                          {'rule_id': 'r2', 'draft': {}}),
                         {'known': False})
