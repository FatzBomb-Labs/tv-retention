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
from pathlib import Path, PurePosixPath, PureWindowsPath

from core import Rejected
from store import CONFIG

PREFIX = 'tv-retention-'
SUFFIX = '.zip'


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
        # Temporary files are never a useful restore point and can contain half-written
        # JSON from a process that was interrupted.
        if path.name.startswith('tv-retention-') and path.suffix in ('.tmp', '.part'):
            continue
        found.append((path, str(path.relative_to(source)).replace(os.sep, '/')))
    return sorted(found, key=lambda item: item[1])


def create(settings: dict) -> dict:
    """Create an atomic timestamped zip and return only non-sensitive metadata."""
    dest = destination(settings)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if not os.access(dest, os.W_OK):
            raise OSError('destination is not writable')
    except OSError as error:
        raise Rejected(f'Backup destination is not writable ({error})') from error
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    # A manual click twice in one second must still leave two restore points. Keep the
    # human-readable timestamp, adding a monotonic suffix only when that name exists.
    final = dest / f'{PREFIX}{stamp}{SUFFIX}'
    suffix = 0
    while final.exists():
        suffix += 1
        final = dest / f'{PREFIX}{stamp}-{suffix}{SUFFIX}'
    temporary = dest / f'.{final.name}.{os.getpid()}.tmp'
    files = _files(settings, dest)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = {'format': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                        'files': [name for _, name in files],
                        'contains_credentials': True}
            archive.writestr('manifest.json', json.dumps(manifest, indent=2) + '\n')
            for path, name in files:
                archive.write(path, name)
        os.replace(temporary, final)
    except (OSError, zipfile.BadZipFile) as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
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
    if confirm != 'RESTORE':
        raise Rejected('Type RESTORE to confirm replacing the saved application files.')
    dest = destination(settings)
    name = Path(str(filename or '')).name
    if name != filename or not name.startswith(PREFIX) or not name.endswith(SUFFIX):
        raise Rejected('Choose a TV Retention backup archive.')
    archive_path = (dest / name).resolve()
    if not _inside(archive_path, dest) or not archive_path.is_file():
        raise Rejected('That backup archive does not exist.')
    source = _source_root()
    staging = Path(tempfile.mkdtemp(prefix='.tv-retention-restore-', dir=str(source.parent)))
    files = []
    try:
        with zipfile.ZipFile(archive_path, 'r') as archive:
            # Archives live in a shared, operator-selected directory. Require the
            # manifest written by `create` and make its file list authoritative, so a
            # renamed or hand-built ZIP cannot smuggle an unexpected file into /config.
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
            if not isinstance(declared, list) or any(not isinstance(name, str) for name in declared):
                raise Rejected('The backup manifest has no valid file list.')
            declared_names = {_safe_member_name(name) for name in declared}
            if len(declared_names) != len(declared) or 'manifest.json' in declared_names:
                raise Rejected('The backup manifest contains an unsafe or duplicate path.')
            members = []
            for info in archive.infolist():
                if info.filename.replace('\\', '/') == 'manifest.json':
                    continue
                if info.is_dir():
                    continue
                if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                    raise Rejected('The backup contains a symbolic link.')
                name = _safe_member_name(info.filename)
                members.append(name)
            if len(set(members)) != len(members) or set(members) != declared_names:
                raise Rejected('The backup manifest does not match its contents.')
            for info in archive.infolist():
                if info.filename.replace('\\', '/') == 'manifest.json' or info.is_dir():
                    continue
                member = PurePosixPath(_safe_member_name(info.filename))
                target = (staging / Path(*member.parts)).resolve()
                if not _inside(target, staging):
                    raise Rejected('The backup contains an unsafe path.')
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as incoming, open(target, 'wb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                files.append(str(member))
        for relative in files:
            target = (source / relative).resolve()
            if not _inside(target, source) or _inside(target, dest):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / relative, target)
    except (OSError, zipfile.BadZipFile) as error:
        raise Rejected(f'Restore failed ({error})') from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {'file': name, 'restored_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'files': len(files), 'requires_reload': True}


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
