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
        self.assertIn("usable: (entry) => entry.selectable", self.js)
        self.assertIn("cannot be used", self.js)


    def test_series_problems_surface_on_the_card(self):
        # A badge only when something needs attention, and the fixes live behind it.
        self.assertIn("function alertBadge", self.js)
        self.assertIn("function showSeriesAlerts", self.js)
        self.assertRegex(self.js, r"head\.append\(enableToggle\(rule\)\)")

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
        # Prove the scan is actually finding constants rather than passing on an empty set.
        self.assertIn('DEFAULT_TIMEOUT', declared, 'the test must be seeing real constants')

    def test_blocking_problems_are_visibly_different_from_advisory_ones(self):
        css = (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text()
        self.assertIn('.tvd-dot-badge.error', css)
        self.assertIn('.tvd-dot-badge.warning', css)
        self.assertIn('.tvd-dot-badge.blocked', css)
        self.assertIn('blocked', self.js)

    def test_the_series_badge_is_a_count_left_of_the_title(self):
        # A circle carrying a number, before the name — not a pill competing with it.
        self.assertRegex(self.js, r'alertBadge\(rule\),\s*\n\s*el\(.span., \{ className: .tvd-rule-title')
        self.assertIn('tvd-dot-badge', self.js)

    def test_removing_a_series_is_queued_and_asks_for_the_right_word(self):
        """Two different consequences, two different words, and neither happens at once.

        The plugin never deletes a series itself: the destructive options ask Sonarr to,
        so Sonarr's own recycle bin and bookkeeping apply.
        """
        self.assertIn("'delete-series': 'DELETE'", self.js)
        self.assertIn("'delete-series-files': 'DELETE ALL'", self.js)
        self.assertIn('Queue removal', self.js)
        self.assertIn('function queuedCard', self.js)
        self.assertRegex(self.js, r'Ask Sonarr to delete the series')

    def test_nothing_on_a_card_acts_immediately(self):
        # Every destructive control queues; only a run applies. Undo is the safety net.
        self.assertNotRegex(self.js, r"api\('remove-series'")
        self.assertIn('Undo', self.js)

    def test_test_mode_governs_the_scheduler_only(self):
        # A manual run is always live, so the confirmation has to say so when Test Mode is
        # on — that is exactly when someone would assume otherwise.
        self.assertIn('tvd-test-mode', self.js)
        self.assertRegex(self.js, r'Test mode is active on the scheduler')
        self.assertNotIn("$('tvd-preview')", self.js)

    def test_the_run_button_hides_only_on_a_complete_answer(self):
        # Hiding it on a stale or partial reading would be a promise the cache cannot keep.
        self.assertRegex(self.js, r'plan\.trustworthy && !plan\.actionable')


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

    def test_a_series_can_be_enabled_from_its_card(self):
        # The most frequent change to a rule should not require opening the editor.
        self.assertIn("function enableToggle", self.js)
        self.assertIn("tvd-switch", (ROOT / "src" / "tv-delete" / "assets" / "app.css").read_text())

    def test_a_refused_toggle_is_reverted(self):
        # Leaving the switch showing a state the backend rejected would be a lie.
        self.assertRegex(self.js, r'target\.enabled = !wanted')

    def test_badge_styling_outranks_the_generic_button_rule(self):
        """`#tv-delete button` outranks a bare class, which is not obvious and bit once.

        The badge is a button, so every property that shapes it — padding, radius, size —
        has to be written under #tv-delete or the generic rule wins and it renders as a
        grey rectangle with the number pushed off centre.
        """
        css = (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text()
        import re
        shaping = re.search(r'#tv-delete \.tvd-dot-badge[^{]*\{([^}]*)\}', css)
        self.assertIsNotNone(shaping, 'the badge must be styled under #tv-delete')
        for property_name in ('padding', 'border-radius', 'width', 'height'):
            self.assertIn(property_name, shaping.group(1))
        # A bare `.tvd-dot-badge {` rule would silently lose to the generic button rule.
        self.assertNotRegex(css, r'(?m)^\.tvd-dot-badge\s*\{')

    def test_severity_is_carried_by_the_frame_not_a_colour_wash(self):
        css = (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text()
        import re
        for match in re.finditer(r'\.tvd-alert-card\.(error|warning|notice)[^{]*\{([^}]*)\}', css):
            self.assertNotIn('background', match.group(2),
                             'an alert card must not be tinted end to end')

    def test_alerts_are_grouped_by_series(self):
        # The series is what you act on, so it owns the card and its problems are lines.
        self.assertIn('function seriesAlertCard', self.js)
        self.assertIn('ALERT_TAG', self.js)
        self.assertNotIn('function alertRow', self.js)

    def test_no_decorative_glyph_is_used_as_a_label(self):
        """A glyph renders at whatever size and baseline the platform picks.

        A bare "!" in a badge and an emoji before every folder name were sized by the font
        rather than by the rule meant to shape them, which is what made those rows look
        wrong. Words, or a count, behave predictably. The "?" hint is exempt: it is a
        control with its own size, radius and font-size written under #tv-delete.
        """
        # The severity marks are exempt: geometric characters with no emoji presentation,
        # in a span with an explicit width and font-size.
        for glyph in ('📁', '⬑', '✓', '✗', '▾', '★', '⚠'):
            self.assertNotIn(glyph, self.js, f'{glyph} renders unpredictably; use a word')
        self.assertNotRegex(self.js, r"textContent: '!'", 'a bare "!" badge; use a count')
        self.assertNotRegex(self.js, r"textContent: '…'", 'a lone ellipsis label; name the action')

    def test_system_alerts_are_grouped_like_series_ones(self):
        self.assertRegex(self.js, r'function systemAlertCard\(instanceName, list\)')
        self.assertIn('byInstance', self.js)

    def test_tab_styling_outranks_the_generic_button_rule(self):
        """Same trap as the badge: a bare `.tvd-tabs button` loses to `#tv-delete button`.

        The active underline was drawn on an element that also had the generic 1px box
        border, so nothing looked selected.
        """
        css = (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text()
        self.assertNotRegex(css, r'(?m)^\.tvd-tabs button')
        self.assertRegex(css, r'#tv-delete \.tvd-tabs button\.active[^{]*\{[^}]*border-bottom')

    def test_a_fix_is_presented_as_an_action(self):
        self.assertIn('Quick action: ', self.js)
        css = (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text()
        self.assertRegex(css, r'#tv-delete button\.tvd-action[^{]*\{[^}]*--tvd-action')

    def test_only_the_mark_and_the_card_edge_carry_severity(self):
        css = (ROOT / 'src' / 'tv-delete' / 'assets' / 'app.css').read_text()
        import re
        # A tag tinted per severity is what made a page of warnings read as solid orange.
        for severity in ('error', 'warning', 'notice'):
            self.assertNotRegex(css, rf'\.tvd-tag\.{severity}\s*\{{')
        self.assertRegex(css, r'\.tvd-sev\.warning\s*\{[^}]*background')

    def test_delete_lives_in_the_editor_action_row(self):
        # Bottom left, beside Cancel and Save — not on the card, where it invites a slip.
        self.assertIn("tvd-dialog-extra", self.js)
        self.assertRegex(self.js, r"textContent: 'Delete…'")
        self.assertIn('tvd-dialog-extra', self.html)

    def test_the_card_offers_no_destructive_button(self):
        import re
        card = re.search(r"const actions = el\('div', \{ className: 'tvd-rule-actions' \}\);(.*?)"
                         r"body\.append\(actions\)", self.js, re.S)
        self.assertIsNotNone(card)
        for word in ('Remove', 'Delete'):
            self.assertNotIn(f"textContent: '{word}", card.group(1))

    def test_the_change_filter_is_passed_not_captured(self):
        """`shows` once leaked out of its closure and would have thrown at runtime.

        The shared renderer now takes it as a parameter, so it cannot be referenced where
        it does not exist — this asserts it stays a parameter rather than a free variable.
        """
        self.assertRegex(self.js, r'function changeRows\(rule, shows\)')
        self.assertRegex(self.js, r'changeRows\(rule, \(\) => true\)')
        self.assertRegex(self.js, r'changeRows\(rule, shows\)')

    def test_one_component_describes_every_change(self):
        # Four places used to render changes; adding a kind meant editing all four.
        for name in ('function changeSummary', 'function changeLines', 'function changeRows'):
            self.assertIn(name, self.js)
        self.assertNotIn('function planLines', self.js)

    def test_series_is_its_own_plural(self):
        """`plural(3, 'series')` produced "3 seriess" everywhere it was used."""
        self.assertRegex(self.js, r"const plural = .*endsWith\('s'\)")
        self.assertNotRegex(self.js, r"\$\{count\} \$\{word\}\$\{count === 1 \? '' : 's'\}")

    def test_series_problems_are_counted_on_the_series_tab_only(self):
        # The roll-up sits on the tab that acts on it, and the Alerts tab counts only
        # what is wrong with the installation.
        panels = self.html.split('id="tvd-panel-')
        series_panel = next(part for part in panels if part.startswith('series"'))
        alerts_panel = next(part for part in panels if part.startswith('alerts"'))
        self.assertIn('id="tvd-series-rollup"', series_panel)
        self.assertNotIn('rollup', alerts_panel)
        self.assertIn('id="tvd-series-badge"', self.html)
        self.assertIn("setBadge($('tvd-series-badge'), seriesList)", self.js)
        self.assertIn("setBadge($('tvd-tab-badge'), systemAlerts)", self.js)

    def test_a_sweep_clears_each_plan_but_keeps_the_series(self):
        # The series are not what is being re-read; their plans are. A plan left standing
        # during the read is a stale reading shown as a current one — but emptying the
        # whole list to say so throws away the page.
        self.assertIn('bulkChecking', self.js)
        self.assertIn('if (isChecking(rule.id) || bulkChecking) {', self.js)
        self.assertIn('bulkChecking = false;', self.js)
        self.assertNotRegex(self.js, r'if \(bulkChecking\) \{\s*\n\s*\$\(.tvd-rules-empty.\)')

    def test_buttons_do_not_inherit_the_font_shorthand(self):
        """`font: inherit` also sets line-height, and outranks any class that sets it.

        At `#tv-delete button` it is (1,0,1), so every dense list built from buttons —
        the scheduled-change lines, the series jump list — silently reverted to the
        page's paragraph spacing however tight the component's own rule was.
        """
        self.assertNotRegex(self.css, r'#tv-delete button \{[^}]*font:\s*inherit')
        self.assertRegex(self.css, r'#tv-delete button \{[^}]*font-family:\s*inherit')

    def test_the_change_view_offers_a_list_of_the_series_it_covers(self):
        self.assertIn('tvd-change-nav', self.js)
        self.assertIn('scrollIntoView', self.js)
        self.assertIn('.tvd-change-view', self.css)
        self.assertIn('.tvd-change-jump', self.css)

    def test_the_refresh_control_sits_beside_the_run_button(self):
        head = self.html.split('</header>')[0]
        self.assertLess(head.index('tvd-refresh-all'), head.index('tvd-run'))
        self.assertIn('.tvd-head-run', self.css)
        self.assertNotIn('tvd-head-plan', self.html)
