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
        cls.package = ROOT / 'dist' / f'tv-retention-{cls.version}-noarch-1.txz'
        cls.manifest = ROOT / 'install' / 'tv-retention.plg'

    def test_artifacts_exist(self):
        self.assertTrue(self.package.is_file())
        self.assertTrue(self.manifest.is_file())

    def test_package_installs_under_the_plugin_path(self):
        with tarfile.open(self.package) as archive:
            names = archive.getnames()
        self.assertIn('usr/local/emhttp/plugins/tv-retention/worker/main.py', names)
        self.assertIn('usr/local/emhttp/plugins/tv-retention/TVRetention.page', names)
        self.assertIn('install/slack-desc', names)
        self.assertFalse([name for name in names if '__pycache__' in name])

    def test_event_scripts_are_executable(self):
        with tarfile.open(self.package) as archive:
            member = archive.getmember('usr/local/emhttp/plugins/tv-retention/event/disks_mounted')
        self.assertEqual(member.mode, 0o755)

    def test_manifest_declares_the_package_checksum(self):
        tree = ET.parse(self.manifest)
        checksum = tree.getroot().findtext('.//SHA256').strip()
        self.assertEqual(checksum, hashlib.sha256(self.package.read_bytes()).hexdigest())

    def test_manifest_launches_the_tools_page(self):
        root = ET.parse(self.manifest).getroot()
        self.assertEqual(root.get('launch'), 'Tools/TVRetention')
        self.assertEqual(root.get('version'), self.version)

    def test_removal_preserves_settings(self):
        text = self.manifest.read_text()
        self.assertIn('removepkg tv-retention', text)
        self.assertNotIn('rm -rf /boot/config/plugins/tv-retention', text)

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
        source = ROOT / 'src' / 'tv-retention'
        cls.css = (source / 'assets' / 'app.css').read_text()
        cls.js = (source / 'assets' / 'app.js').read_text()
        cls.html = (source / 'include' / 'interface.html').read_text()

    def test_the_hidden_attribute_is_forced_to_win(self):
        self.assertRegex(self.css, r'#tv-retention \[hidden\][^{]*\{[^}]*display:\s*none\s*!important')

    def test_every_element_the_script_hides_exists_in_the_markup(self):
        import re
        for identifier in set(re.findall(r"\$\('([a-z0-9-]+)'\)\.hidden", self.js)):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is toggled but not in the markup')

    def test_the_busy_overlay_starts_hidden(self):
        self.assertRegex(self.html, r'id="tvr-busy"[^>]*hidden')

    def test_the_cache_key_comes_from_asset_contents(self):
        """A timestamp-based key is worthless here.

        The package ships every file with mtime 0 to keep builds reproducible, so a key
        built from filemtime() is the same string for every release: after an upgrade the
        browser keeps serving the previous script from cache. That presented as the whole
        configuration vanishing, since a stale script cannot render the new data.
        """
        page = (ROOT / 'src' / 'tv-retention' / 'TVRetention.page').read_text()
        self.assertIn('md5_file', page)
        self.assertNotIn('filemtime', page)
        self.assertIn('app.js?v=', page)
        self.assertIn('app.css?v=', page)

    def test_the_package_ships_reproducible_timestamps(self):
        # The reason the key cannot use mtime; asserted so the two stay consistent.
        version = (ROOT / 'VERSION').read_text().strip()
        with tarfile.open(ROOT / 'dist' / f'tv-retention-{version}-noarch-1.txz') as archive:
            self.assertTrue(all(member.mtime == 0 for member in archive.getmembers()))

    def test_the_package_ships_a_version_file(self):
        version = (ROOT / 'VERSION').read_text().strip()
        package = ROOT / 'dist' / f'tv-retention-{version}-noarch-1.txz'
        with tarfile.open(package) as archive:
            self.assertIn('usr/local/emhttp/plugins/tv-retention/VERSION', archive.getnames())

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
        with tarfile.open(ROOT / 'dist' / f'tv-retention-{(ROOT / "VERSION").read_text().strip()}-noarch-1.txz') as archive:
            self.assertIn('usr/local/emhttp/plugins/tv-retention/README.md', archive.getnames())
        readme = (ROOT / 'src' / 'tv-retention' / 'README.md').read_text()
        self.assertTrue(readme.lstrip().startswith('**TV Retention**'), 'the description must lead with the display name')

    def test_the_manifest_declares_an_icon(self):
        import xml.etree.ElementTree as ElementTree
        root = ElementTree.parse(ROOT / 'install' / 'tv-retention.plg').getroot()
        self.assertTrue(root.get('icon'))

    def test_a_series_that_cannot_be_used_is_refused_on_save(self):
        """The picker is gone: the navigator says which series you mean.

        What it guarded still holds — a series Sonarr has no folder for cannot take a
        rule — so the check moved to the save, which is the only place it can be evaded.
        """
        self.assertIn('series.selectable === false', self.js)
        self.assertIn('cannot be used', self.js)
        self.assertNotIn('Refresh list', self.js)


    def test_series_problems_surface_on_the_card(self):
        # A badge only when something needs attention, and the fixes live behind it.
        self.assertIn("function alertBadge", self.js)
        self.assertIn("function showSeriesAlerts", self.js)
        self.assertRegex(self.js, r"head\.append\(enableToggle\(rule\)\)")

    def test_the_script_is_not_prefixed_by_a_stray_fragment(self):
        # A build-time edit once prepended a fragment above the opening comment, which
        # broke the whole file. The header is cheap to assert and would have caught it.
        self.assertTrue(self.js.lstrip().startswith('/* TV Retention web UI.'))
        self.assertEqual(self.js.count("function render() {"), 1)

    def test_braces_and_parentheses_balance(self):
        for pair in ('{}', '()', '[]'):
            self.assertEqual(self.js.count(pair[0]), self.js.count(pair[1]),
                             f'unbalanced {pair} in app.js')

    def test_the_manual_check_button_is_gone(self):
        # The pill reads from the cache; the operator should never have to ask it to look.
        self.assertNotIn('tvr-check-monitoring', self.js)
        self.assertNotIn('tvr-check-monitoring', self.html)

    def test_checks_never_block_the_page(self):
        # Background reads pass quiet, so the busy overlay is not raised for them.
        self.assertRegex(self.js, r"api\('check-rule',[^;]*, true\)")
        self.assertRegex(self.js, r"api\('progress'[^)]*, true\)")
        self.assertIn('function queueChecks', self.js)

    def test_a_show_being_read_says_so(self):
        # It is not hidden and not silently stale: the card stays, and its plan is replaced
        # by what is happening to it.
        self.assertIn('isChecking(rule.id)', self.js)
        self.assertRegex(self.js, r"isChecking\(rule\.id\)\) \{\s*\n\s*main\.append")

    def test_the_page_has_a_heartbeat_that_never_blocks_it(self):
        # It must not raise the busy overlay, must stand aside for a sweep, and must not
        # re-render on a timer for its own sake.
        self.assertRegex(self.js, r"api\('watch'[^;]*, true\)")
        self.assertIn('if (document.hidden || checkRunning || checkQueue.length || pollTimer) return;', self.js)
        self.assertIn('if (stamp === watchStamp) return;', self.js)

    def test_the_tick_asks_what_changed_whether_or_not_anyone_is_looking(self):
        # A problem the page discovers first is a notification that never fired.
        worker = ROOT / 'src' / 'tv-retention' / 'worker'
        tick = (worker / 'main.py').read_text().split('def tick()')[1].split('\ndef ')[0]
        self.assertIn('sync_from_sonarr', tick)
        self.assertIn("'watch': action_watch", (worker / 'actions.py').read_text())

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
        css = (ROOT / 'src' / 'tv-retention' / 'assets' / 'app.css').read_text()
        self.assertIn('.tvr-dot-badge.error', css)
        self.assertIn('.tvr-dot-badge.warning', css)
        self.assertIn('.tvr-dot-badge.blocked', css)
        self.assertIn('blocked', self.js)

    def test_the_series_badge_is_a_count_beside_the_title(self):
        # A circle carrying a number, next to the name — not a pill competing with it.
        self.assertIn('head.append(tick, alertBadge(rule));', self.js)
        self.assertIn('tvr-dot-badge', self.js)

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
        self.assertIn('tvr-test-mode', self.js)
        self.assertRegex(self.js, r'Test mode is active on the scheduler')
        self.assertNotIn("$('tvr-preview')", self.js)

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
        manifest_icon = ElementTree.parse(ROOT / 'install' / 'tv-retention.plg').getroot().get('icon')
        page = (ROOT / 'src' / 'tv-retention' / 'TVRetention.page').read_text()
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
        self.assertIn("tvr-switch", (ROOT / "src" / "tv-retention" / "assets" / "app.css").read_text())

    def test_a_refused_toggle_is_reverted(self):
        # Leaving the switch showing a state the backend rejected would be a lie.
        self.assertRegex(self.js, r'target\.enabled = !wanted')

    def test_badge_styling_outranks_the_generic_button_rule(self):
        """`#tv-retention button` outranks a bare class, which is not obvious and bit once.

        The badge is a button, so every property that shapes it — padding, radius, size —
        has to be written under #tv-retention or the generic rule wins and it renders as a
        grey rectangle with the number pushed off centre.
        """
        css = (ROOT / 'src' / 'tv-retention' / 'assets' / 'app.css').read_text()
        import re
        shaping = re.search(r'#tv-retention \.tvr-dot-badge[^{]*\{([^}]*)\}', css)
        self.assertIsNotNone(shaping, 'the badge must be styled under #tv-retention')
        for property_name in ('padding', 'border-radius', 'width', 'height'):
            self.assertIn(property_name, shaping.group(1))
        # A bare `.tvr-dot-badge {` rule would silently lose to the generic button rule.
        self.assertNotRegex(css, r'(?m)^\.tvr-dot-badge\s*\{')

    def test_severity_is_carried_by_the_frame_not_a_colour_wash(self):
        css = (ROOT / 'src' / 'tv-retention' / 'assets' / 'app.css').read_text()
        import re
        for match in re.finditer(r'\.tvr-alert-card\.(error|warning|notice)[^{]*\{([^}]*)\}', css):
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
        control with its own size, radius and font-size written under #tv-retention.
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
        """Same trap as the badge: a bare `.tvr-tabs button` loses to `#tv-retention button`.

        The active underline was drawn on an element that also had the generic 1px box
        border, so nothing looked selected.
        """
        css = (ROOT / 'src' / 'tv-retention' / 'assets' / 'app.css').read_text()
        self.assertNotRegex(css, r'(?m)^\.tvr-tabs button')
        self.assertRegex(css, r'#tv-retention \.tvr-tabs button\.active[^{]*\{[^}]*border-bottom')

    def test_a_fix_is_presented_as_an_action(self):
        self.assertIn('Quick action: ', self.js)
        css = (ROOT / 'src' / 'tv-retention' / 'assets' / 'app.css').read_text()
        self.assertRegex(css, r'#tv-retention button\.tvr-action[^{]*\{[^}]*--tvr-action')

    def test_only_the_mark_and_the_card_edge_carry_severity(self):
        css = (ROOT / 'src' / 'tv-retention' / 'assets' / 'app.css').read_text()
        import re
        # A tag tinted per severity is what made a page of warnings read as solid orange.
        for severity in ('error', 'warning', 'notice'):
            self.assertNotRegex(css, rf'\.tvr-tag\.{severity}\s*\{{')
        self.assertRegex(css, r'\.tvr-sev\.warning\s*\{[^}]*background')

    def test_delete_lives_in_the_editor_action_row(self):
        # Bottom left, beside Cancel and Save — not on the card, where it invites a slip.
        self.assertIn("tvr-dialog-extra", self.js)
        self.assertRegex(self.js, r"textContent: 'Delete…'")
        self.assertIn('tvr-dialog-extra', self.html)

    def test_the_card_offers_no_destructive_button(self):
        import re
        card = re.search(r"const actions = el\('div', \{ className: 'tvr-rule-actions' \}\);(.*?)"
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
        # A badge next to the thing it is about: series problems on the series items,
        # connection problems on Connections. Neither counts the other's.
        self.assertIn("setBadge($('tvr-badge-series-connected'), connectedAlerts)", self.js)
        self.assertIn("setBadge($('tvr-badge-media-connections'), instances)", self.js)
        for identifier in ('tvr-badge-series-all', 'tvr-badge-series-connected',
                           'tvr-badge-media-connections', 'tvr-alert-total'):
            self.assertIn(f'id="{identifier}"', self.html)

    def test_a_sweep_clears_each_plan_but_keeps_the_series(self):
        # The series are not what is being re-read; their plans are. A plan left standing
        # during the read is a stale reading shown as a current one — but emptying the
        # whole list to say so throws away the page.
        self.assertIn('bulkChecking', self.js)
        self.assertIn("textContent: 'Reading from Sonarr…'", self.js)
        self.assertIn('bulkChecking = false;', self.js)

    def test_buttons_do_not_inherit_the_font_shorthand(self):
        """`font: inherit` also sets line-height, and outranks any class that sets it.

        At `#tv-retention button` it is (1,0,1), so every dense list built from buttons —
        the scheduled-change lines, the series jump list — silently reverted to the
        page's paragraph spacing however tight the component's own rule was.
        """
        self.assertNotRegex(self.css, r'#tv-retention button \{[^}]*font:\s*inherit')
        self.assertRegex(self.css, r'#tv-retention button \{[^}]*font-family:\s*inherit')

    def test_the_change_view_offers_a_list_of_the_series_it_covers(self):
        self.assertIn('tvr-change-nav', self.js)
        self.assertIn('scrollIntoView', self.js)
        self.assertIn('.tvr-change-view', self.css)
        self.assertIn('.tvr-change-jump', self.css)

    def test_the_refresh_control_sits_beside_the_run_button(self):
        head = self.html.split('</header>')[0]
        self.assertLess(head.index('tvr-refresh-all'), head.index('tvr-run'))
        self.assertIn('.tvr-head-run', self.css)
        self.assertNotIn('tvr-head-plan', self.html)

    def test_the_schedule_saves_itself(self):
        """A switch that looks live and is not lost a schedule entirely.

        It was set, it read as set on every later visit because the page renders from the
        stored settings, and no run ever came — the value only left the page if you found
        a Save button below the fold.
        """
        self.assertIn('async function saveScheduleNow()', self.js)
        self.assertRegex(self.js, r"\$\(id\)\.addEventListener\('change', \(\) => guarded\('', saveScheduleNow\)\)")
        self.assertNotIn('tvr-save-schedule', self.html)
        self.assertNotIn('tvr-save-schedule', self.js)

    def test_a_form_that_keeps_an_explicit_save_says_when_it_is_dirty(self):
        self.assertIn('class="tvr-dirty tvr-dirty-mark"', self.html)
        self.assertIn('settingsDirty(true)', self.js)
        self.assertIn('settingsDirty(false)', self.js)

    def test_every_schedule_control_is_wired_to_save(self):
        # A control added to the panel and not to the list would silently not persist,
        # which is the whole bug repeating.
        import re
        wired = set(re.findall(r"'(tvr-(?:schedule-enabled|test-mode|freq|minute|hour|weekday|"
                               r"monthly-mode|monthly-day|monthly-weekday|cron|match-freq|"
                               r"match-hour|match-minute|connectivity))'", self.js))
        panel = self.html.split('id="tvr-view-media-schedule"')[1].split('</section>')[0]
        for identifier in re.findall(r'id="(tvr-[a-z-]+)"', panel):
            if identifier in ('tvr-schedule-summary', 'tvr-match-summary') or 'field' in identifier:
                continue
            self.assertIn(identifier, wired, f'{identifier} is on the schedule panel but never saved')

    def test_the_badge_is_as_tall_as_it_is_round(self):
        # Height came from line-height while width came from padding, so it rendered as a
        # squashed oval rather than a badge.
        self.assertRegex(self.css, r'\.tvr-tab-badge \{[^}]*height: 18px')
        self.assertRegex(self.css, r'\.tvr-tab-badge \{[^}]*min-width: 18px')

    def test_the_manifest_does_not_advertise_what_was_removed(self):
        """The first thing a new install prints has to be true.

        The changelog on the Plugins page still described path mapping, deletion guards,
        re-monitoring and dry run — four features removed across three versions — and the
        install message named a mode the plugin had stopped having.
        """
        import xml.etree.ElementTree as ElementTree
        root = ElementTree.parse(ROOT / 'install' / 'tv-retention.plg').getroot()
        # The changelog and the install message only — never the embedded package, or a
        # failure here would print a megabyte of base64 at whoever ran the tests.
        spoken = (root.findtext('CHANGES') or '')
        for node in root.iter('FILE'):
            if node.get('Method') == 'install':
                spoken += node.findtext('INLINE') or ''
        for gone in ('path mapping', 'deletion guards', 're-monitoring', 'Dry run is ON'):
            self.assertNotIn(gone, spoken, f'the manifest still advertises {gone}')
        self.assertIn('Test Mode', spoken)

    def test_the_tab_list_is_not_a_second_copy_of_the_markup(self):
        """Removing a tab left a stale name in a hand-kept list.

        `$('tvr-panel-schedule')` was null, setting `.hidden` on it threw, and the loop
        that shows one panel and hides the rest died at that point — so Settings, Job
        History, Live Log and Help, all listed after it, simply stopped appearing.
        """
        self.assertRegex(self.js, r"const VIEWS = \[\.\.\.document\.querySelectorAll\('\.tvr-side \[data-view\]'\)\]")

    def test_every_tab_has_a_panel_and_every_panel_has_a_tab(self):
        import re
        wanted = set(re.findall(r'data-view="([a-z-]+)"', self.html))
        views = set(re.findall(r'<section id="tvr-view-([a-z-]+)"', self.html))
        # The three series views share one section, narrowed by filter: one library, and
        # whether a series has a rule is a property of it rather than a different place.
        views |= {'series-connected', 'series-unconnected'}
        self.assertEqual(wanted, views, 'a sidebar item with no view, or a view nothing reaches')

    def test_the_version_constant_matches_the_version_file(self):
        """They are read from different places and must not drift.

        The file drives the package name and the manifest; the constant is what the
        interface shows and what a run records. Unraid also skips an install when the
        manifest version matches what is registered, so a stale version means a rebuilt
        package silently does not install.
        """
        import core
        self.assertEqual(core.VERSION, (ROOT / 'VERSION').read_text().strip())

    def test_editing_a_series_happens_beside_the_list(self):
        """No dialog for add or edit: the list stays visible while you change one.

        The form is built once and handed to whichever surface shows it, so the pane and
        any future dialog cannot drift into two different editors.
        """
        self.assertIn('function ruleForm(existing, preselect)', self.js)
        self.assertIn('function openEditor(existing, preselect)', self.js)
        self.assertIn('id="tvr-details"', self.html)
        self.assertNotRegex(self.js, r"dialog\((existing \? 'Edit series'|'Add series')")

    def test_the_list_offers_a_layout_and_a_selection(self):
        for identifier in ('tvr-layout-list', 'tvr-layout-grid', 'tvr-select-shown',
                           'tvr-select-none', 'tvr-selected-count'):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('function renderMassEdit', self.js)
        self.assertIn('tvr-rules-grid', self.css)

    def test_a_mass_edit_leaves_unchanged_fields_alone(self):
        # Every field defaults to "Unchanged" and is only applied when it is set, so
        # selecting thirty series and touching one field cannot rewrite the other five.
        block = self.js.split('function renderMassEdit')[1].split('function seriesFacts')[0]
        for control in ('presetSelect', 'monitoringSelect', 'specialsSelect', 'enabledSelect'):
            self.assertRegex(block, rf'if \({control}\.value\)')
        self.assertIn("'Unchanged'", block)

    def test_the_sidebar_scrolls_on_its_own(self):
        self.assertRegex(self.css, r'\.tvr-side \{[^}]*overflow-y: auto')
        self.assertRegex(self.css, r'\.tvr-side \{[^}]*position: sticky')

    def test_one_library_serves_every_series_view(self):
        """A series Sonarr knows about belongs in one place whether it has a rule or not.

        Add was a separate view answering a different question — "what can I add?" — which
        is why it showed 2986 of 3022. There is one list now, and three filters over it.
        """
        self.assertIn("const LIBRARY = { 'series-all': 'all'", self.js)
        self.assertIn('function renderLibrary', self.js)
        self.assertNotIn('function renderAddSeries', self.js)
        self.assertNotIn('id="tvr-add-grid"', self.html)

    def test_the_ended_toggle_only_hides_what_has_no_rule(self):
        # A connected ended series is where retention matters most; hiding it would hide
        # a rule that is actively deleting.
        self.assertIn('if (hideEnded && series.ended && !rule) return;', self.js)

    def test_neither_the_list_nor_its_payload_carries_three_thousand_of_anything(self):
        self.assertIn('LIBRARY_LIMIT', self.js)
        self.assertIn('LIST_FIELDS', (ROOT / 'src' / 'tv-retention' / 'worker' / 'actions.py').read_text())
        worker = (ROOT / 'src' / 'tv-retention' / 'worker' / 'actions.py').read_text()
        for heavy in ("'overview'", "'seasons'"):
            self.assertNotIn(heavy, worker.split('LIST_FIELDS = (')[1].split(')')[0])

    def test_the_icon_is_not_a_bin(self):
        # The plugin is named for keeping things.
        page = (ROOT / 'src' / 'tv-retention' / 'TVRetention.page').read_text()
        self.assertIn('Icon="television"', page)
        self.assertNotIn('trash', page)

    def test_what_stops_work_sits_above_the_chrome(self):
        # A message about the array being down belongs over the page, not inside it.
        head = self.html.split('<header class="tvr-topbar">')[0]
        for identifier in ('tvr-array', 'tvr-checking', 'tvr-test-banner'):
            self.assertIn(f'id="{identifier}"', head, f'{identifier} is below the header')

    def test_no_two_elements_share_an_id(self):
        """A duplicate id is a lookup that silently finds the wrong element.

        The Test Mode select was given the banner's id, so reading the setting read a div
        and writing it wrote to nothing.
        """
        import re
        found = re.findall(r'id="([a-z0-9-]+)"', self.html)
        duplicates = sorted({name for name in found if found.count(name) > 1})
        self.assertEqual(duplicates, [])

    def test_test_mode_can_be_made_small_but_never_silent(self):
        self.assertIn('id="tvr-test-chip"', self.html)
        options = self.html.split('id="tvr-test-banner-mode"')[1].split('</select>')[0]
        self.assertIn('value="full"', options)
        self.assertIn('value="chip"', options)
        self.assertNotIn('value="off"', options)
        self.assertNotIn('value="none"', options)

    def test_an_error_can_never_be_acknowledged_or_muted(self):
        """Hiding \"this series will not run\" does not stop it being true."""
        import alerts as alert_module
        from core import BLOCKING_KINDS
        for kind in BLOCKING_KINDS:
            self.assertTrue(alert_module.KINDS[kind]['blocking'])
        error = alert_module.make('unmatched', rule_id='r1')
        self.assertFalse(alert_module.may_acknowledge(error, {'alerts': {'acknowledge': True}}))
        notice = alert_module.make('ended-expired', rule_id='r1')
        self.assertTrue(alert_module.may_acknowledge(notice, {'alerts': {'acknowledge': True}}))
        # And the interface offers it on exactly the same terms.
        self.assertIn("alert.severity !== 'error' && !alert.blocking", self.js)

    def test_add_and_edit_are_one_panel(self):
        """They were the same form with different framing; now they are the same form.

        The differences that remain are the ones that mean something: what the heading
        says, and whether the action adds, updates or deletes.
        """
        self.assertIn("title: existing ? 'Edit series' : 'Add series'", self.js)
        self.assertIn("textContent: editing.existing ? 'Update' : 'Add series'", self.js)
        # No picker and no instance choice: the navigator already said which series, and a
        # rule *is* its binding to one, so re-pointing it is delete and add.
        self.assertNotIn('instanceSelect', self.js)
        self.assertNotIn("placeholder: 'Type a few letters", self.js)

    def test_the_action_says_whether_pressing_it_would_do_anything(self):
        self.assertIn('primary.disabled = !valid || (editing.existing && context.state() === settled)', self.js)
        self.assertIn("primary.title = !valid ? 'Set a preset, or at least one keep value'", self.js)

    def test_inheriting_names_what_it_inherits(self):
        # "Use the global setting" made you go and look it up.
        self.assertIn('`[Default] ${globalSpecials}`', self.js)
        self.assertIn('`[Default] ${globalMonitoring}`', self.js)

    def test_posters_are_fitted_rather_than_cropped(self):
        self.assertRegex(self.css, r'\.tvr-rules-grid \.tvr-poster-row img \{ object-fit: contain')
        # And connected is a whole border in poster view, not a bar down one side.
        self.assertRegex(self.css, r'\.tvr-rules-grid \.tvr-rule\.ok \{ border-color: var\(--tvr-good\)')

    def test_opening_a_series_opens_its_settings(self):
        """A read-only card with an Edit button was a step that only ever had one answer.

        Removing the button without moving the settings left a connected series with no
        way in at all, which is the regression this pins.
        """
        self.assertIn('openEditor(rule);', self.js)
        self.assertNotIn('renderOneDetail', self.js)
        self.assertIn('if (chosen.length === 1) { openEditor(chosen[0]); return; }', self.js)

    def test_each_column_scrolls_within_something(self):
        # overflow:auto with nothing to overflow moves the whole page instead, which is why
        # the sidebar did not keep its own scrollbar.
        self.assertRegex(self.css, r'\.tvr-shell \{ height: calc\(100vh')
        for selector in (r'\.tvr-side \{ height: 100%', r'\.tvr-main \{ height: 100%; overflow-y: auto',
                         r'\.tvr-details \{ position: sticky; top: 0; max-height: 100%; overflow-y: auto'):
            self.assertRegex(self.css, selector)
        self.assertRegex(self.css, r'\.tvr-topbar \{ position: sticky; top: 0')
