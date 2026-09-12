import re
import unittest
from pathlib import Path

import context  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'src' / 'assets'
ENTRY = 'app.js'


def interface_js() -> str:
    """Every shipped module as one string, the entry first.

    The interface is a module graph rather than a single file, so a test asking whether
    the page does something has to ask the whole graph: a helper that moved from `app.js`
    to `format.js` is still shipped, and a check reading only the entry would call it
    gone. Order is the entry then the rest sorted, so a failure message is stable.

    Reading the graph rather than the entry is what makes an extraction a no-op here,
    which is the point: these tests describe the interface's behaviour, and moving a
    function between files does not change it. The few tests that genuinely care *which*
    file something is in read that file directly through `module_js`.
    """
    return '\n'.join(module_js(name) for name in module_names())


def module_names() -> list[str]:
    """The shipped modules, entry first, then the rest sorted."""
    rest = sorted(p.name for p in ASSETS.glob('*.js') if p.name != ENTRY)
    return [ENTRY] + rest


def function_body(source: str, name: str) -> str:
    """The body of a named function declaration, found by its own indentation.

    Splitting on the next `\\n  function ` worked while every function sat two spaces deep
    inside one IIFE. At module scope they sit at column zero and their nested helpers sit
    where they used to, so a fixed indent either stops at the first nested function or
    runs past the end. Matching the declaration's own indent and closing on the brace at
    that same indent gives the same body either way, which is what lets these tests
    survive an extraction unchanged.
    """
    opener = re.search(r'^([ \t]*)function ' + re.escape(name) + r'\(', source, re.M)
    assert opener, f'no declaration of {name}()'
    tail = source[opener.end():]
    end = re.search(r'\n' + opener.group(1) + r'\}', tail)
    assert end, f'unterminated body for {name}()'
    return tail[:end.start()]


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and string literals, so a scan sees only code.

    One left-to-right pass rather than a comment sweep followed by a string sweep. Taking
    comments first lets the `//` inside `'http://…'` open a comment that runs to the end of
    the line, eating the closing quote; every literal after it is then read inside-out, and
    with the modules joined into one string that phase error crosses file boundaries. A
    single pass cannot start a comment inside a string or a string inside a comment,
    because whichever opens first consumes the other.
    """
    out = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if source.startswith('/*', i):
            end = source.find('*/', i + 2)
            end = n if end < 0 else end + 2
            out.append(' ' * (end - i))
            i = end
        elif source.startswith('//', i):
            end = source.find('\n', i)
            end = n if end < 0 else end
            out.append(' ' * (end - i))
            i = end
        elif ch in '`"\'':
            j = i + 1
            while j < n:
                if source[j] == '\\':
                    j += 2
                    continue
                if source[j] == ch:
                    j += 1
                    break
                j += 1
            out.append(' ' * (j - i))
            i = j
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def module_js(name: str) -> str:
    # Explicit encoding: the source carries characters outside the Windows default
    # codepage, and read_text() would decode with it and fail off the container.
    return (ASSETS / name).read_text(encoding='utf-8')


class Interface(unittest.TestCase):
    """Guards against the class of bug where an author `display` rule defeats `hidden`."""

    @classmethod
    def setUpClass(cls):
        source = ROOT / 'src'
        cls.css = (source / 'assets' / 'app.css').read_text(encoding='utf-8')
        cls.js = interface_js()
        cls.entry = module_js(ENTRY)
        cls.html = (source / 'include' / 'interface.html').read_text(encoding='utf-8')

    def test_the_hidden_attribute_is_forced_to_win(self):
        self.assertRegex(self.css, r'#tv-retention \[hidden\][^{]*\{[^}]*display:\s*none\s*!important')

    def test_every_element_the_script_hides_exists_in_the_markup(self):
        import re
        for identifier in set(re.findall(r"\$\('([a-z0-9-]+)'\)\.hidden", self.js)):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is toggled but not in the markup')

    def test_the_busy_overlay_starts_hidden(self):
        self.assertRegex(self.html, r'id="tvr-busy"[^>]*hidden')

    def test_every_element_the_script_addresses_exists_in_the_markup(self):
        import re
        for identifier in sorted(set(re.findall(r"\$\('([a-z0-9-]+)'\)", self.js))):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is addressed but not in the markup')

    def test_requests_cannot_hang_forever(self):
        self.assertIn('AbortController', self.js)
        self.assertIn('DEFAULT_TIMEOUT', self.js)

    def test_a_failed_start_clears_the_overlay(self):
        self.assertRegex(self.js, r"refresh\(\)\.catch")

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
        self.assertIn('function alertMarks', self.js)

    def test_the_script_is_not_prefixed_by_a_stray_fragment(self):
        # A build-time edit once prepended a fragment above the opening comment, which
        # broke the whole file. The header is cheap to assert and would have caught it.
        # Against the entry: it is the file with the header, and the one a build-time
        # edit would have prepended to.
        self.assertTrue(self.entry.lstrip().startswith('/* TV Retention web UI.'))
        self.assertEqual(self.js.count("function render() {"), 1)

    def test_braces_and_parentheses_balance(self):
        for pair in ('{}', '()', '[]'):
            self.assertEqual(self.js.count(pair[0]), self.js.count(pair[1]),
                             f'unbalanced {pair} across the shipped modules')

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
        worker = ROOT / 'src' / 'worker'
        tick = (worker / 'main.py').read_text().split('def tick()')[1].split('\ndef ')[0]
        self.assertIn('sync_from_sonarr', tick)
        self.assertIn("'watch': action_watch", (worker / 'actions.py').read_text())

    def test_a_background_sweep_is_watched_not_duplicated(self):
        self.assertIn('startPolling', self.js)
        self.assertIn('data.busy', self.js)

    def test_the_background_flows_call_nothing_that_is_not_defined(self):
        """`renderStats()` and `planText(plan)` crashed live flows after later edits
        removed their definitions.

        renderStats aborted the check queue after its first completed rule and killed
        sweep polling on its first tick; planText broke the Run confirmation whenever a
        plan was actionable. The executable suite in tests/frontend/ exercises the
        flows; this keeps the dangling names from coming back.
        """
        self.assertNotIn('renderStats()', self.js)
        self.assertNotIn('planText', self.js)

    def test_every_module_constant_used_is_declared(self):
        """Catch a constant left behind when an edit replaced the block that declared it.

        A syntax check cannot see this: `PILL_ENDED is not defined` is a runtime error, and
        it broke the whole page once because a batched edit dropped the declaration while
        leaving two uses behind. Only SCREAMING_SNAKE names are considered — that is the
        shape every constant in this file has, and it keeps prose like "TVDB" or "HTTP"
        out of the comparison without needing an allowlist to be maintained.

        The scan reads the whole graph, so a constant declared in one module and used in
        another is declared as far as this is concerned — which is right: the import is
        checked for real by the runtime test, which links the graph and would fail on a
        binding no module exports.
        """
        code = strip_comments_and_strings(self.js)
        # Not preceded by a dot: Number.MAX_SAFE_INTEGER is a property, not a module constant.
        shape = r'(?<![.\w])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b'
        declared = set(re.findall(r'\b(?:const|let|var)\s+' + shape, code))
        used = set(re.findall(shape, code))
        missing = sorted(used - declared)
        self.assertEqual(missing, [], f'used but never declared: {missing}')
        # Prove the scan is actually finding constants rather than passing on an empty set.
        self.assertIn('DEFAULT_TIMEOUT', declared, 'the test must be seeing real constants')

    def test_blocking_problems_are_visibly_different_from_advisory_ones(self):
        css = (ROOT / 'src' / 'assets' / 'app.css').read_text()
        self.assertIn('.tvr-dot-badge.error', css)
        self.assertIn('.tvr-dot-badge.warning', css)
        self.assertIn('.tvr-dot-badge.blocked', css)
        self.assertIn('blocked', self.js)

    def test_the_series_badge_is_a_count_beside_the_title(self):
        # A circle carrying a number, next to the name — not a pill competing with it.
        self.assertIn('head.append(alertBadge(rule));', self.js)
        self.assertIn('tvr-dot-badge', self.js)

    def test_removing_a_series_is_queued_and_asks_for_the_right_word(self):
        """Two different consequences, two different words, and neither happens at once.

        The plugin never deletes a series itself: the destructive options ask Sonarr to,
        so Sonarr's own recycle bin and bookkeeping apply.
        """
        self.assertIn("'delete-series': 'DELETE'", self.js)
        self.assertIn("'delete-series-files': 'DELETE ALL'", self.js)
        self.assertIn('Queue removal', self.js)
        self.assertIn('function queuedBanner', self.js)
        self.assertRegex(self.js, r'Ask Sonarr to delete the series')

    def test_nothing_on_a_card_acts_immediately(self):
        # Every destructive control queues; only a run applies. Undo is the safety net.
        self.assertNotRegex(self.js, r"api\('remove-series'")
        self.assertIn('Undo', self.js)

    def test_test_mode_means_nothing_writes(self):
        """Scheduled or manual, no exceptions.

        It governed only the scheduler once, so a manual run deleted for real while the
        page said TEST MODE at the top of it, and a paragraph in a confirmation dialog was
        the only thing reconciling the two readings.
        """
        self.assertIn('tvr-test-mode', self.js)
        self.assertRegex(self.js, r'Test mode is on, so this changes nothing')
        self.assertNotIn("$('tvr-preview')", self.js)
        worker = (ROOT / 'src' / 'worker' / 'main.py').read_text()
        self.assertIn("test_mode = bool((settings.get('schedule') or {}).get('test_mode', True))",
                      worker)
        self.assertNotIn('test_mode = scheduled and', worker)

    def test_the_run_button_says_what_pressing_it_would_do(self):
        """Three states, and the colour is the sentence.

        Red is reserved for a fault that stops the whole run — no instance answering, or
        none configured. One broken series among thirty-five healthy ones is skipped, not a
        reason to call the button disabled.
        """
        block = self.js.split('const RUN_STATES = {')[1].split('};')[0]
        self.assertIn("live: ['Run'", block)
        self.assertIn("test: ['Run Test'", block)
        self.assertIn("blocked: ['Disabled'", block)
        state = self.js.split('function runState()')[1].split('\n  }')[0]
        self.assertIn('getSystemAlerts().some((alert) => alert.blocking)', state)
        self.assertNotIn('isBlocked', state, 'one broken series must not disable the button')
        # Blocked stays pressable: it is the shortest route to the reason.
        self.assertIn("if (runState() === 'blocked') return void showEverythingNeedingAttention();",
                      self.js)
        for tone in ('.tvr-run.live', '.tvr-run.test', '.tvr-run.blocked'):
            self.assertIn(tone, self.css)

    def test_the_run_button_follows_both_things_that_change_it(self):
        """Test Mode and the alerts move independently, and it missed both.

        `snapshot.test_mode` is refreshed only by a full snapshot call, so turning Test
        Mode off and saving left the button describing the mode the page had loaded with —
        at exactly the moment somebody is reading it to see whether it will delete
        something. And alerts arrive on their own schedule, never passing through
        renderTopBar, so a Sonarr coming back up left the button red.
        """
        state = self.js.split('const testMode = ()')[1].split('};')[0]
        self.assertIn('(settings || {}).schedule', state)
        # Nowhere else reads it. The helper's own fallback, for the moment before the first
        # settings arrive, is the only legitimate use — and a comment explaining why is not
        # a use at all.
        code = [line for line in self.js.splitlines()
                if 'snapshot.test_mode' in line and not line.strip().startswith('//')]
        self.assertEqual(code, [],
                         'read anywhere but the helper, it is stale the moment settings are saved')
        self.assertIn('!!(snapshot || {}).test_mode', state, 'the helper still needs a fallback')
        # It works out its own "nothing to do", so anything can call it.
        self.assertIn('function renderRunButton() {', self.js)
        applied = self.js.split('function applyAlerts')[1].split('\n  }')[0]
        self.assertIn('renderRunButton()', applied)

    def test_the_run_is_blue_rather_than_green(self):
        # Green already means "monitored" in this interface, and the run is the primary
        # action rather than a state that is going well.
        self.assertIn('button.tvr-run.live { background: var(--tvr-action); }', self.css)

    def test_the_top_bar_has_three_zones(self):
        # The middle is centred on the window, which a flex spacer cannot do: it would
        # centre on whatever is left after the other two.
        self.assertRegex(self.css, r'\.tvr-topbar \{ display: grid; grid-template-columns: 1fr auto 1fr')
        head = self.html.split('</header>')[0]
        for zone in ('tvr-brand', 'tvr-topbar-middle', 'tvr-topbar-actions'):
            self.assertIn(f'class="{zone}"', head)
        self.assertLess(head.index('tvr-brand'), head.index('tvr-topbar-middle'))
        self.assertLess(head.index('tvr-topbar-middle'), head.index('tvr-topbar-actions'))

    def test_the_version_is_a_version(self):
        # Not a date. A build stamp cannot say whether anything changed.
        self.assertIn('`v${snapshot.version}`', self.js)
        self.assertRegex((ROOT / 'VERSION').read_text().strip(), r'^\d+\.\d+\.\d+$')

    def test_about_is_the_help_default_and_carries_build_metadata(self):
        help_items = self.html.split('data-section="help"')[1].split('</div>')[0]
        system_items = self.html.split('data-section="system"')[1].split('</div>')[0]
        self.assertLess(help_items.index('data-view="help-about"'),
                        help_items.index('data-view="help-adding"'))
        self.assertNotIn('About', system_items)
        self.assertIn('id="tvr-view-help-about"', self.html)
        self.assertIn("if (section === 'help') { showView('help-about'); return; }", self.js)
        self.assertIn("['Version', snapshot().version || 'unknown']", self.js)
        self.assertIn("['Build', snapshot().build_number || 'not recorded']", self.js)
        self.assertIn("['Build date', buildDate]", self.js)
        actions = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        self.assertIn("'build_number': package_metadata('BUILD', 'TVR_BUILD_NUMBER')", actions)
        self.assertIn("'build_date': package_metadata('BUILD_DATE', 'TVR_BUILD_DATE')", actions)
        self.assertRegex((ROOT / 'BUILD').read_text().strip(), r'^[1-9]\d*$')

    def test_the_run_button_hides_only_on_a_complete_answer(self):
        # Hiding it on a stale or partial reading would be a promise the cache cannot keep.
        self.assertRegex(self.js, r'plan\.trustworthy && !plan\.actionable')


    def test_switching_a_series_off_is_done_where_its_settings_are(self):
        """Not on the card. It is a setting, and settings live in the panel.

        On the card it was a control sitting on a list built for browsing, one slip away
        from turning off a rule while looking for another one. The frame says which series
        are off instead.
        """
        self.assertNotIn('function enableToggle', self.js)
        self.assertIn('tvr-rule.disabled', self.css)
        self.assertIn("toggle('', rule.enabled, null, { className: 'tvr-identity-switch' })", self.js)

    def test_switching_a_series_off_happens_when_it_is_switched(self):
        """Not on Update. It is not a change to a form, it is a thing being done.

        Left as a form field it needed a save to take effect, so the list behind the panel
        went on showing a series as on after it had been switched off — and the dirty check
        called an already-applied value an unsaved change.
        """
        block = self.js.split("const enabled = toggle('', rule.enabled")[1].split('const identity')[0]
        self.assertIn("await saveSettings(null, true)", block)
        self.assertIn('target.enabled = rule.enabled = !on', block)   # nothing written, nothing shown
        # Adding is switched by which button saves it, so the switch would be a second
        # answer to the same question sitting above the first.
        self.assertIn('enabled.node.hidden = !existing;', block)
        # A rule that exists is switched on the spot, and one being added is switched by
        # which button saves it. Neither is a form value the dirty check should count.
        state = self.js.split('const formState = () => JSON.stringify(')[1].split('});')[0]
        self.assertNotIn('enabled', state)

    def test_the_switch_says_which_way_it_is_without_a_word_beside_it(self):
        # A caption reading "Enabled" beside every series is a word that never changes.
        block = self.js.split('const sayState = ()')[1].split('sayState();')[0]
        self.assertIn('Click to disable it.', block)
        self.assertIn('Click to enable it.', block)
        self.assertIn("aria-label", block)
        self.assertIn('.tvr-identity-controls { position: absolute; top: 0; right: 0;', self.css)

    def test_the_editor_does_not_repeat_what_the_panel_already_says(self):
        """An ended series with no episodes said the same four facts four times.

        The alert card's own heading named the series, counted its issues and dated the
        reading, and its footer offered a re-check — all of which the panel around it
        already carries.
        """
        self.assertIn("seriesAlertCard(rule, alertsHere, { compact: true })", self.js)
        block = self.js.split('function seriesAlertCard')[1].split('function systemAlertCard')[0]
        self.assertIn('if (compact) return card;', block)
        self.assertIn("if (!compact) {", block)

    def test_the_path_is_sonarrs_business_and_is_not_shown(self):
        # Nothing in the editor is decided by it, and it was the one line long enough to
        # wrap the panel.
        block = self.js.split("const identity = el('div', { className: 'tvr-identity' }")[1] \
                       .split(']);')[0]
        self.assertNotIn('tvr-mono', block)
        self.assertNotIn('rule.path', block)

    def test_the_library_asks_its_question_on_one_line(self):
        """Heading, search, sort, two filters and the layout are one question.

        Each of them was answering it from a row of its own, which cost two lines of a
        page whose subject is a list. The captions went with them: a search box says what
        it is by being one, and every sort option names an order.
        """
        head = (self.html.split('<div class="tvr-view-head tvr-series-head">')[1]
                .split('<div id="tvr-series-shell"')[0])
        for inside in ('tvr-library-title', 'tvr-search', 'tvr-sort', 'tvr-hide-ended',
                       'tvr-layout-list', 'tvr-scale'):
            self.assertIn(inside, head)
        self.assertNotIn('tvr-series-toolbar', self.html)
        self.assertNotIn('tvr-series-toolbar', self.css)
        # Short on the switch, and the whole rule in the tooltip: "Hide ended without a
        # rule" explained on the control what the control could explain on hover.
        self.assertIn('>Hide ended</span>', self.html)
        self.assertIn('One that has a rule stays, and one with an alert always stays.', self.html)
        # It wraps rather than squeezing: two rows on a narrow window is the honest
        # answer, not a search box six characters wide.
        self.assertIn('.tvr-series-head { flex-wrap: wrap', self.css)

    def test_a_poster_is_cached_against_the_artwork_and_not_the_series(self):
        """Sonarr's artwork path carries its last-write marker; nothing was reading it.

        `series.poster` was used as a boolean — *is there a picture* — and the proxy keyed
        its cache on the series id alone. So the first poster ever fetched was the one
        served for good, with a day of browser caching over the top of it.
        """
        self.assertIn('&stamp=${encodeURIComponent(series.poster)}', self.js)

    def test_the_library_says_used_by_the_binding_a_rule_holds(self):
        """Not by folder. A bare set of paths was wrong in both directions.

        It said "already used" about a series a different Sonarr owns, and said nothing
        about a series whose folder had moved since its rule was written.
        """
        actions = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        block = actions.split('def action_series')[1].split('\n\n\n')[0]
        self.assertIn("used = {(r['instance_id'], r['series_id'])", block)
        self.assertNotIn("{r['path'] for r in", block)

    def test_the_rpc_surface_offers_nothing_the_page_cannot_reach(self):
        """An unreachable action is still reachable by anyone who can post to the bridge.

        `monitoring` read every bound series from Sonarr synchronously, in one request,
        with no `offline=True` — against the rule that reads happen per show, in the
        background, and never hold anything but the show being read.
        """
        actions = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        offered = set(re.findall(r"^    '([a-z-]+)': action_", actions, re.M))
        asked = set(re.findall(r"api\('([a-z-]+)'", self.js))
        self.assertEqual(sorted(offered - asked), [],
                         'actions the interface never asks for')

    def test_the_webgui_defaults_this_page_has_to_undo_are_undone(self):
        """Unraid styles every button and text input through `:where()` selectors.

        `:where()` contributes no specificity, so those rules never *conflict* with this
        file — every property it does not name simply applies. That is not a cascade
        problem anyone sees; it is twenty pixels of margin on each of fifteen sidebar
        entries, an 86px floor under a one-character icon button, and a 2rem line box
        around a 13px input.
        """
        base = self.css.split('#tv-retention button {')[1].split('}')[0]
        for undone in ('margin: 0', 'min-width: 0', 'white-space: normal'):
            self.assertIn(undone, base, 'the WebGUI sets this on every button')
        fields = self.css.split('#tv-retention input, #tv-retention select,')[1].split('}')[0]
        self.assertIn('min-height: 0', fields)
        self.assertIn('line-height: 1.35', fields)
        # `width: 100%` on a select is harmless in a grid cell and means "all of it" in a
        # flex row, which is how the sort dropdown took the search box's space.
        self.assertIn('width: auto', fields)
        self.assertIn('.tvr-field > input, .tvr-field > select, .tvr-field > textarea '
                      '{ width: 100%; }', self.css)

    def test_a_sidebar_group_is_a_band_and_now_a_control(self):
        """Reaching the panel edge is what makes it a section rather than small type.

        It was a div when nothing collapsed. It opens its section now, so it is a button —
        and one that says whether it is open, for anyone not looking at the caret.
        """
        self.assertIn('class="tvr-side-head" data-section-head=', self.html)
        self.assertIn('aria-expanded=', self.html)
        block = self.css.split('.tvr-side-head {')[1].split('}')[0]
        self.assertIn('background: var(--tvr-soft)', block)
        # Out past the panel's own padding, which is what "full width" means here.
        panel = self.css.split('.tvr-side { display: block;')[1].split('}')[0]
        self.assertIn('margin: 0 -4px', block)

    def test_one_section_is_open_at_a_time(self):
        """Nineteen items in five groups is a wall; four headings and one group is a list."""
        self.assertIn('function openSection(section)', self.js)
        self.assertRegex(self.css, r'\.tvr-side-items \{[^}]*display: none')
        self.assertRegex(self.css, r'\.tvr-side-group\.open \.tvr-side-items \{[^}]*display: (grid|block)')

    def test_a_heading_returns_you_to_where_you_were(self):
        """A section is a place, not a label.

        And Series is the exception worth encoding: with nothing connected yet, All is the
        only one of the three lists with anything in it.
        """
        self.assertIn("remembered(`last.${section}`, '')", self.js)
        self.assertIn('sectionDefault(section)', self.js)
        self.assertIn('remember(`last.${section}`, name)', self.js)
        block = self.js.split('function sectionDefault(section)')[1].split('\n  }')[0]
        self.assertIn("(getSettings().rules || []).length ? 'series-connected' : 'series-all'", block)

    def test_the_sidebar_says_watching_rather_than_connected(self):
        # "Connected" also describes Sonarr, TMDB and everything else under Connections.
        self.assertIn('data-view="series-connected">Watching', self.html)
        self.assertIn('data-view="series-unconnected">Not watching', self.html)
        self.assertNotIn('>Connected\n', self.html)

    def test_the_layout_switch_is_two_square_icons(self):
        # A box around them made one control out of two buttons, and the padding inside it
        # was carrying an icon that needs none.
        seg = self.css.split('#tv-retention .tvr-seg button {')[1].split('}')[0]
        self.assertIn('width: 24px', seg)
        self.assertIn('height: 24px', seg)
        self.assertIn('padding: 0', seg)
        self.assertNotIn('border: 1px solid var(--tvr-line)', self.css.split('.tvr-seg {')[1].split('}')[0])

    def test_a_sentence_shaped_button_is_not_shouted(self):
        # Uppercase with 1.8px tracking is the WebGUI's idea of a button. These are
        # buttons because they can be clicked, not because they are named actions.
        block = self.css.split('#tv-retention button.tvr-plan, #tv-retention button.tvr-plan-total,')[1] \
                        .split('}')[0]
        self.assertIn('text-transform: none', block)
        self.assertIn('letter-spacing: normal', block)

    def test_badge_styling_outranks_the_generic_button_rule(self):
        """`#tv-retention button` outranks a bare class, which is not obvious and bit once.

        The badge is a button, so every property that shapes it — padding, radius, size —
        has to be written under #tv-retention or the generic rule wins and it renders as a
        grey rectangle with the number pushed off centre.
        """
        css = (ROOT / 'src' / 'assets' / 'app.css').read_text()
        import re
        shaping = re.search(r'#tv-retention \.tvr-dot-badge[^{]*\{([^}]*)\}', css)
        self.assertIsNotNone(shaping, 'the badge must be styled under #tv-retention')
        for property_name in ('padding', 'border-radius', 'width', 'height'):
            self.assertIn(property_name, shaping.group(1))
        # A bare `.tvr-dot-badge {` rule would silently lose to the generic button rule.
        self.assertNotRegex(css, r'(?m)^\.tvr-dot-badge\s*\{')

    def test_severity_is_carried_by_the_frame_not_a_colour_wash(self):
        css = (ROOT / 'src' / 'assets' / 'app.css').read_text()
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

    def test_a_fix_is_presented_as_an_action(self):
        self.assertIn('Quick action: ', self.js)
        css = (ROOT / 'src' / 'assets' / 'app.css').read_text()
        self.assertRegex(css, r'#tv-retention button\.tvr-action[^{]*\{[^}]*--tvr-action')

    def test_only_the_mark_and_the_card_edge_carry_severity(self):
        css = (ROOT / 'src' / 'assets' / 'app.css').read_text()
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
        self.assertIn('.tvr-topbar-actions', self.css)
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

    def _panel(self, view):
        return self.html.split(f'id="tvr-view-{view}"')[1].split('</section>')[0]

    def test_every_automation_control_is_wired_to_save(self):
        """The same bug the schedule panel already has a guard for.

        Automation is where a control is most likely to be added and least likely to be
        noticed if it does nothing: every setting on it is global, so nothing on a series
        card contradicts a value that was never saved.

        A mount point is exempt from the second half and only the second half: its value
        travels through `questionInputs` rather than by id, and the test below is what
        holds *those* to the same standard.
        """
        collect = self.js.split('function collectSettings()')[1].split('\n  }')[0]
        render = self.js.split('function renderSettings()')[1].split('\n  }\n')[0]
        for view in ('media-automation', 'settings-safety'):
            for identifier in re.findall(r'id="(tvr-[a-z-]+)"', self._panel(view)):
                if 'help' in identifier or 'field' in identifier:
                    continue
                # A mount point holds controls built at render time: it is filled from a
                # question table rather than by name, and its value reaches the document
                # through a collector rather than by id. Naming that collector here is the
                # point — a new mount cannot be added without saying what saves it, which
                # is the whole failure this test exists to prevent.
                if identifier in self.MOUNTS:
                    self.assertIn(f"'{identifier}'", self.js,
                                  f'{identifier} is a mount nothing fills')
                    self.assertIn(self.MOUNTS[identifier], collect,
                                  f'{identifier} is on {view} but nothing saves it')
                    continue
                self.assertIn(identifier, render, f'{identifier} is on {view} but never filled in')
                self.assertIn(identifier, collect, f'{identifier} is on {view} but never saved')

    MOUNTS = {
        'tvr-auto-monitoring': "questionInputs['monitoring.",
        'tvr-auto-persistence': "questionInputs['persistence.",
        'tvr-auto-search': "questionInputs['search.after_monitor']",
        'tvr-air-unresolved': "questionInputs['air.unresolved']",
        'tvr-air-still': "questionInputs['air.still_unresolved']",
        'tvr-air-providers': 'providers: airOrder',
        'tvr-exclude-folders': 'folderPhrases()',
        'tvr-exclude-episodes': 'episodePhrases()',
    }

    def test_every_question_offered_is_a_question_read_back(self):
        """A radio group renders from one table and saves from a hand-written line.

        Adding a question to the table puts it on the page and nowhere else: it renders,
        it takes a click, it looks saved, and `collectSettings` never mentions it. That is
        the mount-point version of the bug the test above catches for plain controls.
        """
        collect = self.js.split('function collectSettings()')[1].split('\n  }')[0]
        groups = re.findall(r"^    \['([a-z_]+)', 'tvr-auto-([a-z]+)', \[", self.js, re.M)
        asked = set()
        for group, _ in groups:
            block = self.js.split(f"['{group}', 'tvr-auto-")[1].split('\n    ]],')[0]
            asked |= {f'{group}.{name}' for name in re.findall(r"^      \['([a-z_]+)',", block, re.M)}
        asked.add('search.after_monitor')
        asked |= {f'air.{name}' for name in re.findall(r"^    \['([a-z_]+)', 'tvr-air-", self.js, re.M)}
        self.assertTrue(asked, 'the question tables were not found at all')
        for key in sorted(asked):
            self.assertIn(f"questionInputs['{key}']", collect,
                          f'{key} is offered on a page but never saved')

    def test_a_remembered_view_is_checked_before_it_is_used(self):
        """localStorage outlives the view it names, and a rename is not a migration.

        `media-rules` became `media-automation`, and every browser that had ever opened
        Media management went on remembering the old name. `showView` does not know which
        section was asked for, so its fallback sent you to `series-all` — in a different
        section — and clicking Media management looked like a dead button.

        Every other remembered value is a preference that degrades harmlessly. This one is
        a name, and a name has to still name something.
        """
        handler = self.js.split('[data-section-head]').pop().split('function showView')[0]
        self.assertIn('VIEWS.includes(last)', handler)
        # The shape of the bug: the remembered name going straight into showView.
        self.assertNotIn("showView(remembered(`last.${section}`", self.js)

    def test_the_picker_names_every_reason_an_episode_can_be_excluded(self):
        """A fourth reason added to core would be shown as a pattern that has no text.

        The picker splits on manual versus not, then on season versus pattern, and the
        second split has no default. Adding a reason server-side and forgetting the
        interface is silent: the episode is greyed out, and the line saying why is blank.
        """
        import core
        self.assertEqual(sorted(core.EXCLUSION_REASONS),
                         ['episode', 'folder', 'manual', 'season', 'specials'])
        # Any indent: the keys sit one level inside the declaration, wherever that lands.
        named = set(re.findall(r'^\s+([a-z]+): \(', self.js.split('EXCLUDED_WHY = {')[1]
                               .split('};')[0], re.M))
        # `manual` is the one the picker does not explain, because it is the one you did.
        self.assertEqual(named, set(core.EXCLUSION_REASONS) - {'manual'})

    def test_excluding_and_monitoring_do_not_share_a_tree(self):
        """A tick means "never touch this" in one and "Sonarr should have this" in the
        other. The day they share a code path, one of them is wrong."""
        self.assertIn('function exclusionTree(', self.js)
        self.assertIn('function monitorTree(', self.js)
        picker = function_body(self.js, 'exclusionTree')
        self.assertNotIn('monitorTree(', picker)
        self.assertNotIn('monitored', picker.split('picked:')[1])

    def test_the_unsaved_mark_reaches_every_view_that_can_save(self):
        """It was bound to views whose id began "tvr-view-settings".

        Automation and Presets each carry a Save button and neither id starts that way, so
        changing something on either left the page dirty with nothing on screen saying so.
        """
        self.assertIn(".tvr-view:has(.tvr-save)", self.js)
        self.assertNotIn('.tvr-view[id^="tvr-view-settings"]', self.js)

    def test_the_badge_is_as_tall_as_it_is_round(self):
        # Height came from line-height while width came from padding, so it rendered as a
        # squashed oval rather than a badge.
        self.assertRegex(self.css, r'\.tvr-tab-badge \{[^}]*height: 18px')
        self.assertRegex(self.css, r'\.tvr-tab-badge \{[^}]*min-width: 18px')

    def test_the_tab_list_is_not_a_second_copy_of_the_markup(self):
        """Removing a tab left a stale name in a hand-kept list.

        `$('tvr-panel-schedule')` was null, setting `.hidden` on it threw, and the loop
        that shows one panel and hides the rest died at that point — so Settings, Job
        History, Live Log and Help, all listed after it, simply stopped appearing.
        """
        self.assertRegex(self.js, r"const VIEWS = \[\.\.\.document\.querySelectorAll\('\.tvr-side \[data-view\]'\)\]")

    def test_every_view_is_inside_the_main_column(self):
        """Five of them were not, and rendered under the shell rather than in it.

        A view outside `<main>` is a sibling of the whole layout, so it lays out below the
        sidebar and the top bar instead of in the column beside them. `showView` still
        finds it and still unhides it, which is why this looks like a styling problem and
        is not one: the markup is wrong and every id check passes anyway.

        The dialog and the busy overlay are deliberately outside and stay there — they
        cover the page rather than sit in it.
        """
        inside = self.html.split('<main')[1].split('</main>')[0]
        after = self.html.split('</main>')[1]
        for view in re.findall(r'<section id="tvr-view-([a-z-]+)"', self.html):
            self.assertIn(f'id="tvr-view-{view}"', inside, f'{view} renders outside <main>')
        self.assertNotIn('<section id="tvr-view-', after, 'a view was left after </main>')

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

    def test_the_sidebar_scrolls_on_its_own(self):
        """It has its own scrollbar rather than dragging the page.

        `position: sticky` went with the frame: sticking to the top of a scrolling page is
        what you do when the page scrolls. The window does not scroll now — the shell is
        the height of it, and the sidebar is the height of the shell.
        """
        self.assertRegex(self.css, r'\.tvr-side \{[^}]*overflow-y: auto')
        self.assertRegex(self.css, r'\.tvr-side \{ height: 100%')
        self.assertNotRegex(self.css, r'\.tvr-side \{[^}]*position: sticky')

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
        self.assertNotIn('LIBRARY_LIMIT', self.js)
        self.assertIn('content-visibility: auto', self.css)
        self.assertIn('LIST_FIELDS', (ROOT / 'src' / 'worker' / 'actions.py').read_text())
        worker = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        for heavy in ("'overview'", "'seasons'"):
            self.assertNotIn(heavy, worker.split('LIST_FIELDS = (')[1].split(')')[0])

    def test_what_stops_work_sits_above_the_chrome(self):
        # A banner about a sweep in progress, or about Test Mode, belongs over the page
        # rather than inside it. The array warning went with the plugin: a container has
        # no array to be told about.
        head = self.html.split('<header class="tvr-topbar">')[0]
        for identifier in ('tvr-checking', 'tvr-test-banner'):
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
        self.assertIn("textContent: editing.existing ? 'Update' : 'Save and enable'", self.js)
        # No picker and no instance choice: the navigator already said which series, and a
        # rule *is* its binding to one, so re-pointing it is delete and add.
        self.assertNotIn('instanceSelect', self.js)
        self.assertNotIn("placeholder: 'Type a few letters", self.js)

    def test_the_action_says_whether_pressing_it_would_do_anything(self):
        self.assertIn('button.disabled = !valid || (editing.existing && now === context.saved)', self.js)
        self.assertIn("button.title = !valid ? 'Set a preset, or at least one keep value'", self.js)

    def test_the_age_sits_under_the_label_without_growing_the_bar(self):
        """It dates the whole pane, so it belongs to the pane's bar.

        Set small and quiet, and neither uppercase nor tracked: two lines fit in the
        height the icons beside them already take.
        """
        self.assertIn('headMain.append(context.readLine);', self.js)
        self.assertIn('.tvr-details-head-main { display: grid', self.css)
        block = self.css.split('.tvr-details-head .tvr-identity-read {')[1].split('}')[0]
        self.assertIn('font-size: 9.5px', block)
        self.assertIn('text-transform: none', block)

    def test_refreshing_a_series_with_no_rule_reads_the_series(self):
        """There is no rule to check, so the catalogue entry is what goes stale.

        Re-running the counts alone left the title, the season count and the next airing
        exactly as they were, and the age line dated by a catalogue sync that had not
        happened — so pressing it appeared to do nothing at all.
        """
        block = self.js.split('const reread = (button) =>')[1].split('const controls =')[0]
        self.assertIn("api('refresh-series'", block)
        self.assertIn('Object.assign(series, data.series)', block)
        self.assertIn('readAt = data.read_at', block)          # dated by its own reading
        actions = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        self.assertIn("'refresh-series': action_refresh_series,", actions)

    def test_the_panel_plan_does_not_total_three_lines_it_can_see(self):
        # A fourth line saying the sum of the three under it.
        self.assertIn('if (rows.length > 1 && onOpen && !compact) {', self.js)

    def test_a_series_with_no_rule_reports_no_monitoring(self):
        # Monitoring against a keep window is a thing a rule does, and there is no rule.
        block = self.js.split('const sayCounts = (counts) =>')[1].split('const stamp =')[0]
        self.assertIn('if (!existing) {', block)
        self.assertIn("textContent: plural(total, 'episode')", block)

    def test_adding_and_switching_on_are_two_buttons(self):
        """Two decisions, so two buttons rather than a switch that has to be found first.

        And no Cancel: the pane's close leaves what was typed where it was, which is what
        clicking away already did, so a third button to throw it away was one more thing
        to read on the way to the two that matter.
        """
        self.assertIn("textContent: 'Save' });", self.js)
        self.assertIn('primary.addEventListener(\'click\', () => commit(true));', self.js)
        self.assertIn('quiet.addEventListener(\'click\', () => commit(false));', self.js)
        self.assertIn('enabled: existing ? context.enabled.input.checked : !!startEnabled', self.js)
        self.assertNotIn("textContent: 'Cancel'", self.js)

    def test_inheriting_names_what_it_inherits(self):
        # "Use the global setting" made you go and look it up.
        self.assertIn('`[Default] ${globalSpecials}`', self.js)
        self.assertIn('`[Default] ${globalMonitoring}`', self.js)

    def test_the_border_says_one_thing_and_says_it_in_both_layouts(self):
        """Ranked, because a border can only say one thing.

        What you are looking at, then what is wrong with it, then whether it is yours,
        then that it is merely known about. A bar down one side read as a printing fault
        under artwork, and as noise beside a 25px poster, so the whole frame carries it.
        """
        base = self.css.split('\n.tvr-rule { border:')[1].split('}')[0]
        self.assertIn('2px solid', base)
        for tone, token in (('loose', '--tvr-line'), ('watched', '--tvr-action'),
                            ('alert-notice', '--tvr-notice'), ('alert-warning', '--tvr-warn'),
                            ('alert-error', '--tvr-bad')):
            self.assertRegex(self.css, rf'\.tvr-rule\.{tone} \{{ border-color: var\({token}\)')
        # Selected is heavier and the one colour nothing else uses — drawn inward, because
        # a thicker border changed the card's size, nudged every card after it, and slid
        # the one just clicked out from under the pointer.
        selected = self.css.split('.tvr-rule.selected {')[1].split('}')[0]
        self.assertIn('border-color: var(--tvr-fg)', selected)
        self.assertIn('box-shadow: inset', selected)
        self.assertNotIn('border-width', selected)
        # And the ranking lives in one place rather than in the cascade.
        block = self.js.split('function cardTone(row)')[1].split('\n  }')[0]
        self.assertLess(block.index('isOpen'), block.index('worstSeverity'))
        self.assertLess(block.index('worstSeverity'), block.index("return 'loose'"))
        self.assertIn('isOpen(rule, series)', block)

    def test_an_unmanaged_series_can_be_selected_by_its_sonarr_identity(self):
        editor = module_js('series-editor.js')
        block = editor.split('const isOpen =')[1].split('// Half-typed edits')[0]
        self.assertIn('editing.series.instance_id === series.instance_id', block)
        self.assertIn('editing.series.series_id === series.series_id', block)
        library = module_js('library.js')
        self.assertGreaterEqual(library.count('isOpen(rule, series)'), 2,
                                'both the selected tone and second-click close need the series identity')
        self.assertIn('isOpen: (rule, series) => isOpen(rule, series)', self.js)

    def test_every_matching_series_is_rendered_without_an_arbitrary_cap(self):
        library = module_js('library.js')
        self.assertNotIn('LIBRARY_LIMIT', library)
        self.assertIn("cardsInto(band.box, rows, 'all');", library)
        self.assertIn('content-visibility: auto', self.css)

    def test_collapsed_band_does_not_show_its_filter_switches(self):
        library = module_js('library.js')
        self.assertIn('if (band.open && sleeping.length)', library)
        self.assertIn('if (band.open) bandFilterSwitches', library)

    def test_native_select_menus_use_the_application_palette(self):
        self.assertIn('#tv-retention select option, #tv-retention select optgroup', self.css)
        self.assertIn('background-color: var(--tvr-bg)', self.css)

    def test_a_card_says_what_is_happening_to_the_series_itself(self):
        """Ended, queued and switched off are states of the series, not of its border.

        Queued used to replace the whole card, which made a series about to be removed the
        one thing in the library you could not see the poster of.
        """
        self.assertRegex(self.css, r'\.tvr-rule\.ended \.tvr-poster img \{ filter: grayscale')
        self.assertRegex(self.css, r'\.tvr-rule\.queued \{ opacity: \.5')
        self.assertIn('.tvr-queued-x', self.css)
        self.assertIn("className: 'tvr-queued-x'", self.js)
        # Dashed and dimmed, keeping whatever colour it had: the colour is the truth about
        # the series, the dashes are the truth about the rule.
        disabled = self.css.split('.tvr-rule.disabled {')[1].split('}')[0]
        self.assertIn('border-style: dashed', disabled)
        self.assertNotIn('border-color', disabled)

    def test_three_sections_answer_three_questions(self):
        """What needs doing, what will be done, and everything.

        The first two ignore the filters, because neither question is less true for being
        asked on another tab. All is always there because its count answers the question
        people actually ask — why is a show I expected not on screen.
        """
        for name, label in (('attention', 'Needs attention'), ('scheduled', 'Scheduled actions'),
                            ('all', 'All')):
            self.assertIn(f"'{name}', '{label}'", self.js)
        # The two upper bands appear only when they hold something; All always does.
        self.assertIn('if (scheduled.length) {', self.js)
        self.assertIn('if (attention.length || sleeping.length) {', self.js)
        self.assertIn("const band = sectionBand('all', 'All', rows.length, library.length);", self.js)
        self.assertIn("BAND_OPEN = { attention: true, scheduled: false, all: true }", self.js)

    def test_a_band_says_how_much_the_filters_are_hiding(self):
        # The first number alone cannot: "573" does not say whether anything is missing.
        self.assertIn("textContent: `${shown}/${total}`", self.js)
        # Totals ignore the search that narrows the list beneath them.
        self.assertIn("visibleLibrary('alerts', false).length", self.js)
        self.assertIn("visibleLibrary('scheduled', false).length", self.js)

    def test_a_queued_removal_is_scheduled_even_with_the_rule_switched_off(self):
        """Removals are the one thing that ignores the enabled flag.

        Hiding the only destructive thing still going to happen, because the rule that no
        longer runs is switched off, would be exactly the wrong way round. A blocked series
        never counts either way, because no run will reach it.
        """
        block = self.js.split('function scheduledFor(rule)')[1].split('\n  }')[0]
        self.assertLess(block.index('queuedRemoval(rule)'), block.index('rule.enabled'))
        self.assertIn('if (!rule.enabled || isBlocked(rule.id)) return false;', block)

    def test_the_main_list_stops_ranking_what_the_section_above_it_ranks(self):
        """Sorting problems to the top was how you found them before there was a section.

        Doing both puts the same series in two places for the same reason, and makes the
        list underneath jump about as alerts come and go.
        """
        self.assertNotIn('value="attention"', self.html)
        self.assertNotIn('ATTENTION_RANK', self.js)
        block = self.js.split("const order = $('tvr-sort').value;")[1].split('return rows;')[0]
        self.assertNotIn('isBlocked', block)
        self.assertNotIn('alerts', block)

    def test_an_ended_series_says_so_on_its_poster(self):
        # Grayscale says something is different about this poster; the pill says what. It
        # is there whether or not anyone is pointing at the card, unlike the retention
        # below it, because it is a fact about the series rather than a detail you ask for.
        self.assertIn("className: 'tvr-card-ended', textContent: 'Ended'", self.js)
        block = self.css.split('.tvr-card-ended {')[1].split('}')[0]
        self.assertIn('left: 50%', block)
        self.assertIn('transform: translateX(-50%)', block)
        pill = self.css.split('.tvr-card-pill {')[1].split('}')[0]
        self.assertIn('bottom: 0', pill)
        self.assertRegex(block, r'bottom: \d+px', 'it sits above the retention, not on it')

    def test_a_switched_off_series_is_offered_rather_than_listed(self):
        """It raises nothing, so it is not in the section about things to do.

        But "I turned that off and forgot" is a real way to lose track of a problem, so it
        is counted and offered — and that is the one case where the band appears with
        nothing under it, because the offer is the only thing there is to show.
        """
        block = self.js.split("const alerting = visibleLibrary('alerts');")[1].split('if (attention.length')[0]
        self.assertIn('row.rule && row.rule.enabled', block)
        self.assertIn('row.rule && !row.rule.enabled', block)
        self.assertIn('if (attention.length || sleeping.length) {', self.js)
        self.assertIn("`Show ${plural(sleeping.length, 'disabled series')}`", self.js)

    def test_opening_a_series_opens_its_settings(self):
        """A read-only card with an Edit button was a step that only ever had one answer.

        Removing the button without moving the settings left a connected series with no
        way in at all, which is the regression this pins.
        """
        self.assertIn('openEditor(rule, rule ? undefined : series);', self.js)
        self.assertNotIn('renderOneDetail', self.js)
        # The card is outside the editor now, so closing the pane goes through the broker
        # the entry hands it rather than assigning the editor's own state.
        self.assertIn('if (isOpen(rule, series)) { closeEditor();', self.js)

    def test_each_column_scrolls_within_something(self):
        """overflow:auto with nothing to overflow moves the whole page instead.

        The height comes from the window now rather than from a guess about how much of it
        the WebGUI had already used: body is the viewport, the app is a column inside it,
        the banners take what they need and the shell takes the rest.
        """
        self.assertRegex(self.css, r'html, body \{ height: 100%')
        self.assertRegex(self.css, r'#tv-retention \{ height: 100%')
        # A flex child refuses to shrink below its contents without being told.
        self.assertRegex(self.css, r'\.tvr-shell \{[^}]*flex: 1; min-height: 0')
        for selector in (r'\.tvr-side \{ height: 100%', r'\.tvr-main \{ height: 100%; overflow-y: auto',
                         r'\.tvr-details \{ position: sticky; top: 0; max-height: 100%; overflow-y: auto'):
            self.assertRegex(self.css, selector)

    def test_the_window_is_the_application(self):
        """No frame, and no headroom left for a page that is no longer above it.

        The rounded border and the 186px subtracted from the viewport were both for living
        inside the WebGUI as a card on somebody else's page.
        """
        self.assertNotIn('calc(100vh - 186px)', self.css)
        shell = self.css.split('.tvr-shell { display: grid;')[1].split('}')[0]
        self.assertNotIn('border:', shell)
        self.assertNotIn('border-radius', shell)
        topbar = self.css.split('.tvr-topbar { display: grid;')[1].split('}')[0]
        self.assertNotIn('border-radius', topbar)

    def test_a_poster_fills_a_box_that_states_its_own_size(self):
        """It fills now, where it used to fit — and that is safe because the box is chosen.

        Fitting was the right answer while the box came from the layout: a poster squashed
        into a shape nobody picked is worse than one letterboxed in it. The sizes are hard
        values now, every one of them within a few percent of 2:3, so cropping takes a
        sliver off a poster instead of leaving a margin around every card.

        What has not changed is why the box must state its height. A height derived from
        width — through aspect-ratio, or from the grid column — is not definite for
        percentage resolution, so the image fell back to its own size and the box clipped
        the bottom off it. That cost three wrong fixes.
        """
        rule = self.css.split('\n.tvr-poster img {')[1].split('}')[0]
        # Positioned against the box's edges: its size is the box's size by construction,
        # with no alignment step to depend on and no percentage that can fail to resolve.
        self.assertIn('position: absolute', rule)
        self.assertIn('inset: 0', rule)
        self.assertIn('object-fit: cover', rule)
        self.assertIn('position: relative', self.css.split('.tvr-poster {')[1].split('}')[0])
        for box in ('.tvr-poster {', '.tvr-poster-row {', '.tvr-poster-panel {',
                    '.tvr-rules-grid .tvr-poster-row {'):
            declared = self.css.split(box)[1].split('}')[0]
            self.assertRegex(declared, r'height:\s*(\d+px|var\(--poster-h\))',
                             f'{box.strip()} does not declare a height')
        import re as regex
        declarations = regex.sub(r'/\*.*?\*/', '', self.css, flags=regex.S)
        self.assertNotIn('aspect-ratio', declarations, 'a derived height is not definite')

    def test_the_slider_sets_hard_sizes_rather_than_fitting_to_the_layout(self):
        """Five steps, two layouts, ten stated sizes.

        And the type steps with them: the grid's sizes were drawn for the middle step, so
        that is 1 and the rest move around it. The list uses the same scale, because the
        same fact should not be a different size for being on a different row.
        """
        block = self.js.split('const SCALES = {')[1].split('};')[0]
        self.assertIn('grid: [[100, 143], [125, 179], [150, 215], [175, 250], [200, 286]]', block)
        self.assertIn('list: [[25, 36], [38, 54], [50, 72], [75, 107], [100, 143]]', block)
        self.assertIn('const FONT_SCALE = [0.8, 0.9, 1, 1.1, 1.2];', self.js)
        # Numbers on the container, so moving the slider resizes what is on screen rather
        # than rebuilding it.
        self.assertIn("container.style.setProperty('--poster-w'", self.js)
        self.assertIn("applyScale($('tvr-rules'))", self.js)
        self.assertIn('id="tvr-scale"', self.html)

    def test_what_needs_attention_is_a_section_rather_than_a_filter(self):
        """A series with a problem is not hidden by being on the wrong tab.

        Which is what "alerts only" was for, and why it could sit switched off with the
        problem still there. The section shows every alerting series whatever the filters
        say — the same series appears again below, because it is still in the library.
        """
        self.assertNotIn('tvr-only-alerts', self.html)
        self.assertNotIn('tvr-only-alerts', self.js)
        self.assertNotIn('tvr-series-rollup', self.html)
        self.assertIn("'attention', 'Needs attention'", self.js)
        block = self.js.split('function visibleLibrary(mode, useSearch = true)')[1].split('\n  }')[0]
        self.assertIn("if (mode === 'alerts') {", block)
        # Search still narrows every section: it is a question rather than a filter.
        self.assertIn("if (term && !(`${series.title}", block)

    def test_the_panel_heading_is_opaque(self):
        # A translucent sticky heading lets its own contents scroll through it.
        head = self.css.split('.tvr-details-head {')[1].split('}')[0]
        self.assertIn('background: var(--tvr-bg)', head)
        self.assertIn('z-index', head)

    def test_the_library_reloads_itself_when_it_is_invalidated(self):
        """Whoever drops the copy in hand should not have to remember to fetch it again.

        A sync and a newly added rule both make it wrong. One of them cleared it and left
        the list saying "reading the stored library" until something else navigated.
        """
        self.assertIn('function forgetLibrary', self.js)
        self.assertIn('if (!libraryLoading) loadLibrary()', self.js)
        # And the two places that invalidate go through it rather than assigning null.
        import re
        dropped = re.findall(r'(?<!let )\blibrary = null\b', self.js)
        self.assertEqual(len(dropped), 1, 'library is dropped somewhere other than forgetLibrary')
        self.assertIn('function forgetLibrary() {\n    library = null;', self.js)

    def test_the_layout_choice_is_remembered(self):
        # A preference about looking, not a setting about behaviour: it belongs to the
        # browser rather than to the plugin's settings document.
        self.assertIn("let layout = remembered('layout', 'list')", self.js)
        self.assertIn("remember('layout', mode)", self.js)
        self.assertIn('catch (error)', self.js.split('const remember =')[1].split('\n')[0]
                      + self.js.split('const remembered =')[1].split('};')[0])

    def test_the_list_scrolls_rather_than_the_column_around_it(self):
        self.assertRegex(self.css, r'\.tvr-series-list \{ height: 100%; overflow-y: auto')
        self.assertRegex(self.css, r'#tvr-view-series-all \{ height: 100%; display: flex')

    def test_a_new_series_starts_on_custom(self):
        """A preset is a decision to share values with other series.

        Adding one is usually not that, and defaulting to the first preset made the
        decision quietly — the values came from somewhere the form never mentioned.
        """
        block = self.js.split("const presetSelect = el('select');")[1].split('presetSelect.value')[0]
        self.assertLess(block.index('Custom — values for this series only'),
                        block.index('getSettings().profiles'),
                        'Custom is not the first option')
        self.assertIn("profile_id: '',", self.js)

    def test_unmonitoring_outside_the_window_is_not_a_choice(self):
        """A run does it regardless, so offering it only chose now or within a day.

        Off by default, that day was one Sonarr spent fetching episodes the run would
        delete. It is stated on the panel instead, because it still happens.
        """
        self.assertNotIn('unmonitorOut', self.js)
        self.assertIn('unmonitor_outside: true', self.js)
        self.assertIn('will be unmonitored so Sonarr stops fetching them', self.js)

    def test_monitoring_inside_the_window_is_chosen_episode_by_episode(self):
        """All or nothing was the wrong question for what is inside the window.

        Which episodes Sonarr should chase is a per-episode answer, so the toggle opens a
        tree of the window's own seasons and episodes, checked where Sonarr monitors them
        now, and only what differs is sent.
        """
        self.assertIn('function monitorTree', self.js)
        self.assertIn("api('episodes'", self.js)
        self.assertIn("api('set-monitored'", self.js)
        # Three states where "some of this" is a real answer, two where it is not.
        self.assertIn('box.indeterminate = on > 0 && off > 0', self.js)
        self.assertIn('if (wanted === !!episode.monitored) return;', self.js)

    def test_removal_offers_to_set_monitoring_before_the_series_goes(self):
        """After it leaves, the plugin stops having an opinion about this series.

        So leaving is the moment to put Sonarr's flags where you want them, across every
        season rather than the keep window's — the window is about to stop mattering.
        """
        block = self.js.split('function deleteSeries')[1].split('// -- schedule')[0]
        self.assertIn('Set monitoring in Sonarr before it goes', block)
        self.assertIn('monitorTree(data.seasons, {})', block)
        self.assertIn("api('set-monitored'", block)

    def test_there_is_no_mass_edit(self):
        """Bulk change already exists twice, in safer shapes.

        A preset moves every series pointing at it; a default moves every series
        inheriting it. Each is one edit with a blast radius you can name. A set of ticked
        boxes is not — a stale tick is invisible, and this app deletes things.
        """
        self.assertNotIn('renderMassEdit', self.js)
        self.assertNotIn('tvr-select-shown', self.html)
        self.assertNotIn('tvr-select-none', self.html)
        self.assertNotIn('tvr-pick', self.js.split('function libraryCard')[1].split('function ')[0])
        self.assertIn('const isOpen = (rule, series) =>', self.js)

    def test_the_list_offers_a_layout(self):
        for identifier in ('tvr-layout-list', 'tvr-layout-grid'):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('tvr-rules-grid', self.css)

    def test_a_poster_card_carries_what_it_knows_on_the_artwork(self):
        """At rest a wall of posters; what it knows appears where you point.

        Alerts top right stacked by severity, what the next run would do down the left as
        a badge and a number, and the retention along the bottom on hover.
        """
        block = self.js.split('function gridCard')[1].split('function listCard')[0]
        self.assertIn('alertMarks(rule)', block)
        self.assertIn('changeMarks(rule, plan)', block)
        self.assertIn('retentionPill(rule)', block)
        self.assertRegex(self.css, r'\.tvr-card-alerts \{ position: absolute; top: 4px; right: 4px')
        self.assertRegex(self.css, r'\.tvr-card-changes \{ position: absolute; top: 4px; left: 4px')
        self.assertRegex(self.css, r'\.tvr-rules-grid \.tvr-rule:hover \.tvr-card-pill \{ opacity: 1')

    def test_the_sonarr_link_is_only_where_one_series_is_in_front_of_you(self):
        # On every card it was three thousand links to nowhere anyone was going.
        for block in ('function gridCard', 'function listCard'):
            body = self.js.split(block)[1].split('\n  }')[0]
            self.assertNotIn('sonarrLink', body)
        self.assertIn('sonarrLink(Object.assign({}, rule,', self.js)

    def test_the_title_row_gives_the_link_and_the_switch_their_own_room(self):
        # Butted against the title, the link read as part of the name.
        self.assertIn('.tvr-identity-title { display: flex; align-items: center; gap: 8px;', self.css)

    def test_a_change_badge_is_as_wide_as_its_own_number(self):
        """Grid items stretch to the widest in the column unless told not to.

        So a badge reading "4" was drawn as wide as one reading "186", and the poster
        carried a column of mostly empty pills.
        """
        block = self.css.split('.tvr-card-changes {')[1].split('}')[0]
        self.assertIn('justify-items: start', block)

    def test_the_banner_sits_under_the_series_it_names(self):
        """Two headings, and each says its own thing.

        The pane's is thin, fixed and names what the pane holds; the form's sits under the
        series it is about. One heading doing both changed under the reader whenever the
        same pane went from adding to editing.
        """
        self.assertIn("el('h3', { textContent: 'Series details' })", self.js)
        top = self.js.split('top.append(identity);')[1].split('body.append(\n')[0]
        self.assertIn('tvr-form-banner', top)
        self.assertIn("existing ? 'Edit series' : 'Add series'", top)

    def test_an_unmanaged_series_opens_as_details_before_offering_the_form(self):
        editor = module_js('series-editor.js')
        self.assertIn("textContent: 'Add to Retention'", editor)
        block = editor.split("if (!editing.existing && !editing.expanded)")[1] \
                      .split("const actions =")[0]
        self.assertIn('context.formBanner.hidden = true', block)
        self.assertIn('body.hidden = true', block)
        self.assertIn('editing.expanded = true', block)

    def test_retention_conditions_are_any_or_all_in_one_row(self):
        presets = module_js('presets.js')
        self.assertIn("className: 'tvr-condition-grid'", presets)
        for label in ("field('Keep', combine)", "field('Episodes', episodes)",
                      "field('Seasons', seasons)", "field('Age', days)"):
            self.assertIn(label, presets)
        self.assertIn("['any', 'Any']", presets)
        self.assertIn("['all', 'All']", presets)
        self.assertNotIn("['earliest'", presets)
        self.assertNotIn("['latest'", presets)
        self.assertIn('24w or 1y', presets)
        self.assertNotIn("placeholder: '30d'", presets)
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', self.css)
        self.assertIn('.tvr-condition-grid input,', self.css)
        self.assertIn('.tvr-condition-grid select { min-width: 0;', self.css)
        self.assertIn("unitDays = { '': 1, d: 1, w: 7, m: 30, y: 365 }", presets)
        self.assertIn('<= 36500', presets)
        self.assertIn("$('tvr-dialog-ok').disabled = !valid", presets)
        self.assertIn('if (!context.conditions.valid())', presets)

    def test_an_ended_disabled_series_can_arm_one_shot_reenable(self):
        editor = module_js('series-editor.js')
        self.assertIn('rule.auto_reenable', editor)
        self.assertIn('existing && series.ended && !rule.enabled', editor)
        self.assertIn('auto_reenable: context.autoReenable.input.checked', editor)

    def test_re_reading_a_series_is_an_icon_with_the_other_things_it_can_be_told(self):
        """Beside the switch, not on a line of prose under the facts.

        Offered while adding too: there is no rule to check, but the counts behind the
        panel come straight from Sonarr, and a series being added is the one most likely
        to have moved since the catalogue was read.
        """
        # In the pane's own bar, and left of the close, which is what the order says.
        self.assertIn("title: 'Re-read this series from Sonarr'", self.js)
        self.assertIn("el('span', { className: 'tvr-spacer' }), refresh, close);", self.js)
        self.assertIn('refresh.addEventListener(\'click\', () => context.reread(refresh));', self.js)
        self.assertIn('`Last refreshed: ${stamp ? ago(stamp) : \'never\'}`', self.js)
        block = self.js.split('const reread = (button) =>')[1].split('const controls =')[0]
        self.assertIn('if (existing) {', block)          # a rule is re-checked
        self.assertIn('refreshCounts();', block)         # everything else is re-counted
        self.assertNotIn('tvr-linky', self.js)           # the text link it replaced
        self.assertNotIn('tvr-linky', self.css)

    def test_the_form_does_not_shadow_the_map_of_readings(self):
        """`monitoring` is the module's readings by rule id, and was also the control.

        Inside the form the control won, so reading `monitoring[rule.id]` above its own
        declaration was a dead-zone reference: the panel threw before appending anything
        and every connected series opened to an empty pane, while a new one was fine —
        because only a saved rule has a reading to look up.
        """
        form = self.js.split('function ruleForm')[1].split('// -- the details pane')[0]
        self.assertNotIn('const monitoring =', form)
        self.assertIn('const monitorMode = options(', form)
        # The readings arrive as an accessor now: the entry replaces the map wholesale on
        # every health reading, so the editor may not hold the object it was built with.
        self.assertIn('const reading = existing ? (getMonitoring()[rule.id] || {}) : {};', form)

    def test_only_the_settings_scroll(self):
        """The series and its plan are what the settings are being changed *about*.

        Scrolled away to reach a keep value, the plan they move was never on screen while
        it moved — and the pane's own heading went with them.
        """
        self.assertIn('const context = editing.build(body, top);', self.js)
        block = self.css.split('.tvr-series-shell.open .tvr-details {')[1].split('}')[0]
        self.assertIn('grid-template-rows: auto auto minmax(0, 1fr) auto', block)
        self.assertIn('overflow: hidden', block)
        self.assertIn('.tvr-series-shell.open .tvr-details-body { overflow-y: auto', self.css)

    def test_the_next_run_lines_follow_the_window_being_typed(self):
        # Answered against the draft by the call that already counts the scope, so the
        # lines move with the keep value rather than describing the last save.
        self.assertIn('if (counts.plan) sayPlan(counts.plan);', self.js)
        self.assertIn('planLines.replaceChildren(', self.js)

    def test_an_unsaved_edit_survives_a_look_at_another_series(self):
        """Clicking a second poster to check something is browsing, not abandoning.

        Nothing was saved, so nothing should be lost — but the draft belongs to the
        library, and leaving it drops them rather than holding a second, invisible copy of
        the settings.
        """
        self.assertIn('const drafts = new Map();', self.js)
        self.assertIn('drafts.set(context.draftKey, now)', self.js)
        self.assertIn('drafts.delete(context.draftKey)', self.js)
        self.assertIn('forgetDrafts(); closeEditor(); renderDetails();', self.js)

    def test_the_dirty_check_compares_against_what_was_saved(self):
        """Not against what the pane opened showing.

        With a draft put back, the two are different things: taking the baseline after the
        restore calls the draft the saved state, and Update sits disabled over changes
        nobody has written.
        """
        block = self.js.split('const saved = formState();')[1].split('return { presetSelect')[0]
        self.assertIn('drafts.get(draftKey)', block)
        self.assertIn('button.disabled = !valid || (editing.existing && now === context.saved)', self.js)

    def test_a_different_series_starts_at_its_own_top(self):
        # The pane is the scroller, and replacing its contents leaves the position the
        # last series had put it in.
        block = self.js.split('function openEditor')[1].split('\n  }')[0]
        self.assertIn("$('tvr-details').scrollTop = 0", block)

    def test_nothing_still_reaches_for_the_tab_bar_that_was_removed(self):
        # querySelector returns null and .click() on null throws, so each of these was a
        # dead button waiting for someone to press it.
        self.assertNotIn('.tvr-tabs', self.js)


class Theme(unittest.TestCase):
    """Light and dark, and the reader's choice winning over their system's."""

    @classmethod
    def setUpClass(cls):
        source = ROOT / 'src'
        cls.css = (source / 'assets' / 'app.css').read_text(encoding='utf-8')
        cls.js = interface_js()
        cls.html = (source / 'include' / 'interface.html').read_text(encoding='utf-8')

    def test_the_page_sets_its_own_type_and_colour(self):
        """It used to inherit both from the WebGUI.

        Standing on its own it inherited a serif on white with dark panels drawn over it,
        which is what happens when nothing above you is setting anything.
        """
        body = self.css.split('\nbody {')[1].split('}')[0]
        for named in ('background: var(--tvr-bg)', 'color: var(--tvr-fg)',
                      'font-family: var(--tvr-font)'):
            self.assertIn(named, body)

    def test_no_colour_is_defined_only_in_the_dark(self):
        """Every token gets a light value on bare :root first.

        A colour whose only definition lives inside a media query has no value at all for
        a reader who is not in that query.
        """
        import re
        light = set(re.findall(r'(--tvr-[a-z-]+):', self.css.split(':root {')[1].split('}')[0]))
        dark = set(re.findall(r'(--tvr-[a-z-]+):',
                              self.css.split(':root[data-theme="dark"] {')[1].split('}')[0]))
        self.assertEqual(dark - light, set(), 'defined dark but never light')

    def test_a_choice_beats_the_system_in_both_directions(self):
        """`prefers-color-scheme` alone cannot be overridden by an attribute that loses to it.

        Someone on a dark desktop must still be able to choose light, which is why the
        media-query block is guarded rather than unconditional.
        """
        self.assertIn(':root:not([data-theme="light"])', self.css)
        self.assertIn(':root[data-theme="dark"]', self.css)

    def test_the_control_offers_following_the_system_as_an_answer(self):
        # Three states: auto is a real choice, not the absence of one.
        block = self.js.split('const THEMES = [')[1].split('];')[0]
        for state in ("'auto'", "'light'", "'dark'"):
            self.assertIn(state, block)
        self.assertIn("delete document.documentElement.dataset.theme", self.js)
        self.assertIn('id="tvr-theme"', self.html)

    def test_text_on_a_filled_badge_follows_the_theme(self):
        # A badge filled with the warning colour had #1a1a1a on it, which assumed the page
        # behind it was dark.
        self.assertIn('--tvr-on-fill', self.css)
        for assumed in ('color: #1a1a1a', 'background: var(--tvr-info); color: #fff'):
            self.assertNotIn(assumed, self.css)


class Bands(unittest.TestCase):
    """The three sections, and what each one lets you put away."""

    @classmethod
    def setUpClass(cls):
        source = ROOT / 'src'
        cls.js = interface_js()
        cls.css = (source / 'assets' / 'app.css').read_text(encoding='utf-8')
        cls.html = (source / 'include' / 'interface.html').read_text(encoding='utf-8')

    def test_hiding_a_severity_does_not_stop_it_counting(self):
        """The toggles are about what you want in front of you, not about what is true.

        The header total and the card badges answer a different question, and a control
        that quietly changed both would be an acknowledgement wearing a filter's clothes.
        """
        block = self.js.split('const bandShows = (key)')[1].split('\n\n')[0]
        self.assertIn("remembered(`show.${key}`", block)
        # Nothing in the filter path touches the counts.
        applied = self.js.split('function applyBandFilters')[1].split('\n  }')[0]
        self.assertNotIn('seriesAlertList', applied)
        self.assertNotIn('setBadge', applied)

    def test_an_error_can_never_be_put_away(self):
        # One stops a series from running, and hiding it would not stop that being true —
        # the same reason an error can never be acknowledged.
        block = self.js.split('const ATTENTION_FILTERS = [')[1].split('];')[0]
        self.assertIn("'warning'", block)
        self.assertIn("'notice'", block)
        self.assertNotIn("'error'", block)

    def test_a_row_carrying_no_named_kind_is_never_filtered_out(self):
        """An error in the attention band carries neither warning nor notice.

        Filtering on "carries none of the switched-on kinds" would have hidden exactly the
        rows that matter most.
        """
        applied = self.js.split('function applyBandFilters')[1].split('\n  }')[0]
        self.assertIn('return !carried.length || carried.some(', applied)

    def test_the_scheduled_band_offers_every_kind_of_work_it_holds(self):
        block = self.js.split('const SCHEDULED_FILTERS = [')[1].split('];')[0]
        for label in ('Series removals', 'Episode deletions', 'Monitoring changes'):
            self.assertIn(label, block)

    def test_a_toggle_appears_only_for_a_kind_that_is_present(self):
        # Offering to hide nothing is a control that can only disappoint.
        block = self.js.split('function bandFilterSwitches')[1].split('\n  }')[0]
        self.assertIn('if (!count) return;', block)
        self.assertIn('`${label} (${count})`', block)

    def test_grey_means_finished_and_that_it_matters(self):
        """Always in the two bands above; in the library, only if the series is yours.

        One you watch has a retention decision behind it that will now only ever shrink.
        One you do not is something Sonarr happens to hold, and greying three thousand of
        those would be a different page.
        """
        self.assertIn("if (series.ended && (row.section !== 'all' || rule)) marks.push('ended');",
                      self.js)
        self.assertIn("cardsInto(band.box, shown, 'attention')", self.js)
        self.assertIn("cardsInto(band.box, rows, 'all')", self.js)

    def test_the_details_pane_is_always_there(self):
        """Selecting a series used to open it, which narrowed the list and reflowed the
        grid — moving the card that had just been clicked out from under the pointer."""
        self.assertIn('<aside id="tvr-details" class="tvr-details"></aside>', self.html)
        self.assertIn("shell.classList.add('open');", self.js)
        self.assertIn('Select a series to view or modify its details.', self.js)
        self.assertIn('.tvr-details-idle', self.css)
