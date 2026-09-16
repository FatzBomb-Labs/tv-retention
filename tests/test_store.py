"""`read_log`, the durable run intent, and `state_dir`'s writability cache.

Both touch no network and import no `fcntl`, so this runs locally as well as on the host.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import context  # noqa: F401
import store
from store import read_log


class ReadLog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / 'state'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings = {'state_dir': str(self.state_dir)}
        self.log = self.state_dir / 'tv-retention.log'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, data: bytes):
        self.log.write_bytes(data)

    def test_a_missing_log_is_reported_as_empty_not_an_error(self):
        self.assertEqual(read_log(self.settings), {'offset': 0, 'size': 0, 'text': ''})

    def test_a_first_read_returns_the_tail_and_the_offset_lands_on_the_end(self):
        self.write(b'a' * 100)
        result = read_log(self.settings, offset=0, limit=40)
        self.assertEqual(result['text'], 'a' * 40)
        self.assertEqual(result['offset'], 100)
        self.assertEqual(result['size'], 100)

    def test_polling_from_a_returned_offset_reads_only_what_is_new(self):
        self.write(b'first\n')
        first = read_log(self.settings, offset=0, limit=65536)
        self.assertEqual(first['text'], 'first\n')
        self.assertEqual(first['offset'], 6)

        with open(self.log, 'ab') as handle:
            handle.write(b'second\n')
        second = read_log(self.settings, offset=first['offset'], limit=65536)
        self.assertEqual(second['text'], 'second\n')
        self.assertEqual(second['offset'], 13)

    def test_a_rotation_that_shrinks_the_file_starts_again_from_the_beginning(self):
        self.write(b'x' * 100)
        stale_offset = 100
        self.write(b'restarted\n')     # the rotated file is short; the old offset overshoots
        result = read_log(self.settings, offset=stale_offset, limit=65536)
        self.assertEqual(result['text'], 'restarted\n')
        self.assertEqual(result['offset'], len(b'restarted\n'))

    def test_a_chunk_boundary_splitting_a_multi_byte_character_does_not_desync_the_offset(self):
        # 'é' is two bytes in UTF-8: \xc3 at byte 3, \xa9 at byte 4. Reading only byte 4 in
        # isolation is invalid UTF-8 on its own — exactly the shape of a chunk boundary
        # landing mid-character — and errors='replace' turns it into one U+FFFD.
        data = 'café — 日本語\n'.encode('utf-8')
        self.write(data)
        # offset=4, not 0: an explicit position lands the read exactly on that split,
        # rather than the offset<=0 special case, which means "give me the tail".
        split = read_log(self.settings, offset=4, limit=1)
        self.assertEqual(split['offset'], 5, 'exactly the one byte read, whatever it decoded to')
        rest = read_log(self.settings, offset=split['offset'], limit=65536)
        self.assertEqual(rest['offset'], len(data), 'the remaining bytes account for the rest of the file exactly')

    def test_a_malformed_byte_does_not_desync_the_offset(self):
        # A lone continuation byte is invalid UTF-8 on its own. Decoded with
        # errors='replace' it becomes one U+FFFD, which re-encodes to three bytes - the
        # text-mode read this replaced counted that re-encoded length as bytes consumed,
        # overshooting the real length by two bytes for every byte like this one.
        data = b'before \x80 after\n'
        self.write(data)
        result = read_log(self.settings, offset=0, limit=65536)
        self.assertEqual(result['offset'], len(data),
                         'the offset must equal the bytes actually read, not the re-encoded text')
        self.assertEqual(result['size'], len(data))
        self.assertIn('\ufffd', result['text'])
        self.assertIn('before', result['text'])
        self.assertIn('after', result['text'])

    def test_an_overshot_offset_from_a_malformed_chunk_never_replays_what_was_already_shown(self):
        # The consequence of the drift this guards against: an offset past the end of the
        # file is treated as a rotation and restarts from byte zero, replaying content a
        # reader has already seen. A correct offset can never overshoot in the first
        # place, so this never fires.
        data = b'before \x80 after\n'
        self.write(data)
        first = read_log(self.settings, offset=0, limit=65536)
        self.assertLessEqual(first['offset'], first['size'])
        second = read_log(self.settings, offset=first['offset'], limit=65536)
        self.assertEqual(second['text'], '', 'nothing new has been appended, so nothing is replayed')
        self.assertEqual(second['offset'], first['offset'])


class StateDir(unittest.TestCase):
    """`state_dir`'s writability probe, and the cache that keeps it off the hot path.

    `state_dir` runs on every cache read, cache write, log line and journal append, so a
    probe on every call was steady churn on flash-backed storage. The cache exists so a
    directory confirmed writable moments ago is trusted rather than reprobed — but it has
    a short lifetime, because the reason to probe at all is to notice the array going
    down, and that has to still be noticed within a bounded time, not only at the first
    call ever made.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_config = store.CONFIG
        store.CONFIG = Path(self.temp.name) / 'settings.json'
        store._writable_since.clear()   # a fresh test must not inherit another test's cache

    def tearDown(self):
        store.CONFIG = self.original_config
        store._writable_since.clear()
        self.temp.cleanup()

    def test_a_writable_directory_is_created_and_returned(self):
        settings = {'state_dir': str(Path(self.temp.name) / 'primary')}
        result = store.state_dir(settings)
        self.assertEqual(result, Path(settings['state_dir']))
        self.assertTrue(result.is_dir())

    def test_a_directory_that_cannot_be_created_falls_back_to_the_config_adjacent_state(self):
        blocked = Path(self.temp.name) / 'blocked'
        blocked.write_text('a file sits where the directory should be')   # mkdir must fail here
        settings = {'state_dir': str(blocked)}
        result = store.state_dir(settings)
        self.assertEqual(result, store.CONFIG.parent / 'state')
        self.assertTrue(result.is_dir())

    def test_a_second_call_within_the_recheck_window_does_not_reprobe(self):
        settings = {'state_dir': str(Path(self.temp.name) / 'primary')}
        store.state_dir(settings)
        probed = []
        original = Path.write_text
        def counting(self, *args, **kwargs):
            probed.append(self)
            return original(self, *args, **kwargs)
        with mock.patch.object(Path, 'write_text', counting):
            store.state_dir(settings)
        self.assertEqual(probed, [], 'a directory confirmed writable moments ago is not probed again')

    def test_a_call_after_the_recheck_window_probes_again(self):
        settings = {'state_dir': str(Path(self.temp.name) / 'primary')}
        store.state_dir(settings)
        key = str(Path(settings['state_dir']))
        store._writable_since[key] -= store.WRITABLE_RECHECK_SECONDS + 1
        probed = []
        original = Path.write_text
        def counting(self, *args, **kwargs):
            probed.append(self)
            return original(self, *args, **kwargs)
        with mock.patch.object(Path, 'write_text', counting):
            store.state_dir(settings)
        self.assertEqual(len(probed), 1, 'a directory not verified recently is probed again')

    def test_a_directory_that_becomes_writable_again_is_used_on_the_very_next_call(self):
        # Failure is never cached — only success is, and only briefly — so the primary
        # directory becoming writable again (the array coming back up) is noticed on the
        # next call, not after a wait. The cache must not turn one failed probe into a
        # standing decision to keep using the fallback.
        blocked = Path(self.temp.name) / 'blocked'
        blocked.write_text('a file sits where the directory should be')
        settings = {'state_dir': str(blocked)}
        first = store.state_dir(settings)
        self.assertEqual(first, store.CONFIG.parent / 'state')

        blocked.unlink()
        blocked.mkdir()
        second = store.state_dir(settings)
        self.assertEqual(second, blocked, 'now writable, and used again rather than left on the fallback')


class RunIntent(unittest.TestCase):
    """The unfinished run record is atomic and never confused with the audit journal."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = {'state_dir': str(Path(self.temp.name) / 'state')}
        store._writable_since.clear()

    def tearDown(self):
        store._writable_since.clear()
        self.temp.cleanup()

    def test_missing_intent_is_not_an_error(self):
        self.assertIsNone(store.load_intent(self.settings))

    def test_intent_round_trips_as_one_replaceable_record(self):
        first = {'id': 'run-one', 'status': 'staged', 'operations': [{'status': 'pending'}]}
        second = {'id': 'run-one', 'status': 'complete', 'operations': [{'status': 'done'}]}
        store.save_intent(self.settings, first)
        store.save_intent(self.settings, second)
        self.assertEqual(store.load_intent(self.settings), second)
        self.assertFalse((Path(self.settings['state_dir']) / 'run-intent.json.tmp').exists())

