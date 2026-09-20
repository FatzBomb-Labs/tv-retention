"""Safe, self-contained backups of TV Retention's config volume.

The archive boundary is deliberately the application volume, not the media library.  A
backup is an operator action, and restore is guarded by an explicit confirmation token;
neither operation talks to Sonarr or changes a monitored flag.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import stat
import tempfile
import zipfile
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from core import Rejected, atomic_json, validate_settings
from migrate import migrate
from store import CONFIG, RUNTIME, settings_transaction, state_dir

PREFIX = 'tv-retention-'
SUFFIX = '.zip'
MAX_ARCHIVE_MEMBERS = 10000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
QUARANTINE_FILES = {'state/run-intent.json', 'state/removal-ledger.json', 'state/jobs.json'}
STAGED_RESTORE_FILE = 'restore-staging.json'
RECOVERY_DIR = '.tv-retention-restore-recovery'
RECOVERY_JOURNAL = 'activation.json'


def _source_root() -> Path:
    return CONFIG.parent.resolve()


def destination(settings: dict) -> Path:
    value = ((settings.get('backup') or {}).get('path') or '').strip()
    if not value:
        raise Rejected('Set a backup destination before creating a backup.')
    path = Path(value)
    if not path.is_absolute():
        raise Rejected('Backup destination must be an absolute path')
    if '..' in path.parts:
        raise Rejected('Backup destination may not contain ".."')
    path = path.resolve()
    source = _source_root()
    # A destination inside /config would be copied into its own next archive, while a
    # parent of /config would make the source and destination overlap.  Both are a
    # surprising restore boundary, so require a genuinely separate directory.
    if _inside(path, source) or _inside(source, path):
        raise Rejected('Backup destination must be outside the active config directory')
    return path


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _files(settings: dict, dest: Path) -> list[tuple[Path, str]]:
    source = _source_root()
    found = []
    if not source.exists():
        return found
    for path in source.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve()
        if _inside(resolved, dest):
            continue
        relative = str(path.relative_to(source)).replace(os.sep, '/')
        if _excluded_source_name(relative):
            continue
        # Temporary files are never a useful restore point and can contain half-written
        # JSON from a process that was interrupted.
        found.append((path, relative))
    return sorted(found, key=lambda item: item[1])


def _require_state_inside_config(settings: dict) -> None:
    configured = str(settings.get('state_dir') or '').strip()
    if not configured:
        raise Rejected('Backup cannot determine the application state directory.')
    state = Path(configured).resolve()
    if not _inside(state, _source_root()):
        raise Rejected('Backup cannot omit state stored outside the active config directory.')


def _excluded_source_name(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return (RECOVERY_DIR in parts or STAGED_RESTORE_FILE in parts
            or 'restore-staging' in parts
            or (parts and parts[-1].startswith('tv-retention-')
                and PurePosixPath(parts[-1]).suffix in ('.tmp', '.part')))


def _source_files() -> list[tuple[Path, str]]:
    source = _source_root()
    found = []
    if not source.exists():
        return found
    for path in source.rglob('*'):
        if path.is_file() and not path.is_symlink():
            relative = str(path.relative_to(source)).replace(os.sep, '/')
            if not _excluded_source_name(relative):
                found.append((path, relative))
    return sorted(found, key=lambda item: item[1])


def create(settings: dict) -> dict:
    """Create an atomic timestamped zip and return only non-sensitive metadata."""
    _require_state_inside_config(settings)
    dest = destination(settings)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if not os.access(dest, os.W_OK):
            raise OSError('destination is not writable')
    except OSError as error:
        raise Rejected(f'Backup destination is not writable ({error})') from error
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    # The settings transaction makes the archive's source list one short, coordinated
    # snapshot. Publication happens with link(), which refuses to replace a name another
    # backup won while this one was being written.
    try:
        with settings_transaction():
            files = _files(settings, dest)
            manifest = {'format': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                        'files': [name for _, name in files],
                        'contains_credentials': True,
                        'sha256': {name: _sha256(path) for path, name in files}}
            suffix = 0
            while True:
                final = dest / f'{PREFIX}{stamp}{SUFFIX}' if not suffix else \
                    dest / f'{PREFIX}{stamp}-{suffix}{SUFFIX}'
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=f'.{final.name}.', suffix='.tmp', dir=str(dest))
                os.close(descriptor)
                temporary = Path(temporary_path)
                try:
                    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr('manifest.json', json.dumps(manifest, indent=2) + '\n')
                        for path, name in files:
                            archive.write(path, name)
                    try:
                        os.link(temporary, final)
                    except FileExistsError:
                        temporary.unlink(missing_ok=True)
                        suffix += 1
                        continue
                    temporary.unlink(missing_ok=True)
                    break
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
    except (OSError, zipfile.BadZipFile) as error:
        raise Rejected(f'Backup failed ({error})') from error
    # Retention is deliberately limited to archives with our own prefix. A destination
    # may be shared with another service, so pruning must never become a directory-wide
    # cleanup. The validator caps this value, while the defensive clamp keeps direct
    # callers safe too.
    try:
        keep = max(1, min(100, int((settings.get('backup') or {}).get('keep', 5))))
    except (TypeError, ValueError):
        keep = 5
    archives = sorted((item for item in dest.glob(f'{PREFIX}*{SUFFIX}')
                       if item.is_file() and not item.is_symlink()),
                      key=lambda item: item.name, reverse=True)
    pruned = 0
    for stale in archives[keep:]:
        try:
            stale.unlink()
            pruned += 1
        except OSError:
            # A failed cleanup does not invalidate the point just created. The next
            # backup retries it, and the status page can still report the usable archive.
            pass
    return {'file': final.name, 'path': str(final), 'created_at': stamp,
            'bytes': final.stat().st_size, 'files': len(files),
            'contains_credentials': True, 'pruned': pruned}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def list_backups(settings: dict) -> list[dict]:
    """List valid-looking archives without opening or extracting them."""
    dest = destination(settings)
    if not dest.exists():
        return []
    found = []
    for path in dest.glob(f'{PREFIX}*{SUFFIX}'):
        if path.is_file() and not path.is_symlink():
            found.append({'file': path.name, 'bytes': path.stat().st_size,
                          'modified_at': dt.datetime.fromtimestamp(
                              path.stat().st_mtime, dt.timezone.utc).isoformat()})
    return sorted(found, key=lambda item: item['file'], reverse=True)


def restore(settings: dict, filename: str, confirm: str = '') -> dict:
    """Restore archived application files after an explicit ``RESTORE`` confirmation."""
    raise Rejected('Restore is temporarily unavailable until safe settings and pending-work '
                   'activation is implemented. No files were restored.')


def activation_pending() -> bool:
    return (_source_root() / RECOVERY_DIR / RECOVERY_JOURNAL).is_file()


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f'.{target.name}.restore-', suffix='.tmp', dir=str(target.parent))
    os.close(descriptor)
    temporary = Path(temporary_path)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read_activation_journal() -> tuple[Path, dict] | None:
    root = _source_root() / RECOVERY_DIR
    journal = root / RECOVERY_JOURNAL
    if not journal.exists():
        return None
    try:
        value = json.loads(journal.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Restore activation recovery data is unreadable ({error}).') from error
    if not isinstance(value, dict) or value.get('format') != 1:
        raise Rejected('Restore activation recovery data is invalid.')
    return root, value


def recover_activation() -> dict | None:
    """Finish or roll back an interrupted local restore before serving actions."""
    found = _read_activation_journal()
    if not found:
        return None
    root, journal = found
    if journal.get('phase') == 'snapshotting':
        shutil.rmtree(root, ignore_errors=False)
        return {'recovered': 'snapshot-aborted'}
    if journal.get('phase') == 'complete':
        marker = state_dir({'state_dir': journal.get('state_dir')}, required=True) / STAGED_RESTORE_FILE
        marker.unlink(missing_ok=True)
        staging = Path(str(journal.get('staging_dir') or ''))
        if staging.is_dir() and _inside(staging.resolve(), (marker.parent / 'restore-staging').resolve()):
            shutil.rmtree(staging, ignore_errors=False)
        shutil.rmtree(root, ignore_errors=False)
        return {'recovered': 'completed'}
    rollback = root / 'current'
    install_names = journal.get('install_files')
    current_names = journal.get('current_files')
    if (not isinstance(install_names, list) or any(not isinstance(name, str) for name in install_names)
            or not isinstance(current_names, list)
            or any(not isinstance(name, str) for name in current_names)):
        raise Rejected('Restore activation recovery data has no valid file list.')
    for name in current_names:
        target = _source_root() / Path(*PurePosixPath(name).parts)
        saved = rollback / Path(*PurePosixPath(name).parts)
        if saved.is_file():
            _copy_atomic(saved, target)
        else:
            target.unlink(missing_ok=True)
    for name in set(install_names) - set(current_names):
        (_source_root() / Path(*PurePosixPath(name).parts)).unlink(missing_ok=True)
    shutil.rmtree(root, ignore_errors=False)
    return {'recovered': 'rolled-back'}


def activate(settings: dict, confirm: str = '', review_pending: bool = False) -> dict:
    """Install a validated staged restore while the caller holds the run lock."""
    if confirm != 'ACTIVATE':
        raise Rejected('Type ACTIVATE to install the staged restore.')
    staged = load_staged_restore(settings)
    if not staged:
        raise Rejected('No restore is staged.')
    if staged.get('requires_review') and not review_pending:
        raise Rejected('Review quarantined pending work before activating this restore.')
    staging = Path(staged['staging_dir']).resolve()
    metadata_manifest = staged.get('manifest') or {}
    names = metadata_manifest.get('files')
    hashes = metadata_manifest.get('sha256')
    if (not isinstance(names, list) or not isinstance(hashes, dict)
            or set(names) != set(hashes)):
        raise Rejected('The staged restore has no complete manifest.')
    install_names = []
    quarantined = set(staged.get('quarantined') or [])
    for name in names:
        if name in quarantined:
            continue
        source = staging / Path(*PurePosixPath(name).parts)
        if not source.is_file() or _sha256(source) != hashes[name]:
            raise Rejected(f'The staged restore file is missing or changed: {name}.')
        install_names.append(name)
    settings_path = staging / 'settings.json'
    try:
        staged_settings = json.loads(settings_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'The staged settings are unreadable ({error}).') from error
    if (not isinstance(staged_settings, dict)
            or staged_settings.get('schedule', {}).get('enabled')
            or not staged_settings.get('schedule', {}).get('test_mode', True)):
        raise Rejected('The staged restore must keep schedules off and Test Mode on.')

    source_root = _source_root()
    recovery = source_root / RECOVERY_DIR
    if recovery.exists():
        raise Rejected('Restore activation recovery is pending; restart or recover it first.')
    rollback = recovery / 'current'
    rollback.mkdir(parents=True, exist_ok=False)
    current_names = [name for _, name in _source_files()]
    atomic_json(recovery / RECOVERY_JOURNAL, {
        'format': 1, 'phase': 'snapshotting', 'staging_dir': str(staging),
        'state_dir': str(state_dir(settings, required=True)),
        'install_files': install_names, 'current_files': current_names,
    })
    for path, name in _source_files():
        _copy_atomic(path, rollback / Path(*PurePosixPath(name).parts))
    atomic_json(recovery / RECOVERY_JOURNAL, {
        'format': 1, 'phase': 'installing', 'staging_dir': str(staging),
        'state_dir': str(state_dir(settings, required=True)),
        'install_files': install_names, 'current_files': current_names,
    })
    try:
        for name in install_names:
            _copy_atomic(staging / Path(*PurePosixPath(name).parts),
                         source_root / Path(*PurePosixPath(name).parts))
        for name in set(current_names) - set(install_names):
            (source_root / Path(*PurePosixPath(name).parts)).unlink(missing_ok=True)
        atomic_json(recovery / RECOVERY_JOURNAL, {
            'format': 1, 'phase': 'complete', 'staging_dir': str(staging),
            'state_dir': str(state_dir(settings, required=True)),
            'install_files': install_names, 'current_files': current_names,
        })
        marker = state_dir(settings, required=True) / STAGED_RESTORE_FILE
        marker.unlink(missing_ok=True)
        shutil.rmtree(recovery, ignore_errors=False)
    except Exception as error:
        raise Rejected(f'Restore activation interrupted; recovery is required ({error}).') from error
    return {'activated': True, 'file': staged['file'], 'requires_reload': True,
            'quarantined': staged.get('quarantined') or []}


def stage_restore(settings: dict, filename: str, confirm: str = 'RESTORE') -> dict:
    """Validate and stage a restore without changing the active config volume."""
    if confirm != 'RESTORE':
        raise Rejected('Type RESTORE to confirm replacing the saved application files.')
    dest = destination(settings)
    name = Path(str(filename or '')).name
    if name != filename or not name.startswith(PREFIX) or not name.endswith(SUFFIX):
        raise Rejected('Choose a TV Retention backup archive.')
    archive_path = (dest / name).resolve()
    if not _inside(archive_path, dest) or not archive_path.is_file():
        raise Rejected('That backup archive does not exist.')
    state = state_dir(settings, required=True)
    marker_path = state / STAGED_RESTORE_FILE
    if marker_path.exists():
        raise Rejected('A restore is already staged; review or discard it before staging another.')
    staging_root = state / 'restore-staging'
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='restore-', dir=str(staging_root))).resolve()
    try:
        files, manifest = _extract_archive(archive_path, staging)
        settings_path = staging / 'settings.json'
        if not settings_path.is_file():
            raise Rejected('The backup does not contain settings.json.')
        try:
            restored = migrate(json.loads(settings_path.read_text(encoding='utf-8')))
            restored = validate_settings(restored, previous=restored)
        except Exception as error:  # noqa: BLE001 - malformed archives must fail closed
            raise Rejected(f'Restored settings are invalid ({error}).') from error
        restored['schedule']['enabled'] = False
        restored['schedule']['test_mode'] = True
        queues = {}
        for rule in restored.get('rules') or []:
            queue = rule.get('queue') or {}
            if queue:
                queues[rule['id']] = queue
                rule['queue'] = {}
        quarantine = staging / 'restore-quarantine'
        quarantine.mkdir(parents=True, exist_ok=True)
        quarantined = []
        for relative in list(files):
            if relative not in QUARANTINE_FILES:
                continue
            incoming = staging / Path(*PurePosixPath(relative).parts)
            target = quarantine / Path(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(incoming, target)
            quarantined.append(relative)
        if queues:
            atomic_json(quarantine / 'queued-removals.json', queues)
            quarantined.append('queued-removals.json')
        atomic_json(settings_path, restored)
        atomic_json(quarantine / 'manifest.json', {
            'format': 1,
            'source': archive_path.name,
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'reason': 'pending work and operator queues require explicit review',
            'files': quarantined,
        })
        staged_files = []
        staged_hashes = {}
        for path in staging.rglob('*'):
            if not path.is_file() or _inside(path.resolve(), quarantine.resolve()):
                continue
            relative = str(path.relative_to(staging)).replace(os.sep, '/')
            staged_files.append(relative)
            staged_hashes[relative] = _sha256(path)
        staged_manifest = {'format': 1, 'files': sorted(staged_files),
                           'sha256': staged_hashes,
                           'source_manifest': manifest}
        metadata = {'format': 1, 'file': name,
                    'source_archive': str(archive_path),
                    'staging_dir': str(staging),
                    'staged_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'files': len(files), 'quarantined': quarantined,
                'requires_review': bool(quarantined), 'requires_reload': True,
                    'manifest': staged_manifest}
        atomic_json(marker_path, metadata)
    except (OSError, zipfile.BadZipFile) as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise Rejected(f'Restore failed ({error})') from error
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**metadata, 'manifest': manifest}


def load_staged_restore(settings: dict) -> dict | None:
    """Read the durable staged-restore marker without activating it."""
    path = state_dir(settings, required=True) / STAGED_RESTORE_FILE
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise Rejected(f'Staged restore metadata is unreadable ({error}).') from error
    if not isinstance(value, dict) or value.get('format') != 1:
        raise Rejected('Staged restore metadata is invalid.')
    staging = Path(str(value.get('staging_dir') or '')).resolve()
    root = (path.parent / 'restore-staging').resolve()
    if not _inside(staging, root) or not staging.is_dir():
        raise Rejected('Staged restore data is missing; discard the stale restore marker.')
    return value


def _extract_archive(archive_path: Path, staging: Path) -> tuple[list[str], dict]:
    """Extract a manifest-authorized archive into staging with bounded expansion."""
    with zipfile.ZipFile(archive_path, 'r') as archive:
        manifests = [info for info in archive.infolist()
                     if info.filename.replace('\\', '/') == 'manifest.json']
        if len(manifests) != 1:
            raise Rejected('The backup is missing its manifest.')
        try:
            manifest = json.loads(archive.read(manifests[0]).decode('utf-8'))
        except (UnicodeDecodeError, ValueError, OSError, zipfile.BadZipFile) as error:
            raise Rejected(f'The backup manifest is invalid ({error}).') from error
        if not isinstance(manifest, dict) or manifest.get('format') != 1:
            raise Rejected('The backup format is not supported.')
        declared = manifest.get('files')
        if (not isinstance(declared, list) or len(declared) > MAX_ARCHIVE_MEMBERS
                or any(not isinstance(name, str) for name in declared)):
            raise Rejected('The backup manifest has no valid file list.')
        declared_names = {_safe_member_name(name) for name in declared}
        if len(declared_names) != len(declared) or 'manifest.json' in declared_names:
            raise Rejected('The backup manifest contains an unsafe or duplicate path.')
        members = []
        total = 0
        hashes = manifest.get('sha256') or {}
        if not isinstance(hashes, dict) or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in hashes.items()):
            raise Rejected('The backup manifest has no valid file hashes.')
        for info in archive.infolist():
            if info.filename.replace('\\', '/') == 'manifest.json':
                continue
            if info.is_dir():
                continue
            if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                raise Rejected('The backup contains a symbolic link.')
            name = _safe_member_name(info.filename)
            total += info.file_size
            if total > MAX_EXPANDED_BYTES:
                raise Rejected('The backup expands beyond the allowed size.')
            members.append(name)
        if len(set(members)) != len(members) or set(members) != declared_names:
            raise Rejected('The backup manifest does not match its contents.')
        if hashes and set(hashes) != declared_names:
            raise Rejected('The backup manifest does not hash every file.')
        files = []
        streamed_total = 0
        for info in archive.infolist():
            if info.filename.replace('\\', '/') == 'manifest.json' or info.is_dir():
                continue
            member = PurePosixPath(_safe_member_name(info.filename))
            target = (staging / Path(*member.parts)).resolve()
            if not _inside(target, staging.resolve()):
                raise Rejected('The backup contains an unsafe path.')
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            copied = 0
            with archive.open(info) as incoming, open(target, 'wb') as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                    copied += len(chunk)
                    streamed_total += len(chunk)
                    if streamed_total > MAX_EXPANDED_BYTES:
                        raise Rejected('The backup expands beyond the allowed size.')
                    digest.update(chunk)
                    outgoing.write(chunk)
            expected_hash = hashes.get(str(member))
            if expected_hash and digest.hexdigest() != expected_hash:
                raise Rejected(f'The backup file hash does not match: {member}.')
            files.append(str(member))
    return files, manifest


def _safe_member_name(value: str) -> str:
    """Normalize a ZIP member as a relative POSIX path or reject it."""
    if not isinstance(value, str) or not value or '\x00' in value:
        raise Rejected('The backup contains an unsafe path.')
    normalized = value.replace('\\', '/')
    path = PurePosixPath(normalized)
    if (normalized.startswith('/') or path.is_absolute() or PureWindowsPath(normalized).is_absolute()
            or PureWindowsPath(normalized).drive or any(part in ('', '.', '..') for part in path.parts)):
        raise Rejected('The backup contains an unsafe path.')
    return str(path)
