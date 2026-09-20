"""Structural checks over the shipped interface, read as source.

A test belongs in this file only if it fails when a user would notice something
broken: a dangling reference, a control that silently does not save, a
destructive path that stops being guarded, a markup invariant nobody can
eyeball. Appearance is not that. A check asserting a colour, a word, an icon
shape or where a control sits freezes a decision rather than protecting one, and
the suite became the most-churned file in the repository by collecting them.
Behaviour that can be exercised belongs in tests/frontend/, which runs the real
module graph instead of matching its text.
"""

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
    """
    return '\n'.join(module_js(name) for name in module_names())


def module_names() -> list[str]:
    """The shipped modules, entry first, then the rest sorted."""
    rest = sorted(p.name for p in ASSETS.glob('*.js') if p.name != ENTRY)
    return [ENTRY] + rest


def function_body(source: str, name: str) -> str:
    """The body of a named function declaration, found by its own indentation.

    Matching the declaration's own indent and closing on the brace at that same indent
    gives the same body whether the function sits at module scope or nested, which is
    what lets these checks survive a file split unchanged.
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


class Sources(unittest.TestCase):
    """Reads the shipped interface once for each group of checks below."""

    @classmethod
    def setUpClass(cls):
        source = ROOT / 'src'
        cls.css = (source / 'assets' / 'app.css').read_text(encoding='utf-8')
        cls.js = interface_js()
        cls.entry = module_js(ENTRY)
        cls.html = (source / 'include' / 'interface.html').read_text(encoding='utf-8')


class Markup(Sources):
    """Invariants between the script and the document it addresses."""

    def test_the_hidden_attribute_is_forced_to_win(self):
        # Guards the class of bug where an author `display` rule defeats `hidden`.
        self.assertRegex(self.css, r'#tv-retention \[hidden\][^{]*\{[^}]*display:\s*none\s*!important')

    def test_the_busy_overlay_starts_hidden(self):
        self.assertRegex(self.html, r'id="tvr-busy"[^>]*hidden')

    def test_every_element_the_script_hides_exists_in_the_markup(self):
        for identifier in set(re.findall(r"\$\('([a-z0-9-]+)'\)\.hidden", self.js)):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is toggled but not in the markup')

    def test_every_element_the_script_addresses_exists_in_the_markup(self):
        for identifier in sorted(set(re.findall(r"\$\('([a-z0-9-]+)'\)", self.js))):
            self.assertIn(f'id="{identifier}"', self.html, f'{identifier} is addressed but not in the markup')

    def test_no_two_elements_share_an_id(self):
        """A duplicate id is a lookup that silently finds the wrong element.

        The Test Mode select was given the banner's id, so reading the setting read a div
        and writing it wrote to nothing.
        """
        found = re.findall(r'id="([a-z0-9-]+)"', self.html)
        duplicates = sorted({name for name in found if found.count(name) > 1})
        self.assertEqual(duplicates, [])

    def test_every_view_is_inside_the_main_column(self):
        """A view outside `<main>` lays out below the shell rather than in it.

        `showView` still finds it and still unhides it, which is why this looks like a
        styling problem and is not one: the markup is wrong and every id check passes
        anyway. The dialog and the busy overlay are deliberately outside and stay there.
        """
        inside = self.html.split('<main')[1].split('</main>')[0]
        after = self.html.split('</main>')[1]
        for view in re.findall(r'<section id="tvr-view-([a-z-]+)"', self.html):
            self.assertIn(f'id="tvr-view-{view}"', inside, f'{view} renders outside <main>')
        self.assertNotIn('<section id="tvr-view-', after, 'a view was left after </main>')

    def test_every_tab_has_a_panel_and_every_panel_has_a_tab(self):
        wanted = set(re.findall(r'data-view="([a-z-]+)"', self.html))
        views = set(re.findall(r'<section id="tvr-view-([a-z-]+)"', self.html))
        # The three series views share one section, narrowed by filter.
        views |= {'series-connected', 'series-unconnected'}
        self.assertEqual(wanted, views, 'a sidebar item with no view, or a view nothing reaches')

    def test_the_tab_list_is_not_a_second_copy_of_the_markup(self):
        """Removing a tab left a stale name in a hand-kept list.

        `$('tvr-panel-schedule')` was null, setting `.hidden` on it threw, and the loop
        that shows one panel and hides the rest died at that point — so Settings, Job
        History, Live Log and Help, all listed after it, simply stopped appearing.
        """
        self.assertRegex(self.js, r"const VIEWS = \[\.\.\.document\.querySelectorAll\('\.tvr-side \[data-view\]'\)\]")

    def test_braces_and_parentheses_balance(self):
        for pair in ('{}', '()', '[]'):
            self.assertEqual(self.js.count(pair[0]), self.js.count(pair[1]),
                             f'unbalanced {pair} across the shipped modules')

    def test_the_script_is_not_prefixed_by_a_stray_fragment(self):
        # A build-time edit once prepended a fragment above the opening comment, which
        # broke the whole file.
        self.assertTrue(self.entry.lstrip().startswith('/* TV Retention web UI.'))
        self.assertEqual(self.js.count("function render() {"), 1)

    def test_every_module_constant_used_is_declared(self):
        """Catch a constant left behind when an edit replaced the block that declared it.

        A syntax check cannot see this: `PILL_ENDED is not defined` is a runtime error, and
        it broke the whole page once because a batched edit dropped the declaration while
        leaving two uses behind. Only SCREAMING_SNAKE names are considered — that is the
        shape every constant here has, and it keeps prose like "TVDB" out of the comparison.
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


class Safety(Sources):
    """Test Mode, queued destruction, exclusions and the monitoring contract."""

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

    def test_nothing_on_a_card_acts_immediately(self):
        # Every destructive control queues; only a run applies. Undo is the safety net.
        self.assertNotRegex(self.js, r"api\('remove-series'")
        self.assertIn('Undo', self.js)

    def test_removing_a_series_is_queued_and_asks_for_the_right_word(self):
        """Two different consequences, two different words, and neither happens at once.

        The application never deletes a series itself: the destructive options ask Sonarr
        to, so Sonarr's own recycle bin and bookkeeping apply.
        """
        self.assertIn("'delete-series': 'DELETE'", self.js)
        self.assertIn("'delete-series-files': 'DELETE ALL'", self.js)
        self.assertIn('Queue removal', self.js)
        self.assertIn('function queuedBanner', self.js)
        self.assertRegex(self.js, r'Ask Sonarr to delete the series')

    def test_the_run_button_says_what_pressing_it_would_do(self):
        """Red is reserved for a fault that stops the whole run.

        One broken series among thirty-five healthy ones is skipped, not a reason to call
        the button disabled.
        """
        block = self.js.split('const RUN_STATES = {')[1].split('};')[0]
        self.assertIn("live: ['Run'", block)
        self.assertIn("test: ['Run Test'", block)
        self.assertIn("blocked: ['Disabled'", block)
        state = self.js.split('function runState()')[1].split('\n  }')[0]
        self.assertIn('system.some((alert) => alert.blocking)', state)
        self.assertNotIn('isBlocked', state, 'one broken series must not disable the button')
        # Blocked stays pressable: it is the shortest route to the reason.
        self.assertIn("if (runState() === 'blocked') return void showEverythingNeedingAttention();",
                      self.js)

    def test_the_run_button_follows_both_things_that_change_it(self):
        """Test Mode and the alerts move independently, and it missed both.

        `snapshot.test_mode` is refreshed only by a full snapshot call, so turning Test
        Mode off and saving left the button describing the mode the page had loaded with —
        at exactly the moment somebody is reading it to see whether it will delete
        something.
        """
        state = self.js.split('const testMode = ()')[1].split('};')[0]
        self.assertIn('(settings || {}).schedule', state)
        code = [line for line in self.js.splitlines()
                if 'snapshot.test_mode' in line and not line.strip().startswith('//')]
        self.assertEqual(code, [],
                         'read anywhere but the helper, it is stale the moment settings are saved')
        self.assertIn('!!(snapshot || {}).test_mode', state, 'the helper still needs a fallback')
        self.assertIn('function renderRunButton() {', self.js)
        applied = self.js.split('function applyAlerts')[1].split('\n  }')[0]
        self.assertIn('renderRunButton()', applied)

    def test_the_run_button_hides_only_on_a_complete_answer(self):
        # Hiding it on a stale or partial reading would be a promise the cache cannot keep.
        self.assertRegex(self.js, r'plan\.trustworthy && !plan\.actionable')

    def test_a_queued_removal_is_scheduled_even_with_the_rule_switched_off(self):
        """Removals are the one thing that ignores the enabled flag.

        Hiding the only destructive thing still going to happen, because the rule that no
        longer runs is switched off, would be exactly the wrong way round.
        """
        block = self.js.split('function scheduledFor(rule)')[1].split('\n  }')[0]
        self.assertLess(block.index('queuedRemoval(rule)'), block.index('rule.enabled'))
        self.assertIn('if (!rule.enabled || isBlocked(rule.id)) return false;', block)

    def test_a_series_that_cannot_be_used_is_refused_on_save(self):
        # A series Sonarr has no folder for cannot take a rule, and the save is the only
        # place that check can be evaded.
        self.assertIn('series.selectable === false', self.js)
        self.assertIn('cannot be used', self.js)

    def test_there_is_no_mass_edit(self):
        """Bulk change already exists twice, in safer shapes.

        A preset moves every series pointing at it; a default moves every series
        inheriting it. Each is one edit with a blast radius you can name. A set of ticked
        boxes is not — a stale tick is invisible, and this application deletes things.
        """
        self.assertNotIn('renderMassEdit', self.js)
        self.assertNotIn('tvr-select-shown', self.html)
        self.assertNotIn('tvr-select-none', self.html)
        self.assertNotIn('tvr-pick', self.js.split('function libraryCard')[1].split('function ')[0])

    def test_a_new_series_starts_on_custom(self):
        """A preset is a decision to share values with other series.

        Adding one is usually not that, and defaulting to the first preset made the
        decision quietly — the retention values came from somewhere the form never named.
        """
        block = self.js.split("const presetSelect = el('select');")[1].split('presetSelect.value')[0]
        self.assertLess(block.index('Custom — values for this series only'),
                        block.index('getSettings().profiles'),
                        'Custom is not the first option')
        self.assertIn("profile_id: '',", self.js)

    def test_retention_conditions_validate_their_units_and_bounds(self):
        # A wrong unit map is a wrong keep window, which is over-deletion.
        presets = module_js('presets.js')
        self.assertIn("unitDays = { '': 1, d: 1, w: 7, m: 30, y: 365 }", presets)
        self.assertIn('<= 36500', presets)
        self.assertIn("['any', 'Any']", presets)
        self.assertIn("['all', 'All']", presets)
        self.assertIn("$('tvr-dialog-ok').disabled = !valid", presets)
        self.assertIn('if (!context.conditions.valid())', presets)

    def test_unmonitoring_outside_the_window_is_not_a_choice(self):
        """A run does it regardless, so offering it only chose now or within a day.

        Off by default, that day was one Sonarr spent fetching episodes the run would
        delete. It is stated on the panel instead, because it still happens.
        """
        self.assertNotIn('unmonitorOut', self.js)
        self.assertIn('unmonitor_outside: true', self.js)
        self.assertIn('will be unmonitored so Sonarr stops fetching them', self.js)

    def test_monitoring_inside_the_window_is_chosen_episode_by_episode(self):
        # Only what differs from Sonarr is sent, so an unopened tree writes nothing.
        self.assertIn('function monitorTree', self.js)
        self.assertIn("api('episodes'", self.js)
        self.assertIn("api('set-monitored'", self.js)
        self.assertIn('box.indeterminate = on > 0 && off > 0', self.js)
        self.assertIn('if (wanted === !!episode.monitored) return;', self.js)

    def test_excluding_and_monitoring_do_not_share_a_tree(self):
        """A tick means "never touch this" in one and "Sonarr should have this" in the
        other. The day they share a code path, one of them is wrong.
        """
        self.assertIn('function exclusionTree(', self.js)
        self.assertIn('function monitorTree(', self.js)
        picker = function_body(self.js, 'exclusionTree')
        self.assertNotIn('monitorTree(', picker)
        # The exclusion list is read back by season and episode *number*, and carries no
        # trace of a monitored flag: ids do not survive a series being re-added.
        exclusions = picker.split('picked: ()')[1].split('return found;')[0]
        self.assertNotIn('monitored', exclusions)
        self.assertNotIn('episode_id', exclusions)
        # The monitoring half reads back ids and only the ones that moved.
        moved = picker.split('monitoring: ()')[1]
        self.assertIn('episode_id', moved)
        self.assertIn('wanted === entry.was', moved)

    def test_the_exclusion_picker_offers_sonarrs_flag_as_sonarr_has_it(self):
        """Pre-filled from Sonarr, not from what we would like.

        An episode somebody already unmonitored by hand has to read that way, or the
        dialog proposes to re-monitor it the moment it is opened.
        """
        picker = function_body(self.js, 'exclusionTree')
        self.assertIn('checked: !!episode.monitored', picker)
        self.assertIn('if (!entry.episode.has_file && !entry.episode.air_date) return;', picker)

    def test_a_whole_season_entry_is_not_inferred_from_ticking_every_episode(self):
        """The two claims differ, and only one of them covers episodes that do not exist.

        Ticking every episode says "these"; the season box says "this season, including
        what has not aired". Reading the display box back would silently promote the first
        into the second the moment a season happened to be fully ticked.
        """
        picker = function_body(self.js, 'exclusionTree')
        stored = picker.split('picked: ()')[1].split('return found;')[0]
        self.assertIn('if (row.whole())', stored)
        self.assertNotIn('row.box.checked', stored)

    def test_the_picker_names_every_reason_an_episode_can_be_excluded(self):
        """A fourth reason added to core would be shown as a pattern that has no text.

        Adding a reason server-side and forgetting the interface is silent: the episode is
        greyed out, and the line saying why is blank.
        """
        import core
        self.assertEqual(sorted(core.EXCLUSION_REASONS),
                         ['air-date', 'episode', 'folder', 'manual', 'season', 'specials'])
        named = set(re.findall(r"^\s*'?([a-z-]+)'?\s*:\s*\(",
                               self.js.split('EXCLUDED_WHY = {')[1].split('};')[0], re.M))
        # `manual` is the one the picker does not explain, because it is the one you did.
        self.assertEqual(named, set(core.EXCLUSION_REASONS) - {'manual'})

    def test_an_error_can_never_be_acknowledged_or_suppressed(self):
        """Hiding "this series will not run" does not stop it being true."""
        import alerts as alert_module
        from core import BLOCKING_KINDS
        for kind in BLOCKING_KINDS:
            self.assertTrue(alert_module.KINDS[kind]['blocking'])
        error = alert_module.make('unmatched', rule_id='r1')
        self.assertFalse(alert_module.may_acknowledge(error))
        notice = alert_module.make('ended-expired', rule_id='r1')
        self.assertTrue(alert_module.may_acknowledge(notice))
        # And the interface offers it on exactly the same terms.
        self.assertIn("alert.severity !== 'error' && !alert.blocking", self.js)
        self.assertNotIn('tvr-alert-ack', self.html)
        self.assertNotIn('alerts.acknowledge', self.js)
        self.assertIn("alert.kind === 'no-recycle-bin'", self.js)

    def test_hiding_a_severity_does_not_stop_it_counting(self):
        """A display filter must not become an acknowledgement wearing a filter's clothes."""
        block = self.js.split('const bandShows = (key)')[1].split('\n\n')[0]
        self.assertIn("remembered(`show.${key}`", block)
        applied = self.js.split('function applyBandFilters')[1].split('\n  }')[0]
        self.assertNotIn('seriesAlertList', applied)
        self.assertNotIn('setBadge', applied)

    def test_an_error_can_never_be_put_away(self):
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

    def test_backup_uses_staging_and_explicit_activation(self):
        settings = module_js('settings.js')
        self.assertIn("operation: 'stage'", settings)
        self.assertIn("operation: 'activate'", settings)
        self.assertNotIn("operation: 'restore'", settings)
        self.assertIn('stopPolling()', module_js('app.js'))
        self.assertIn('discardSettingsDrafts()', module_js('app.js'))

    def test_automatic_search_is_removed_but_legacy_setting_remains_readable(self):
        """Sonarr RSS owns discovery; old settings still load without doing a search."""
        worker = (ROOT / 'src' / 'worker' / 'main.py').read_text(encoding='utf-8')
        migration = (ROOT / 'src' / 'worker' / 'migrate.py').read_text(encoding='utf-8')
        settings = module_js('settings.js')
        self.assertNotIn('search_episodes', worker)
        self.assertNotIn('tvr-auto-search', self.html)
        self.assertNotIn('SEARCH_QUESTION', settings)
        self.assertIn('search_after_monitor', migration)
        self.assertIn('Object.assign({}, settings().automation || {}, {', settings)


class Wiring(Sources):
    """Nothing offered on a page may go unsaved, and nothing offered on the bridge unasked."""

    def _panel(self, view):
        return self.html.split(f'id="tvr-view-{view}"')[1].split('</section>')[0]

    MOUNTS = {
        'tvr-auto-monitoring': "questionInputs['monitoring.",
        'tvr-auto-persistence': "questionInputs['persistence.",
        'tvr-air-unresolved': "questionInputs['air.unresolved']",
        'tvr-air-still': "questionInputs['air.still_unresolved']",
        'tvr-air-providers': 'providers: airOrder',
        'tvr-exclude-folders': 'folderPhrases()',
        'tvr-exclude-episodes': 'episodePhrases()',
    }

    def test_every_schedule_control_is_wired_to_save(self):
        # A control added to the panel and not to the list would silently not persist.
        wired = set(re.findall(r"'(tvr-(?:schedule-enabled|test-mode|freq|minute|hour|weekday|"
                               r"monthly-mode|monthly-day|monthly-weekday|cron|timezone|match-freq|"
                               r"match-hour|match-minute|connectivity))'", self.js))
        panel = self.html.split('id="tvr-view-settings-schedule"')[1].split('</section>')[0]
        for identifier in re.findall(r'id="(tvr-[a-z-]+)"', panel):
            if identifier in ('tvr-schedule-summary', 'tvr-match-summary', 'tvr-test-mode-label') or 'field' in identifier:
                continue
            self.assertIn(identifier, wired, f'{identifier} is on the schedule panel but never saved')

    def test_every_automation_control_is_wired_to_save(self):
        """Automation is where a control is most likely to be added and least likely to be
        noticed if it does nothing: every setting on it is global, so nothing on a series
        card contradicts a value that was never saved.
        """
        collect = self.js.split('function collectSettings()')[1].split('\n  }')[0]
        render = self.js.split('function renderSettings()')[1].split('\n  }\n')[0]
        for view in ('series-exclusions', 'settings-air-dates'):
            for identifier in re.findall(r'id="(tvr-[a-z-]+)"', self._panel(view)):
                if 'help' in identifier or 'field' in identifier:
                    continue
                # A mount point holds controls built at render time: its value reaches the
                # document through a collector rather than by id. Naming that collector
                # here is the point — a new mount cannot be added without saying what
                # saves it.
                if identifier in self.MOUNTS:
                    self.assertIn(f"'{identifier}'", self.js,
                                  f'{identifier} is a mount nothing fills')
                    self.assertIn(self.MOUNTS[identifier], collect,
                                  f'{identifier} is on {view} but nothing saves it')
                    continue
                self.assertIn(identifier, render, f'{identifier} is on {view} but never filled in')
                self.assertIn(identifier, collect, f'{identifier} is on {view} but never saved')

    def test_every_question_offered_is_a_question_read_back(self):
        """A radio group renders from one table and saves from a hand-written line.

        Adding a question to the table puts it on the page and nowhere else: it renders,
        it takes a click, it looks saved, and `collectSettings` never mentions it.
        """
        collect = self.js.split('function collectSettings()')[1].split('\n  }')[0]
        groups = re.findall(r"^    \['([a-z_]+)', 'tvr-auto-([a-z]+)', \[", self.js, re.M)
        asked = set()
        for group, _ in groups:
            block = self.js.split(f"['{group}', 'tvr-auto-")[1].split('\n    ]],')[0]
            asked |= {f'{group}.{name}' for name in re.findall(r"^      \['([a-z_]+)',", block, re.M)}
        asked |= {f'air.{name}' for name in re.findall(r"^    \['([a-z_]+)', 'tvr-air-", self.js, re.M)}
        self.assertTrue(asked, 'the question tables were not found at all')
        for key in sorted(asked):
            self.assertIn(f"questionInputs['{key}']", collect,
                          f'{key} is offered on a page but never saved')

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

    def test_background_renders_do_not_replace_a_dirty_settings_form(self):
        """A watch response must not turn an unsaved edit back into the saved value."""
        self.assertIn('let dirty = false;', self.js)
        self.assertIn('isDirty: () => dirty', self.js)
        render = self.js.split('function render()')[1].split('// -- start')[0]
        self.assertIn('if (!settingsView.isDirty())', render)
        self.assertIn('renderSettings();', render)
        saving = self.js.split('async function saveSettings(')[1].split('  }')[0]
        self.assertIn('settingsDirty(false);', saving)

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

    def test_the_library_says_used_by_the_binding_a_rule_holds(self):
        """Not by folder. A bare set of paths was wrong in both directions.

        It said "already used" about a series a different Sonarr owns, and said nothing
        about a series whose folder had moved since its rule was written.
        """
        actions = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        block = actions.split('def action_series')[1].split('\n\n\n')[0]
        self.assertIn("used = {(r['instance_id'], r['series_id'])", block)
        self.assertNotIn("{r['path'] for r in", block)

    def test_the_release_identity_is_well_formed(self):
        # A version, not a date: a build stamp cannot say whether anything changed.
        self.assertIn('`v${snapshot.version}`', self.js)
        self.assertRegex((ROOT / 'VERSION').read_text().strip(), r'^\d+\.\d+\.\d+$')
        self.assertRegex((ROOT / 'BUILD').read_text().strip(), r'^[1-9]\d*$')

    def test_the_version_constant_matches_the_version_file(self):
        """They are read from different places and must not drift.

        The file drives the package name and the image; the constant is what the interface
        shows and what a run records.
        """
        import core
        self.assertEqual(core.VERSION, (ROOT / 'VERSION').read_text().strip())


class Background(Sources):
    """Sonarr reads stay per-show and in the background, and nothing shows as fresh that is not."""

    def test_requests_cannot_hang_forever(self):
        self.assertIn('AbortController', self.js)
        self.assertIn('DEFAULT_TIMEOUT', self.js)

    def test_checks_never_block_the_page(self):
        # Background reads pass quiet, so the busy overlay is not raised for them.
        self.assertRegex(self.js, r"api\('check-rule',[^;]*, true\)")
        self.assertRegex(self.js, r"api\('progress'[^)]*, true\)")
        self.assertIn('function queueChecks', self.js)

    def test_the_page_has_a_heartbeat_that_never_blocks_it(self):
        # It must not raise the busy overlay, must stand aside for a sweep, and must not
        # re-render on a timer for its own sake.
        self.assertRegex(self.js, r"api\('watch'[^;]*, true\)")
        self.assertIn('if (document.hidden || checkRunning || checkQueue.length || pollTimer) return;', self.js)
        self.assertIn('if (stamp === watchStamp) return;', self.js)

    def test_page_freshness_is_background_coalesced_and_cache_first(self):
        actions = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        snapshot = actions.split('def action_snapshot')[1].split('\ndef action_sync')[0]
        self.assertNotIn('sync_from_sonarr', snapshot)
        sync = actions.split('def action_sync')[1].split('\ndef _sync_response')[0]
        self.assertIn('with main.run_lock()', sync)
        self.assertIn('main.PAGE_REFRESH_SECONDS', sync)
        self.assertIn("api('sync', { reason: reason || 'opened', force: !!force }, '', true)", self.js)
        self.assertIn("checks.requestFreshness(fresh ? 'manual' : 'opened', fresh)", self.js)
        self.assertIn("requestFreshness('visible')", self.js)
        self.assertIn('5 * 60 * 1000', self.js)

    def test_the_tick_asks_what_changed_whether_or_not_anyone_is_looking(self):
        # A problem the page discovers first is a notification that never fired.
        worker = ROOT / 'src' / 'worker'
        tick = (worker / 'main.py').read_text().split('def _tick_locked()')[1].split('\ndef ')[0]
        self.assertIn('sync_from_sonarr', tick)
        self.assertIn("'watch': action_watch", (worker / 'actions.py').read_text())

    def test_the_schedule_uses_container_local_time_and_names_test_passes(self):
        worker = ROOT / 'src' / 'worker' / 'main.py'
        tick = worker.read_text(encoding='utf-8').split('def _tick_locked()')[1].split('\ndef ')[0]
        self.assertIn('now = dt.datetime.now().astimezone()', tick)
        self.assertIn("'scheduled test run started; Test Mode is on and nothing will change'", tick)
        self.assertIn("'scheduled test run: {summary[\"planned\"]} planned across '", tick)
        self.assertIn('nothing changed', tick)
        self.assertIn("'scheduled run did not complete: {error}'", tick)
        status = worker.read_text(encoding='utf-8').split('def status_snapshot')[1].split('\ndef ')[0]
        self.assertIn("'last_scheduled_run': jobs.get('last_run')", status)

    def test_neither_the_list_nor_its_payload_carries_three_thousand_of_anything(self):
        self.assertNotIn('LIBRARY_LIMIT', self.js)
        self.assertIn('content-visibility: auto', self.css)
        worker = (ROOT / 'src' / 'worker' / 'actions.py').read_text()
        self.assertIn('LIST_FIELDS', worker)
        for heavy in ("'overview'", "'seasons'"):
            self.assertNotIn(heavy, worker.split('LIST_FIELDS = (')[1].split(')')[0])

    def test_a_poster_is_cached_against_the_artwork_and_not_the_series(self):
        """Sonarr's artwork path carries its last-write marker; nothing was reading it.

        `series.poster` was used as a boolean — *is there a picture* — and the proxy keyed
        its cache on the series id alone. So the first poster ever fetched was the one
        served for good, with a day of browser caching over the top of it.
        """
        self.assertIn('&stamp=${encodeURIComponent(series.poster)}', self.js)


class Regressions(Sources):
    """Named runtime failures that source text can pin and a syntax check cannot."""

    def test_the_background_flows_call_nothing_that_is_not_defined(self):
        """`renderStats()` and `planText(plan)` crashed live flows after later edits
        removed their definitions.

        renderStats aborted the check queue after its first completed rule and killed
        sweep polling on its first tick; planText broke the Run confirmation whenever a
        plan was actionable.
        """
        self.assertNotIn('renderStats()', self.js)
        self.assertNotIn('planText', self.js)

    def test_the_change_filter_is_passed_not_captured(self):
        """`shows` once leaked out of its closure and would have thrown at runtime."""
        self.assertRegex(self.js, r'function changeRows\(rule, shows\)')
        self.assertRegex(self.js, r'changeRows\(rule, \(\) => true\)')
        self.assertRegex(self.js, r'changeRows\(rule, shows\)')

    def test_the_form_does_not_shadow_the_map_of_readings(self):
        """`monitoring` is the module's readings by rule id, and was also the control.

        Inside the form the control won, so reading `monitoring[rule.id]` above its own
        declaration was a dead-zone reference: the panel threw before appending anything
        and every connected series opened to an empty pane.
        """
        form = self.js.split('function ruleForm')[1].split('// -- the details pane')[0]
        self.assertNotIn('const monitoring =', form)
        self.assertIn('const reading = existing ? (getMonitoring()[rule.id] || {}) : {};', form)

    def test_a_remembered_view_is_checked_before_it_is_used(self):
        """localStorage outlives the view it names, and a rename is not a migration.

        `media-rules` became `media-automation`, and every browser that had ever opened
        Media management went on remembering the old name. `showView` does not know which
        section was asked for, so its fallback sent you to `series-all` — in a different
        section — and clicking Media management looked like a dead button.
        """
        handler = self.js.split('[data-section-head]').pop().split('function showView')[0]
        self.assertIn('VIEWS.includes(last)', handler)
        self.assertNotIn("showView(remembered(`last.${section}`", self.js)

    def test_legacy_view_names_are_rehomed_on_load(self):
        """A browser can remember a view name across an upgrade.

        Renaming a tab without a landing map leaves an operator on the library with no
        explanation.
        """
        navigation = module_js('navigation.js')
        expected = {
            'media-stats': 'system-stats',
            'media-presets': 'series-presets',
            'media-automation': 'series-exclusions',
            'media-schedule': 'settings-schedule',
            'general-alerts': 'system-status',
            'general-connections': 'settings-connections',
            'general-air-dates': 'settings-air-dates',
            'general-safety': 'system-status',
            'general-logging': 'settings-general',
            'general-backup': 'system-backup',
        }
        for old, new in expected.items():
            self.assertIn(f"'{old}': '{new}'", navigation)
            self.assertIn('name = LEGACY_VIEWS[name] || name;', navigation)
            self.assertIn(f'data-view="{new}"', self.html)

    def test_the_library_reloads_itself_when_it_is_invalidated(self):
        """Whoever drops the copy in hand should not have to remember to fetch it again.

        A sync and a newly added rule both make it wrong. One of them cleared it and left
        the list saying "reading the stored library" until something else navigated.
        """
        self.assertIn('function forgetLibrary', self.js)
        self.assertIn('if (!libraryLoading) loadLibrary()', self.js)
        dropped = re.findall(r'(?<!let )\blibrary = null\b', self.js)
        self.assertEqual(len(dropped), 1, 'library is dropped somewhere other than forgetLibrary')
        self.assertIn('function forgetLibrary() {\n    library = null;', self.js)

    def test_a_failed_start_clears_the_overlay(self):
        self.assertRegex(self.js, r"refresh\(\)\.catch")

    def test_series_is_its_own_plural(self):
        """`plural(3, 'series')` produced "3 seriess" everywhere it was used."""
        self.assertRegex(self.js, r"const plural = .*endsWith\('s'\)")
        self.assertNotRegex(self.js, r"\$\{count\} \$\{word\}\$\{count === 1 \? '' : 's'\}")

    def test_the_ended_toggle_only_hides_what_has_no_rule(self):
        # A connected ended series is where retention matters most; hiding it would hide
        # a rule that is actively deleting.
        self.assertIn('if (hideEnded && series.ended && !rule) return;', self.js)


class Styling(Sources):
    """Only the two things a stylesheet can get wrong invisibly."""

    def test_the_webgui_defaults_this_page_has_to_undo_are_undone(self):
        """The standalone container does not load Unraid's CSS, but the resets stay.

        They are not a host dependency: without them, inherited margins, minimum widths,
        nowrap and full-width selects break the compact layouts this page is built from.
        """
        base = self.css.split('#tv-retention button {')[1].split('}')[0]
        for undone in ('margin: 0', 'min-width: 0', 'white-space: normal'):
            self.assertIn(undone, base)
        fields = self.css.split('#tv-retention input, #tv-retention select,')[1].split('}')[0]
        self.assertIn('min-height: 0', fields)
        self.assertIn('line-height: 1.35', fields)
        # `width: 100%` on a select is harmless in a grid cell and means "all of it" in a
        # flex row, which is how the sort dropdown took the search box's space.
        self.assertIn('width: auto', fields)
        self.assertIn('.tvr-field > input, .tvr-field > select, .tvr-field > textarea '
                      '{ width: 100%; }', self.css)

    def test_buttons_do_not_inherit_the_font_shorthand(self):
        """`font: inherit` also sets line-height, and outranks any class that sets it.

        At `#tv-retention button` it is (1,0,1), so every dense list built from buttons
        silently reverted to the page's paragraph spacing however tight its own rule was.
        """
        self.assertNotRegex(self.css, r'#tv-retention button \{[^}]*font:\s*inherit')
        self.assertRegex(self.css, r'#tv-retention button \{[^}]*font-family:\s*inherit')

    def test_no_colour_is_defined_only_in_the_dark(self):
        """A colour whose only definition lives inside a media query has no value at all
        for a reader who is not in that query."""
        light = set(re.findall(r'(--tvr-[a-z-]+):', self.css.split(':root {')[1].split('}')[0]))
        dark = set(re.findall(r'(--tvr-[a-z-]+):',
                              self.css.split(':root[data-theme="dark"] {')[1].split('}')[0]))
        self.assertEqual(dark - light, set(), 'defined dark but never light')

    def test_a_choice_beats_the_system_in_both_directions(self):
        """Someone on a dark desktop must still be able to choose light, which is why the
        media-query block is guarded rather than unconditional."""
        self.assertIn(':root:not([data-theme="light"])', self.css)
        self.assertIn(':root[data-theme="dark"]', self.css)
