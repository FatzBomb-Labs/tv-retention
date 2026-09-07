import datetime as dt
import unittest

import context  # noqa: F401
from core import DEFAULTS, classify_monitoring

NOW = dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc)


def settings():
    return {'retention': dict(DEFAULTS['retention']),
            'guards': {'max_deletes_per_run': 1000, 'max_percent_per_rule': 50, 'min_file_age_hours': 0}}


def episode(number, days_ago, monitored, has_file=True):
    aired = (NOW - dt.timedelta(days=days_ago)).date()
    return {'episode_id': number, 'season': 1, 'episode': number, 'title': f'E{number}',
            'path': f'/mnt/user/media/TV/Show/S01E{number:02d}.mkv' if has_file else f'sonarr:episode:{number}',
            'air_date': aired.isoformat(), 'air_source': 'sonarr', 'has_file': has_file,
            'monitored': monitored, 'mtime': (NOW - dt.timedelta(days=days_ago)).timestamp(), 'size': 100}


RULE = {'keep_days': 180, 'combine': 'earliest'}


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

    def test_the_percentage_guard_does_not_limit_the_view(self):
        episodes = [episode(index, 400, True) for index in range(1, 11)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(len(state['out_frame_monitored']), 10)

    def test_an_empty_series_is_reported_not_crashed(self):
        self.assertEqual(classify_monitoring([], RULE, settings(), now=NOW)['status'], 'empty')

    def test_specials_stay_inside_the_frame(self):
        episodes = [dict(episode(1, 900, True), season=0)]
        state = classify_monitoring(episodes, RULE, settings(), now=NOW)
        self.assertEqual(state['out_frame'], 0)


if __name__ == '__main__':
    unittest.main()
