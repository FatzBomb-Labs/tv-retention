#!/usr/bin/env python3
"""Build the installable Unraid plugin.

Produces two things:

  dist/tv-delete-<version>-noarch-1.txz   the Slackware package emhttp installs
  install/tv-delete.plg                   a self-contained plugin manifest with the
                                          package embedded, so the WebGUI's
                                          "Install Plugin" field needs one file only

Nothing here contacts a server, installs anything, or touches /boot. Paths resolve
relative to this repository so the checkout can live anywhere.
"""
from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = 'tv-delete'
VERSION = (ROOT / 'VERSION').read_text().strip()
PACKAGE = f'{NAME}-{VERSION}-noarch-1.txz'
SOURCE = ROOT / 'src' / NAME
DIST = ROOT / 'dist'
INSTALL = ROOT / 'install'
BOOT = f'/boot/config/plugins/{NAME}'
CHANGES = (f'{VERSION}: First release. Per-show retention by days, episodes, or seasons, '
           'set directly or through named presets; mandatory Sonarr matching across '
           'multiple instances with auto-detected root path mapping; optional re-monitoring '
           'when a rule is widened; scheduled runs; dry run; deletion guards; optional TMDB '
           'air dates; run journal and history.')


def build_package() -> str:
    DIST.mkdir(exist_ok=True)
    target = DIST / PACKAGE
    with tarfile.open(target, 'w:xz') as archive:
        # The page reads this to cache-bust its assets after an upgrade.
        version_info = tarfile.TarInfo(f'usr/local/emhttp/plugins/{NAME}/VERSION')
        version_data = f'{VERSION}\n'.encode()
        version_info.size = len(version_data)
        version_info.mtime = 0
        version_info.uid = version_info.gid = 0
        version_info.mode = 0o644
        archive.addfile(version_info, io.BytesIO(version_data))
        for path in sorted(SOURCE.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            relative = path.relative_to(SOURCE)
            info = tarfile.TarInfo(str(Path('usr/local/emhttp/plugins') / NAME / relative))
            data = path.read_bytes()
            info.size = len(data)
            # A fixed mtime and owner keep rebuilds byte-identical for a given source tree.
            info.mtime = 0
            info.uid = info.gid = 0
            info.mode = 0o755 if relative.parts[0] == 'event' or path.suffix == '.sh' else 0o644
            archive.addfile(info, io.BytesIO(data))
        # Slackware expects an 11-line slack-desc; the first line names the package.
        description = (f'{NAME}: TV Delete (Unraid plugin)\n'
                       f'{NAME}:\n'
                       f'{NAME}: Retention for TV libraries. Keeps a chosen number of days,\n'
                       f'{NAME}: episodes, or seasons per show and removes the rest through\n'
                       f'{NAME}: Sonarr, so Sonarr stays in sync and episodes are unmonitored.\n'
                       + f'{NAME}:\n' * 6).encode()
        info = tarfile.TarInfo('install/slack-desc')
        info.size = len(description)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(description))
    return hashlib.sha256(target.read_bytes()).hexdigest()


def build_manifest(checksum: str) -> Path:
    plugin = ET.Element('PLUGIN', dict(name=NAME, author='FatzServer', version=VERSION,
                                       launch='Tools/TVDelete', min='7.0.0'))
    ET.SubElement(plugin, 'CHANGES').text = CHANGES

    payload = ET.SubElement(plugin, 'FILE', Name=f'{BOOT}/{PACKAGE}', Type='base64')
    ET.SubElement(payload, 'INLINE').text = base64.b64encode((DIST / PACKAGE).read_bytes()).decode('ascii')
    ET.SubElement(payload, 'SHA256').text = checksum

    install = ET.SubElement(plugin, 'FILE', Run='/bin/bash')
    ET.SubElement(install, 'INLINE').text = f'''
set -eu
test -x /usr/bin/python3 || {{ echo 'Python 3.9+ is required at /usr/bin/python3.'; exit 1; }}
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
test -f {BOOT}/{PACKAGE} || {{ echo 'Package missing at {BOOT}/{PACKAGE}.'; exit 1; }}
echo '{checksum}  {BOOT}/{PACKAGE}' | sha256sum -c -
upgradepkg --install-new --reinstall {BOOT}/{PACKAGE}
chmod 755 /usr/local/emhttp/plugins/{NAME}/event/*
# Republish the schedule from the saved settings; a fresh install has none, and an
# upgrade keeps whatever the operator had configured.
/usr/bin/python3 /usr/local/emhttp/plugins/{NAME}/worker/main.py resume || true
echo 'TV Delete installed. Open Tools > TV Delete. Dry run is ON and no schedule is set.'
'''

    remove = ET.SubElement(plugin, 'FILE', Run='/bin/bash', Method='remove')
    ET.SubElement(remove, 'INLINE').text = f'''
set -eu
# Only the plugin's own cron entry and package are removed. Settings, journals and
# history are left in place so a reinstall picks up where it left off.
if [ -f {BOOT}/schedule.cron ]; then unlink {BOOT}/schedule.cron; fi
/usr/local/sbin/update_cron
removepkg {NAME}
echo 'TV Delete removed. Settings and run journals were preserved.'
'''

    ET.indent(plugin)
    manifest = DIST / f'{NAME}.plg'
    ET.ElementTree(plugin).write(manifest, encoding='utf-8', xml_declaration=True)
    return manifest


def main() -> None:
    checksum = build_package()
    manifest = build_manifest(checksum)
    (DIST / 'SHA256SUMS').write_text(''.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
        for path in (DIST / PACKAGE, manifest)))
    INSTALL.mkdir(exist_ok=True)
    (INSTALL / manifest.name).write_bytes(manifest.read_bytes())
    (INSTALL / 'SHA256SUMS').write_text(
        f'{hashlib.sha256(manifest.read_bytes()).hexdigest()}  {manifest.name}\n')
    print(f'Built {DIST / PACKAGE}')
    print(f'Built {manifest}')
    print(f'Installable manifest: {INSTALL / manifest.name}')
    print(f'Package SHA256 {checksum}')


if __name__ == '__main__':
    main()
