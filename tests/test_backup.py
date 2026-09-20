"""Backup archive validation and restore staging tests."""
import json
import contextlib
import datetime as dt
import threading
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import context  # noqa: F401
import backup
from core import Rejected, validate_settings


REAL_DATETIME = dt.datetime


class FixedDateTime(REAL_DATETIME):
    current = REAL_DATETIME(2026, 9, 19, 1, 30, tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is not None else cls.current.replace(tzinfo=None)


class BackupStaging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / 'config'
        self.destination = root / 'backups'
        self.config.mkdir()
        self.destination.mkdir()
        self.settings = validate_settings({
            'schedule': {'enabled': True, 'test_mode': False},
            'backup': {'path': '/tmp/tv-retention-backup-test/backups', 'keep': 2},
            'instances': [{'id': 'i1', 'name': 'Series', 'url': 'http://sonarr.invalid',
                           'api_key': 'a' * 32}],
            'rules': [{'id': 'r1', 'instance_id': 'i1', 'series_id': 1,
                       'series_title': 'Fixture', 'path': '/tv/Fixture', 'keep_days': 30,
                       'queue': {'removal': {'action': 'delete-series', 'request_id': 'request-1'}}}],
            'state_dir': '/tmp/tv-retention-backup-test/state',
        })
        # The worker contract is POSIX-path based; Windows normalizes these values while
        # validating, so restore fixtures must put the container form back before archiving.
        self.settings['rules'][0]['path'] = '/tv/Fixture'
        self.settings['backup']['path'] = '/tmp/tv-retention-backup-test/backups'
        self.settings['state_dir'] = str(self.config / 'state')
        self.settings_path = self.config / 'settings.json'
        archive_settings = dict(self.settings)
        archive_settings['state_dir'] = '/tmp/tv-retention-backup-test/state'
        self.settings_path.write_text(json.dumps(archive_settings), encoding='utf-8')
        state = self.config / 'state'
        state.mkdir()
        (state / 'run-intent.json').write_text(json.dumps({'id': 'run-1'}), encoding='utf-8')
        (state / 'removal-ledger.json').write_text(json.dumps({'version': 1, 'batches': []}),
                                                   encoding='utf-8')
        (state / 'jobs.json').write_text(json.dumps({'pending_run': 'stamp'}), encoding='utf-8')
        self.config_bytes = self.settings_path.read_bytes()
        self.patch_config = patch.object(backup, 'CONFIG', self.settings_path)
        self.patch_destination = patch.object(backup, 'destination', return_value=self.destination)
        self.patch_config.start()
        self.patch_destination.start()

    def tearDown(self):
        self.patch_config.stop()
        self.patch_destination.stop()
        self.temp.cleanup()

    def test_stage_restore_validates_and_quarantines_without_touching_active_config(self):
        archive = backup.create(self.settings)

        result = backup.stage_restore(self.settings, archive['file'])

        staged = Path(result['staging_dir'])
        restored = json.loads((staged / 'settings.json').read_text(encoding='utf-8'))
        self.assertFalse(restored['schedule']['enabled'])
        self.assertTrue(restored['schedule']['test_mode'])
        self.assertEqual(restored['rules'][0]['queue'], {})
        self.assertEqual(self.settings_path.read_bytes(), self.config_bytes)
        self.assertIn('state/run-intent.json', result['quarantined'])
        self.assertIn('state/removal-ledger.json', result['quarantined'])
        self.assertIn('state/jobs.json', result['quarantined'])
        self.assertIn('queued-removals.json', result['quarantined'])
        self.assertTrue((staged / 'restore-quarantine' / 'queued-removals.json').is_file())
        self.assertTrue((staged / 'restore-quarantine' / 'manifest.json').is_file())
        persisted = backup.load_staged_restore(self.settings)
        self.assertEqual(persisted['file'], archive['file'])
        self.assertEqual(persisted['staging_dir'], result['staging_dir'])
        self.assertTrue(persisted['requires_review'])

    def test_malformed_restored_settings_are_rejected_before_activation(self):
        archive_path = self.destination / 'tv-retention-invalid.zip'
        manifest = {'format': 1, 'files': ['settings.json'], 'contains_credentials': True}
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('settings.json', json.dumps({'rules': 'not-a-list'}))

        with self.assertRaisesRegex(Rejected, 'Restored settings are invalid'):
            backup.stage_restore(self.settings, archive_path.name)
        self.assertEqual(self.settings_path.read_bytes(), self.config_bytes)
        self.assertEqual(list(self.config.parent.glob('.tv-retention-restore-*')), [])

    def test_oversized_archive_is_rejected_before_extraction(self):
        archive_path = self.destination / 'tv-retention-large.zip'
        manifest = {'format': 1, 'files': ['settings.json', 'large.bin'], 'contains_credentials': True}
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('settings.json', self.settings_path.read_bytes())
            archive.writestr('large.bin', b'xx')

        with patch.object(backup, 'MAX_EXPANDED_BYTES', 1):
            with self.assertRaisesRegex(Rejected, 'allowed size'):
                backup.stage_restore(self.settings, archive_path.name)
        self.assertEqual(list(self.config.parent.glob('.tv-retention-restore-*')), [])

    def test_tampered_archive_file_is_rejected_without_touching_active_config(self):
        archive = backup.create(self.settings)
        archive_path = self.destination / archive['file']
        tampered = self.destination / 'tv-retention-tampered.zip'
        with zipfile.ZipFile(archive_path, 'r') as original, zipfile.ZipFile(tampered, 'w') as replacement:
            for info in original.infolist():
                data = original.read(info)
                if info.filename == 'settings.json':
                    data = data.replace(b'Fixture', b'Changed', 1)
                replacement.writestr(info, data)

        with self.assertRaisesRegex(Rejected, 'hash does not match'):
            backup.stage_restore(self.settings, tampered.name)
        self.assertEqual(self.settings_path.read_bytes(), self.config_bytes)
        self.assertEqual(list(self.config.parent.glob('.tv-retention-restore-*')), [])

    def test_concurrent_same_second_creates_publish_distinct_archives(self):
        barrier = threading.Barrier(2)
        call_lock = threading.Lock()
        call_count = 0
        real_link = backup.os.link

        def coordinated_link(source, target):
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count
            if current_call <= 2:
                barrier.wait(timeout=10)
            return real_link(source, target)

        with patch.object(backup.dt, 'datetime', FixedDateTime), \
                patch.object(backup, 'settings_transaction',
                             side_effect=lambda: contextlib.nullcontext()), \
                patch.object(backup.os, 'link', side_effect=coordinated_link):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: backup.create(self.settings), range(2)))

        self.assertEqual(len({result['file'] for result in results}), 2)
        self.assertEqual(sorted(result['file'] for result in results), [
            'tv-retention-20260919T013000Z-1.zip',
            'tv-retention-20260919T013000Z.zip',
        ])
        self.assertEqual(len(list(self.destination.glob('tv-retention-*.zip'))), 2)

    def test_retention_prunes_only_older_owned_archives(self):
        oldest = self.destination / 'tv-retention-20200101T000000Z.zip'
        newer = self.destination / 'tv-retention-20200102T000000Z.zip'
        unrelated = self.destination / 'other-service.zip'
        oldest.write_bytes(b'oldest')
        newer.write_bytes(b'newer')
        unrelated.write_bytes(b'keep')

        with patch.object(backup.dt, 'datetime', FixedDateTime):
            result = backup.create(self.settings)

        self.assertEqual(result['file'], 'tv-retention-20260919T013000Z.zip')
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())
        self.assertTrue((self.destination / result['file']).exists())
        self.assertTrue(unrelated.exists())

    def test_activation_requires_review_and_installs_forced_safe_settings(self):
        archive = backup.create(self.settings)
        backup.stage_restore(self.settings, archive['file'])

        with self.assertRaisesRegex(Rejected, 'Review quarantined'):
            backup.activate(self.settings, 'ACTIVATE')

        result = backup.activate(self.settings, 'ACTIVATE', review_pending=True)

        self.assertTrue(result['activated'])
        restored = json.loads(self.settings_path.read_text(encoding='utf-8'))
        self.assertFalse(restored['schedule']['enabled'])
        self.assertTrue(restored['schedule']['test_mode'])
        self.assertEqual(restored['rules'][0]['queue'], {})
        self.assertIsNone(backup.load_staged_restore(self.settings))
        self.assertFalse(backup.activation_pending())

    def test_interrupted_activation_rolls_back_before_next_action(self):
        archive = backup.create(self.settings)
        backup.stage_restore(self.settings, archive['file'])
        extra = self.config / 'state' / 'newer-cache.json'
        extra.write_text('{"newer": true}', encoding='utf-8')
        original = self.settings_path.read_bytes()
        real_copy = backup._copy_atomic
        calls = 0
        snapshot_calls = len(backup._source_files())

        def fail_after_first(source, target):
            nonlocal calls
            calls += 1
            if calls > snapshot_calls:
                raise OSError('simulated activation interruption')
            return real_copy(source, target)

        with patch.object(backup, '_copy_atomic', side_effect=fail_after_first):
            with self.assertRaisesRegex(Rejected, 'recovery is required'):
                backup.activate(self.settings, 'ACTIVATE', review_pending=True)
        self.assertTrue(backup.activation_pending())
        backup.recover_activation()
        self.assertEqual(self.settings_path.read_bytes(), original)
        self.assertTrue(extra.exists())
        self.assertFalse(backup.activation_pending())

    def test_completed_activation_recovery_cleans_marker_and_staging(self):
        archive = backup.create(self.settings)
        staged = backup.stage_restore(self.settings, archive['file'])
        recovery = self.config / backup.RECOVERY_DIR
        recovery.mkdir()
        backup.atomic_json(recovery / backup.RECOVERY_JOURNAL, {
            'format': 1, 'phase': 'complete', 'staging_dir': staged['staging_dir'],
            'state_dir': str(self.config / 'state'), 'install_files': ['settings.json'],
            'current_files': ['settings.json'],
        })

        backup.recover_activation()

        self.assertIsNone(backup.load_staged_restore(self.settings))
        self.assertFalse(Path(staged['staging_dir']).exists())
        self.assertFalse(backup.activation_pending())

    def test_activation_removes_files_absent_from_staged_restore(self):
        archive = backup.create(self.settings)
        backup.stage_restore(self.settings, archive['file'])
        extra = self.config / 'state' / 'newer-cache.json'
        extra.write_text('{"newer": true}', encoding='utf-8')

        backup.activate(self.settings, 'ACTIVATE', review_pending=True)

        self.assertFalse(extra.exists())

    def test_backup_rejects_state_outside_active_config(self):
        external = self.config.parent / 'external-state'
        external.mkdir()
        settings = dict(self.settings)
        settings['state_dir'] = str(external)

        with self.assertRaisesRegex(Rejected, 'outside the active config'):
            backup.create(settings)


if __name__ == '__main__':
    unittest.main()
