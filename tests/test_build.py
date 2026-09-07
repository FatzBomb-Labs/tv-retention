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


class Interface(unittest.TestCase):
    """Guards against the class of bug where an author `display` rule defeats `hidden`."""

    @classmethod
    def setUpClass(cls):
        source = ROOT / 'src' / 'tv-delete'
        cls.css = (source / 'assets' / 'app.css').read_text()
        cls.js = (source / 'assets' / 'app.js').read_text()
        cls.html = (source / 'include' / 'interface.html').read_text()

    def test_the_hidden_attribute_is_forced_to_win(self):
        self.assertRegex(self.css, r'#tv-delete \[hidden\][^{]*\{[^}]*display:\s*none\s*!important')

    def test_every_element_the_script_hides_exists_in_the_markup(self):
        import re
        for identifier in set(re.findall(r"\$\('([a-z0-9-]+)'\)\.hidden", self.js)):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is toggled but not in the markup')

    def test_the_busy_overlay_starts_hidden(self):
        self.assertRegex(self.html, r'id="tvd-busy"[^>]*hidden')

    def test_assets_are_cache_busted(self):
        page = (ROOT / 'src' / 'tv-delete' / 'TVDelete.page').read_text()
        self.assertIn('app.css?v=', page)
        self.assertIn('app.js?v=', page)

    def test_the_package_ships_a_version_file(self):
        version = (ROOT / 'VERSION').read_text().strip()
        package = ROOT / 'dist' / f'tv-delete-{version}-noarch-1.txz'
        with tarfile.open(package) as archive:
            self.assertIn('usr/local/emhttp/plugins/tv-delete/VERSION', archive.getnames())

    def test_every_element_the_script_addresses_exists_in_the_markup(self):
        import re
        for identifier in sorted(set(re.findall(r"\$\('([a-z0-9-]+)'\)", self.js))):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is addressed but not in the markup')

    def test_requests_cannot_hang_forever(self):
        self.assertIn('AbortController', self.js)
        self.assertIn('DEFAULT_TIMEOUT', self.js)

    def test_a_failed_start_clears_the_overlay(self):
        self.assertRegex(self.js, r"refresh\(\)\.catch")
