"""Every global name each worker module uses is one it can actually reach.

A batched edit removed the unmonitored ledger and took `MONITOR_STATUS` with it, because
the constant sat between the function being deleted and the next one. Python raises that
at call time, not at import, so a module can be syntactically perfect and still be broken
in a branch nothing exercises. The JavaScript side has had this guard since a similar edit
dropped a constant there; this is the same test for the worker.

It is a static scan, deliberately: it does not import the modules, so it sees names in
branches no test reaches, and it costs nothing to run.
"""
import ast
import builtins
import re
import unittest
from pathlib import Path

import context  # noqa: F401

WORKER = Path(__file__).resolve().parents[1] / 'src' / 'worker'
BUILTINS = set(dir(builtins))


def bound_by(node) -> set:
    """Every name a function binds locally: arguments, assignments, and all the rest."""
    names = set()
    args = getattr(node, 'args', None)
    if args:
        for group in (args.posonlyargs, args.args, args.kwonlyargs):
            names.update(arg.arg for arg in group)
        for extra in (args.vararg, args.kwarg):
            if extra:
                names.add(extra.arg)
    for child in ast.walk(node):
        # ast.walk yields the node itself first, and a lambda that recursed into itself
        # was an infinite loop rather than a wrong answer.
        if child is node:
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # The nested function's own name, and everything it binds: its parameters are
            # reachable from inside it, and this scan works on the text of the outer one.
            names.add(child.name)
            names.update(bound_by(child))
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in child.names)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            names.update(child.names)
        elif isinstance(child, ast.Lambda):
            names.update(bound_by(child))
    return names


def module_names(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
    for node in tree.body:
        for child in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                             ast.ClassDef)) else ():
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
    return names


def unreachable_names(path: Path) -> list:
    tree = ast.parse(path.read_text())
    known = module_names(tree) | BUILTINS | {'__file__', '__name__', '__doc__'}
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = bound_by(node)
        for child in ast.walk(node):
            if (isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                    and child.id not in local and child.id not in known):
                missing.append(f'{path.name}:{child.lineno} {child.id}')
    return sorted(set(missing))


class Names(unittest.TestCase):
    def test_no_module_uses_a_name_it_cannot_reach(self):
        found = []
        for path in sorted(WORKER.glob('*.py')):
            found.extend(unreachable_names(path))
        self.assertEqual(found, [], 'used but never defined or imported')

    def test_the_scan_would_notice_a_deleted_constant(self):
        """Proof the test is looking, rather than passing on an empty set.

        Written as the failure that happened: a constant used by a function, with the line
        that defined it gone.
        """
        import tempfile
        source = 'GONE = {1: 2}\n\n\ndef read(key):\n    return GONE[key]\n'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.py'
            path.write_text(source)
            self.assertEqual(unreachable_names(path), [])
            path.write_text(source.replace('GONE = {1: 2}\n', ''))
            self.assertEqual([entry.split()[-1] for entry in unreachable_names(path)], ['GONE'])

    def test_it_does_not_trip_over_ordinary_python(self):
        # The shapes that made a cruder version report names that were perfectly fine.
        import tempfile
        source = (
            'import os\n\n\n'
            'def work(items, *rest, **options):\n'
            '    try:\n'
            '        picked = [item for item in items if item]\n'
            '        with open(os.devnull) as handle:\n'
            '            return {key: value for key, value in options.items()}, picked, handle\n'
            '    except OSError as error:\n'
            '        return str(error), rest\n'
            '    finally:\n'
            '        helper = lambda spare: spare\n'
            '        helper(1)\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.py'
            path.write_text(source)
            self.assertEqual(unreachable_names(path), [])


if __name__ == '__main__':
    unittest.main()


class Reachability(unittest.TestCase):
    """Every alert kind can be raised, and every action it offers can be carried out.

    A kind nobody constructs is an alert that cannot appear; an action no handler accepts
    is a button that fails when pressed. Both existed: `path-changed` was declared and
    never raised, so its `accept-path` fix was unreachable from either direction.
    """

    def setUp(self):
        self.worker = WORKER
        self.alerts = (WORKER / 'alerts.py').read_text()
        self.sources = '\n'.join(path.read_text() for path in WORKER.glob('*.py')
                                 if path.name != 'alerts.py')

    def kinds(self):
        import alerts
        return alerts.KINDS

    def test_every_alert_kind_is_raised_somewhere(self):
        unraised = [kind for kind in self.kinds() if f"'{kind}'" not in self.sources]
        self.assertEqual(unraised, [], 'declared but never made')

    def test_every_offered_action_has_a_handler(self):
        actions = {spec['action'] for spec in self.kinds().values() if spec['action']}
        handler = (WORKER / 'actions.py').read_text().split('def action_alert_action')[1]
        missing = sorted(action for action in actions if f"'{action}'" not in handler)
        self.assertEqual(missing, [], 'offered by an alert but not handled')

    def test_no_handler_reads_a_setting_that_no_longer_exists(self):
        """`settings.get('preview', True)` outlived the setting by three versions.

        It defaulted to on, so both quick actions were refused for ever, with a message
        naming a mode the plugin had stopped having.
        """
        import core
        handler = (WORKER / 'actions.py').read_text()
        self.assertNotIn("settings.get('preview'", handler)
        self.assertNotIn('preview', core.DEFAULTS)


class Acknowledgement(unittest.TestCase):
    """An alert is a fact about the present, so acknowledging one is against the fact."""

    def setUp(self):
        import alerts
        self.alerts = alerts
        self.settings = {'alerts': {'header': 'all', 'acknowledge': True, 'muted': []}}

    def test_it_lapses_when_what_the_alert_says_changes(self):
        first = self.alerts.make('no-recycle-bin', instance_id='i1', detail='Sonarr deletes outright')
        acknowledged = {first['key']: self.alerts.fingerprint(first)}
        self.assertTrue(self.alerts.annotate([first], self.settings, acknowledged)[0]['acknowledged'])
        moved = self.alerts.make('no-recycle-bin', instance_id='i1', detail='something else now')
        self.assertFalse(self.alerts.annotate([moved], self.settings, acknowledged)[0]['acknowledged'],
                         'a different fact is not the one that was acknowledged')

    def test_a_muted_kind_is_not_shown_at_all(self):
        settings = {'alerts': {'muted': ['ended-expired']}}
        both = [self.alerts.make('ended-expired', rule_id='r1'),
                self.alerts.make('no-recycle-bin', instance_id='i1')]
        kinds = [alert['kind'] for alert in self.alerts.annotate(both, settings, {})]
        self.assertEqual(kinds, ['no-recycle-bin'])

    def test_a_series_that_is_switched_off_counts_for_nothing(self):
        """It is marked rather than dropped, and marked is what makes it count for nothing.

        Dropping it left the interface unable to offer "show me the ones I switched off",
        which is a real way to lose track of a problem — you turned it off and forgot.
        """
        settings = {'rules': [{'id': 'r1', 'enabled': False}, {'id': 'r2', 'enabled': True}]}
        found = [self.alerts.make('ended', rule_id='r1'),
                 self.alerts.make('unmatched', rule_id='r2'),
                 self.alerts.make('no-recycle-bin', instance_id='i1')]
        shown = self.alerts.annotate(found, settings, {})
        unmanaged = {alert['kind']: alert['unmanaged'] for alert in shown}
        self.assertEqual(unmanaged, {'ended': True, 'unmatched': False,
                                     'no-recycle-bin': False},
                         'a rule about the instance is not about a series')
        # Counted nowhere: not in the header, not in a badge.
        self.assertEqual(self.alerts.header_worthy(shown, settings),
                         [a for a in shown if not a['unmanaged']])
        self.assertEqual(self.alerts.summarise(shown)['total'], 2)

    def test_switching_it_back_on_makes_its_alerts_count_again(self):
        # Nothing was deleted: the facts stay in the cache and stop being ignored.
        found = [self.alerts.make('ended', rule_id='r1')]
        off = {'rules': [{'id': 'r1', 'enabled': False}]}
        on = {'rules': [{'id': 'r1', 'enabled': True}]}
        self.assertEqual(self.alerts.summarise(self.alerts.annotate(found, off, {}))['total'], 0)
        self.assertEqual(self.alerts.summarise(self.alerts.annotate(found, on, {}))['total'], 1)

    def test_an_alert_about_what_we_did_survives_the_disable_it_announces(self):
        """Auto-disable would otherwise silence the only notice that it happened.

        The distinction is tense, not scope. A *state* nobody is managing is fairly
        suppressed; an *action this application took* without being asked is not.
        """
        off = {'rules': [{'id': 'r1', 'enabled': False}]}
        did = self.alerts.make('ended-expired', rule_id='r1')   # we switched it off
        state = self.alerts.make('ended', rule_id='r1')         # merely true of it
        self.assertTrue(self.alerts.survives_disable(did))
        self.assertFalse(self.alerts.survives_disable(state))
        kept = self.alerts.managed_only([did, state], off)
        self.assertEqual([a['kind'] for a in kept], ['ended-expired'])
        shown = {a['kind']: a['unmanaged'] for a in self.alerts.annotate([did, state], off, {})}
        self.assertEqual(shown, {'ended-expired': False, 'ended': True})

    def test_the_surviving_notice_can_still_be_acknowledged(self):
        # It records something finished, so putting it away for good is the right end.
        did = self.alerts.make('ended-expired', rule_id='r1')
        self.assertTrue(self.alerts.may_acknowledge(did, {'alerts': {'acknowledge': True}}))

    def test_muting_and_switching_off_are_not_the_same_question(self):
        """Muting hides a kind across every series and leaves a blocker blocking.

        Switching a series off is about that one series: its alerts still exist and can
        still be looked at, they simply stop counting and stop blocking a run they are not
        part of.
        """
        blocker = self.alerts.make('unmatched', rule_id='r1')
        self.assertTrue(blocker['blocking'])
        muted = self.alerts.annotate([blocker], {'alerts': {'muted': ['unmatched']}}, {})
        self.assertEqual(muted, [], 'a muted kind is not shown at all')
        off = self.alerts.annotate([blocker], {'rules': [{'id': 'r1', 'enabled': False}]}, {})
        self.assertEqual(len(off), 1, 'still there to be looked at')
        self.assertTrue(off[0]['unmanaged'])
        self.assertEqual(self.alerts.summarise(off)['blocking'], 0, 'and blocking nothing')

    def test_the_header_counts_what_it_was_told_to(self):
        found = [self.alerts.make('unmatched', rule_id='r1'),
                 self.alerts.make('no-recycle-bin', instance_id='i1'),
                 self.alerts.make('ended-expired', rule_id='r2')]
        counts = {}
        for wanted in ('errors', 'warnings', 'all'):
            counts[wanted] = len(self.alerts.header_worthy(found, {'alerts': {'header': wanted}}))
        # ended-expired is a notice: nothing is wrong, the rule was switched off because
        # there is nothing left for it to do, and counting that as a problem is how a
        # header stops being read.
        self.assertEqual(counts, {'errors': 1, 'warnings': 2, 'all': 3})

    def test_an_acknowledged_alert_is_never_counted(self):
        found = self.alerts.annotate([self.alerts.make('no-recycle-bin', instance_id='i1')],
                                     self.settings,
                                     {'no-recycle-bin:i1': self.alerts.fingerprint(
                                         self.alerts.make('no-recycle-bin', instance_id='i1'))})
        self.assertEqual(self.alerts.header_worthy(found, self.settings), [])


class AcrossModules(unittest.TestCase):
    """Every `main.something` the RPC surface reaches for actually exists.

    The static scan catches a bare name that has gone; it cannot see `main.thing`, because
    that is an attribute lookup and only fails when the line runs. `log_settings_change`
    was deleted by an edit that replaced the block around it, and the only symptom was
    "Unexpected backend error" the next time settings were saved.
    """

    def test_actions_only_calls_into_main_for_things_main_has(self):
        import ast
        import main
        tree = ast.parse((WORKER / 'actions.py').read_text())
        wanted = sorted({node.attr for node in ast.walk(tree)
                         if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                         and node.value.id == 'main'})
        self.assertTrue(wanted, 'the scan must be seeing real references')
        missing = [name for name in wanted if not hasattr(main, name)]
        self.assertEqual(missing, [], 'actions.py reaches for something main does not have')


class Unused(unittest.TestCase):
    """The other half of the reachability question.

    `unreachable_names` catches a name a module *uses* and cannot reach. It says nothing
    about a name defined and never used, which is how six dead functions and two extension
    lists survived a rewrite that removed everything calling them — including
    `MEDIA_EXTENSIONS` and `SIDECAR_EXTENSIONS`, sitting in the module documented as
    touching no filesystem and implying a capability the project had given up.
    """

    ROOTS = sorted(WORKER.glob('*.py')) + sorted((WORKER.parents[1] / 'tests').glob('*.py'))

    @classmethod
    def setUpClass(cls):
        cls.everything = '\n'.join(path.read_text() for path in cls.ROOTS)

    def mentions(self, name):
        return len(re.findall(rf'\b{re.escape(name)}\b', self.everything))

    def test_no_worker_module_defines_something_nothing_uses(self):
        orphans = []
        for path in sorted(WORKER.glob('*.py')):
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and not node.name.startswith('__'):
                    # One mention is the definition itself.
                    if self.mentions(node.name) <= 1:
                        orphans.append(f'{path.name}: {node.name}()')
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            if self.mentions(target.id) <= 1:
                                orphans.append(f'{path.name}: {target.id}')
        self.assertEqual(orphans, [], 'defined and never used anywhere, tests included')

    def test_the_scan_would_notice_something_left_behind(self):
        # Proof it is looking: a name this suite mentions nowhere else.
        self.assertEqual(self.mentions('a_name_nothing_in_this_project_uses'), 1)


class EndedAlerts(unittest.TestCase):
    """A series that has finished, told apart from one that has finished and emptied."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def rule(self):
        return {'id': 'r1', 'match_status': 'matched', 'enabled': True}

    def kinds(self, lifecycle, **state):
        found = self.main.alerts_for_rule({}, self.rule(),
                                          dict({'ok': True, 'lifecycle': lifecycle}, **state))
        return [alert['kind'] for alert in found]

    def test_ended_with_episodes_left_is_a_notice_of_its_own(self):
        """No further episodes are coming, so what is kept can only shrink.

        Worth saying while there is still something to decide about — which is exactly the
        case the old alert did not cover, because it only fired once the window was empty.
        """
        self.assertEqual(self.kinds('ended', files_in_frame=12, files_total=60), ['ended'])

    def test_ended_and_empty_is_still_the_other_one(self):
        self.assertEqual(self.kinds('ended_expired'), ['ended-expired'])
        self.assertEqual(self.kinds('ended_empty'), ['ended-expired'])

    def test_a_running_series_says_nothing(self):
        self.assertEqual(self.kinds(''), [])

    def test_it_is_a_notice_and_does_not_notify_twice(self):
        # Sonarr's own "this series ended" notification already goes out once, the first
        # time it says so. This is the standing fact rather than the news of it.
        import alerts
        made = alerts.make('ended', rule_id='r1', detail='x')
        self.assertEqual(made['severity'], alerts.NOTICE)
        self.assertFalse(made['blocking'])
        self.assertFalse(alerts.notifies(made))
