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

    def test_the_cache_key_comes_from_asset_contents(self):
        """A timestamp-based key is worthless here.

        The package ships every file with mtime 0 to keep builds reproducible, so a key
        built from filemtime() is the same string for every release: after an upgrade the
        browser keeps serving the previous script from cache. That presented as the whole
        configuration vanishing, since a stale script cannot render the new data.
        """
        page = (ROOT / 'src' / 'tv-delete' / 'TVDelete.page').read_text()
        self.assertIn('md5_file', page)
        self.assertNotIn('filemtime', page)
        self.assertIn('app.js?v=', page)
        self.assertIn('app.css?v=', page)

    def test_the_package_ships_reproducible_timestamps(self):
        # The reason the key cannot use mtime; asserted so the two stay consistent.
        version = (ROOT / 'VERSION').read_text().strip()
        with tarfile.open(ROOT / 'dist' / f'tv-delete-{version}-noarch-1.txz') as archive:
            self.assertTrue(all(member.mtime == 0 for member in archive.getmembers()))

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

    def test_the_package_ships_a_readme_for_the_plugins_page(self):
        # Unraid renders plugins/<name>/README.md as the description on the Plugins page,
        # falling back to the bare slug when it is absent.
        with tarfile.open(ROOT / 'dist' / f'tv-delete-{(ROOT / "VERSION").read_text().strip()}-noarch-1.txz') as archive:
            self.assertIn('usr/local/emhttp/plugins/tv-delete/README.md', archive.getnames())
        readme = (ROOT / 'src' / 'tv-delete' / 'README.md').read_text()
        self.assertTrue(readme.lstrip().startswith('**TV Delete**'), 'the description must lead with the display name')

    def test_the_manifest_declares_an_icon(self):
        import xml.etree.ElementTree as ElementTree
        root = ElementTree.parse(ROOT / 'install' / 'tv-delete.plg').getroot()
        self.assertTrue(root.get('icon'))

    def test_the_picker_refuses_unselectable_series(self):
        self.assertIn('!entry.selectable', self.js)
        self.assertIn('cannot be given a rule', self.js)


    def test_the_monitoring_pill_sits_with_the_title(self):
        self.assertRegex(self.js, r'head\.append\(monitorPill\(rule\)\)')

    def test_the_pill_opens_a_menu_built_from_its_state(self):
        self.assertIn('aria-haspopup', self.js)
        self.assertIn('function monitorMenu', self.js)
        # An aligned show must not be offered a correction that would write nothing.
        self.assertRegex(self.js, r'if \(inside\) \{')
        self.assertRegex(self.js, r'if \(outside\) \{')

    def test_the_script_is_not_prefixed_by_a_stray_fragment(self):
        # A build-time edit once prepended a fragment above the opening comment, which
        # broke the whole file. The header is cheap to assert and would have caught it.
        self.assertTrue(self.js.lstrip().startswith('/* TV Delete web UI.'))
        self.assertEqual(self.js.count("function render() {"), 1)

    def test_braces_and_parentheses_balance(self):
        for pair in ('{}', '()', '[]'):
            self.assertEqual(self.js.count(pair[0]), self.js.count(pair[1]),
                             f'unbalanced {pair} in app.js')

    def test_the_manual_check_button_is_gone(self):
        # The pill reads from the cache; the operator should never have to ask it to look.
        self.assertNotIn('tvd-check-monitoring', self.js)
        self.assertNotIn('tvd-check-monitoring', self.html)

    def test_checks_never_block_the_page(self):
        # Background reads pass quiet, so the busy overlay is not raised for them.
        self.assertRegex(self.js, r"api\('check-rule'[^)]*, true\)")
        self.assertRegex(self.js, r"api\('progress'[^)]*, true\)")
        self.assertIn('function queueChecks', self.js)

    def test_a_show_being_read_is_not_editable(self):
        self.assertIn('isChecking(rule.id)', self.js)
        self.assertRegex(self.js, r'button\.disabled = true')

    def test_a_background_sweep_is_watched_not_duplicated(self):
        self.assertIn('startPolling', self.js)
        self.assertIn('data.busy', self.js)

    def test_every_module_constant_used_is_declared(self):
        """Catch a constant left behind when an edit replaced the block that declared it.

        A syntax check cannot see this: `PILL_ENDED is not defined` is a runtime error, and
        it broke the whole page once because a batched edit dropped the declaration while
        leaving two uses behind. Only SCREAMING_SNAKE names are considered — that is the
        shape every constant in this file has, and it keeps prose like "TVDB" or "HTTP"
        out of the comparison without needing an allowlist to be maintained.
        """
        import re
        code = re.sub(r'/\*.*?\*/', ' ', self.js, flags=re.S)
        code = re.sub(r'//[^\n]*', ' ', code)
        # Template literals first: they nest the other quote styles inside ${...}, so
        # stripping the plain quotes first would eat across their boundaries.
        for quote in ('`', '"', "'"):
            code = re.sub(quote + r'(?:\\.|[^' + quote + r'\\])*' + quote, ' ', code, flags=re.S)
        # Not preceded by a dot: Number.MAX_SAFE_INTEGER is a property, not a module constant.
        shape = r'(?<![.\w])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b'
        declared = set(re.findall(r'\b(?:const|let|var)\s+' + shape, code))
        used = set(re.findall(shape, code))
        missing = sorted(used - declared)
        self.assertEqual(missing, [], f'used but never declared in app.js: {missing}')
        self.assertIn('PILL_ENDED', declared, 'the test must be seeing real constants')

    def test_an_ended_show_still_keeping_episodes_reads_differently(self):
        # Same options, deliberately not the same colour or wording.
        self.assertIn("PILL_ENDED_SPENT", self.js)
        self.assertRegex(self.js, r"lifecycle === 'ended'\) kind = 'ended'")
        self.assertIn('tvd-pill.ended', (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text())

    def test_the_icon_is_a_font_awesome_name(self):
        """A name is not enough: it has to resolve to a glyph.

        Two icons were shipped that rendered as nothing. icon-trash is absent from Unraid's
        font entirely; icon-bin appears in a stylesheet but has no `:before{content}` rule,
        which looks identical in a grep and identical on screen. Font Awesome is loaded on
        every Unraid page and is what twelve other plugins on this server use, so the icon
        is required to be a plain FA name. tools/check-on-host.sh confirms the glyph exists.
        """
        import xml.etree.ElementTree as ElementTree
        manifest_icon = ElementTree.parse(ROOT / 'install' / 'tv-delete.plg').getroot().get('icon')
        page = (ROOT / 'src' / 'tv-delete' / 'TVDelete.page').read_text()
        page_icon = next(line.split('=', 1)[1].strip().strip('"')
                         for line in page.splitlines() if line.startswith('Icon='))
        self.assertEqual(manifest_icon, page_icon, 'the Tools tile and Plugins row must agree')
        self.assertFalse(manifest_icon.startswith('icon-'),
                         'Unraid font names have silently rendered empty; use a Font Awesome name')
        self.assertFalse(manifest_icon.endswith('.png'), 'no image is shipped with this plugin')
        self.assertRegex(manifest_icon, r'^[a-z0-9-]+$')

    def test_a_show_can_be_enabled_from_its_card(self):
        # The most frequent change to a rule should not require opening the editor.
        self.assertIn('function enableToggle', self.js)
        self.assertRegex(self.js, r'head\.append\(enableToggle\(rule\)\)')
        self.assertIn('tvd-toggle', (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text())

    def test_a_refused_toggle_is_reverted(self):
        # Leaving the switch showing a state the backend rejected would be a lie.
        self.assertRegex(self.js, r'target\.enabled = !wanted')
