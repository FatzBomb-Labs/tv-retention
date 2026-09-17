"""Real os._exit + exec restart coverage of public run; all Sonarr I/O is in-process fake."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fake_sonarr import SERIES, episode_payload


QUERY = {'seriesId': ['1'], 'includeEpisodeFile': ['true']}


def rows(files=(99, 199), monitored=True):
    result = []
    for index, file_id in enumerate(files):
        row = episode_payload(101 + index, number=index + 1, file_id=file_id,
                              monitored=monitored)
        if file_id is not None:
            row['episodeFile']['size'] = (index + 1) * 1000
        result.append(row)
    return result


def reads(payload, catalogue=True):
    return ([{'method': 'GET', 'path': 'series', 'response': [SERIES]}]
            if catalogue else []) + [
        {'method': 'GET', 'path': 'episode', 'query': QUERY, 'response': payload}]


def monitor(ids):
    return {'method': 'PUT', 'path': 'episode/monitor',
            'body': {'episodeIds': ids, 'monitored': False}}


def delete(file_id, error=None):
    step = {'method': 'DELETE', 'path': 'episodefile/' + str(file_id)}
    if error:
        step['error'] = error
    return step


class RunInterruption(unittest.TestCase):
    def child(self, root, label, requests, **options):
        scenario = dict(requests=requests, events=label + '.jsonl',
                        result=label + '-result.json', **options)
        path = root / (label + '-scenario.json')
        path.write_text(json.dumps(scenario), encoding='utf-8')
        child = subprocess.run([sys.executable, str(Path(__file__).with_name('run_crash_child.py')),
                                str(root), str(path)], capture_output=True, text=True, timeout=30)
        expected_code = 73 if options.get('cut') and not options.get('fail') else 0
        self.assertEqual(child.returncode, expected_code, child.stdout + child.stderr)
        events = [json.loads(line) for line in (root / scenario['events']).read_text().splitlines()]
        self.assertNotEqual(events[0]['pid'], os.getpid())
        if expected_code == 73:
            self.assertEqual(events[-1]['kind'], 'cut', 'exit must be at the requested checkpoint')
            self.assertFalse((root / scenario['result']).exists())
            result = None
        else:
            result = json.loads((root / scenario['result']).read_text())['result']
        return result, events

    def intent(self, root):
        path = root / 'state' / 'run-intent.json'
        return json.loads(path.read_text()) if path.exists() else None

    def test_crash_before_and_after_every_operation_checkpoint_replans_in_fresh_process(self):
        # 1=initial plan; 2/3=unmonitor before/after dispatch; 4/5=delete99;
        # 6/7=delete199. Both sides of each real save are killed, without unwinding.
        writes = [monitor([101, 102]), delete(99), delete(199)]
        for checkpoint in range(1, 8):
            for phase in ('before', 'after'):
                with self.subTest(checkpoint=checkpoint, phase=phase), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    accepted = (checkpoint - 1) // 2
                    _, events = self.child(root, 'crash', reads(rows()) + writes[:accepted],
                                           cut=[checkpoint, phase])
                    self.assertEqual(len([e for e in events if e['kind'] == 'accepted']), accepted)
                    old = self.intent(root)
                    persisted = checkpoint - (phase == 'before')
                    if persisted == 0:
                        self.assertIsNone(old)
                    else:
                        statuses = ['pending'] * 3
                        attempts = [0] * 3
                        for i in range(3):
                            if persisted >= 2 + 2 * i:
                                statuses[i], attempts[i] = 'in-progress', 1
                            if persisted >= 3 + 2 * i:
                                statuses[i] = 'done'
                        self.assertEqual([op['status'] for op in old['operations']], statuses)
                        self.assertEqual([op['attempts'] for op in old['operations']], attempts)
                    # Accepted deletions are now authoritatively absent. Remaining file
                    # 199 has been replaced by 299 with a different size: no stale replay.
                    fresh = rows((None if accepted >= 2 else 99,
                                  None if accepted >= 3 else 299), monitored=accepted == 0)
                    if accepted < 3:
                        fresh[1]['episodeFile']['size'] = 4000
                    next_writes = ([monitor([101, 102])] if accepted == 0 else [])
                    next_writes += ([delete(99)] if accepted < 2 else [])
                    next_writes += ([delete(299)] if accepted < 3 else [])
                    result, resumed = self.child(root, 'restart',
                        reads(fresh, catalogue=old is not None) + next_writes, scheduled=True)
                    self.assertNotEqual(events[0]['pid'], resumed[0]['pid'])
                    self.assertEqual(resumed[0]['prior_intent'], old)
                    self.assertEqual(result['status'], 'complete')
                    self.assertEqual(result['deleted'], int(accepted < 2) + int(accepted < 3))
                    self.assertEqual(result['freed_bytes'],
                                     (1000 if accepted < 2 else 0) + (4000 if accepted < 3 else 0))
                    if old:
                        self.assertNotEqual(result['id'], old['id'])
                        archives = list((root / 'state' / 'run-history').glob('*.json'))
                        self.assertEqual([json.loads(p.read_text()) for p in archives], [old])
                    # A third fresh process sees no files: no duplicate write or success.
                    last, _ = self.child(root, 'again', reads(rows((None, None), False), False))
                    self.assertEqual((last['deleted'], last['freed_bytes']), (0, 0))

    def test_failed_initial_or_operation_checkpoint_prevents_next_write(self):
        writes = [monitor([101, 102]), delete(99)]
        for number, accepted in ((1, 0), (2, 0), (3, 1), (4, 1), (5, 2), (6, 2)):
            with self.subTest(checkpoint=number), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result, events = self.child(root, 'failure', reads(rows()) + writes[:accepted],
                                           cut=[number, 'before'], fail=True)
                self.assertIn('checkpoint_error', result)
                self.assertEqual(len([e for e in events if e['kind'] == 'accepted']), accepted)
                if number == 1:
                    self.assertIsNone(self.intent(root))

    def test_failed_unmonitor_blocks_all_deletes_then_fresh_run_replans(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            step = dict(monitor([101, 102]), error='unmonitor unavailable')
            first, events = self.child(root, 'failure', reads(rows()) + [step])
            self.assertEqual(first['status'], 'incomplete')
            self.assertEqual((first['deleted'], first['freed_bytes']), (0, 0))
            self.assertFalse(any(e['kind'] == 'accepted' for e in events))
            self.assertEqual([op['status'] for op in self.intent(root)['operations']],
                             ['failed', 'pending', 'pending'])
            result, _ = self.child(root, 'restart', reads(rows()) + [
                monitor([101, 102]), delete(99), delete(199)])
            self.assertEqual((result['deleted'], result['freed_bytes']), (2, 3000))

    def test_partial_success_counts_only_acknowledged_bytes_and_retry_only_remaining(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, _ = self.child(root, 'partial', reads(rows((99, 199, 399))) + [
                monitor([101, 102, 103]), delete(99), delete(199, 'delete unavailable')])
            self.assertEqual(first['status'], 'incomplete')
            self.assertEqual((first['planned'], first['deleted'], first['freed_bytes']), (3, 1, 1000))
            self.assertEqual([op['status'] for op in self.intent(root)['operations']],
                             ['done', 'done', 'failed', 'pending'])
            second, _ = self.child(root, 'restart', reads(rows((None, 199, 399), False)) + [
                delete(199), delete(399)])
            self.assertEqual((second['planned'], second['deleted'], second['freed_bytes']), (2, 2, 5000))
            state = json.loads((root / 'state' / 'state.json').read_text())
            self.assertEqual([run['freed_bytes'] for run in state['runs']], [1000, 5000])

    def test_restart_under_test_mode_preserves_interrupted_intent_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.child(root, 'crash', reads(rows()) + [monitor([101, 102]), delete(99)],
                       cut=[5, 'before'])
            before = (root / 'state' / 'run-intent.json').read_bytes()
            result, events = self.child(root, 'preview', reads(rows((None, 199), False), False),
                                        test_mode=True)
            self.assertTrue(result['test_mode'])
            self.assertEqual((result['deleted'], result['freed_bytes']), (0, 0))
            self.assertFalse(any(e['kind'] == 'accepted' for e in events))
            self.assertEqual((root / 'state' / 'run-intent.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
