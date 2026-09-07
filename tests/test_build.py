import hashlib
import subprocess
import sys
import tarfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import context  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, str(ROOT / 'tools' / 'build.py')], check=True,
                       stdout=subprocess.DEVNULL)
        cls.version = (ROOT / 'VERSION').read_text().strip()
        cls.package = ROOT / 'dist' / f'tv-delete-{cls.version}-noarch-1.txz'
        cls.manifest = ROOT / 'install' / 'tv-delete.plg'

    def test_artifacts_exist(self):
        self.assertTrue(self.package.is_file())
        self.assertTrue(self.manifest.is_file())

    def test_package_installs_under_the_plugin_path(self):
        with tarfile.open(self.package) as archive:
            names = archive.getnames()
        self.assertIn('usr/local/emhttp/plugins/tv-delete/worker/main.py', names)
        self.assertIn('usr/local/emhttp/plugins/tv-delete/TVDelete.page', names)
        self.assertIn('install/slack-desc', names)
        self.assertFalse([name for name in names if '__pycache__' in name])

    def test_event_scripts_are_executable(self):
        with tarfile.open(self.package) as archive:
            member = archive.getmember('usr/local/emhttp/plugins/tv-delete/event/disks_mounted')
        self.assertEqual(member.mode, 0o755)

    def test_manifest_declares_the_package_checksum(self):
        tree = ET.parse(self.manifest)
        checksum = tree.getroot().findtext('.//SHA256').strip()
        self.assertEqual(checksum, hashlib.sha256(self.package.read_bytes()).hexdigest())

    def test_manifest_launches_the_tools_page(self):
        root = ET.parse(self.manifest).getroot()
        self.assertEqual(root.get('launch'), 'Tools/TVDelete')
        self.assertEqual(root.get('version'), self.version)

    def test_removal_preserves_settings(self):
        text = self.manifest.read_text()
        self.assertIn('removepkg tv-delete', text)
        self.assertNotIn('rm -rf /boot/config/plugins/tv-delete', text)

    def test_rebuild_is_reproducible(self):
        first = hashlib.sha256(self.package.read_bytes()).hexdigest()
        subprocess.run([sys.executable, str(ROOT / 'tools' / 'build.py')], check=True,
                       stdout=subprocess.DEVNULL)
        self.assertEqual(first, hashlib.sha256(self.package.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
