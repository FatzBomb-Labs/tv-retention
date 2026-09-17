"""Test-only fresh worker process. Never opens a socket or uses an inherited config.

The parent supplies ordered fake responses and a checkpoint number, not executable code.
Events are flushed/fsynced before os._exit so the parent can inspect accepted fake writes
independently of the application's intent. This simulates process loss, not power loss.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import urllib.error
from unittest.mock import patch


def execute(root, scenario_path):
    scenario = json.loads(scenario_path.read_text())
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            'TVR_CONFIG_DIR': str(root), 'TVR_CONFIG': str(root / 'settings.json'),
            'TVR_RUNTIME': str(root / 'runtime'), 'TVR_DEVELOPMENT': '1',
        }))
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'socket.socket.connect_ex', 'socket.getaddrinfo'):
            stack.enter_context(patch(target, side_effect=AssertionError('Network forbidden')))
        import context  # noqa: F401
        from fake_sonarr import FakeSonarr

        def event(kind, **values):
            with (root / scenario['events']).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(kind=kind, pid=os.getpid(), **values)) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

        class RecordingSonarr(FakeSonarr):
            def urlopen(self, request, **kwargs):
                response = super().urlopen(request, **kwargs)
                if request.get_method() != 'GET':
                    event('accepted', request=self.requests[-1])
                return response

        fake = RecordingSonarr()
        for step in scenario['requests']:
            step = dict(step)
            if 'error' in step:
                step['response'] = urllib.error.URLError(step.pop('error'))
            fake.expect(**step)
        stack.enter_context(patch('urllib.request.urlopen', fake.urlopen))
        import main
        import store
        from core import validate_settings

        # Only the first child creates settings; later children reload the same files.
        if not store.CONFIG.exists():
            settings = validate_settings({
                'instances': [{'id': 'fake', 'name': 'Fixture', 'url': fake.url,
                               'api_key': fake.api_key}],
                'schedule': {'test_mode': False, 'enabled': False},
                'rules': [{'id': 'r1', 'instance_id': 'fake', 'series_id': 1,
                           'path': '/tv/Fixture', 'tvdb_id': 10, 'keep_days': 30}],
            })
            settings['state_dir'] = str(root / 'state')
            store.save_settings(settings)
        if scenario.get('test_mode'):
            settings = store.load_settings()
            settings['schedule']['test_mode'] = True
            store.save_settings(settings)
        real_save = main.save_intent
        count = 0

        def checkpoint(settings, intent):
            nonlocal count
            count += 1
            for phase in ('before', 'after'):
                if phase == 'after':
                    real_save(settings, intent)
                event('checkpoint', number=count, phase=phase)
                if scenario.get('cut') == [count, phase]:
                    fake.assert_finished()
                    event('cut', number=count, phase=phase)
                    if scenario.get('fail'):
                        raise OSError('fixture checkpoint unavailable')
                    os._exit(73)

        stack.enter_context(patch.object(main, 'save_intent', side_effect=checkpoint))
        event('start', prior_intent=store.load_intent(store.load_settings()))
        try:
            result = main.run(scheduled=scenario.get('scheduled', False))
        except OSError as error:
            if not scenario.get('fail') or str(error) != 'fixture checkpoint unavailable':
                raise
            result = {'checkpoint_error': str(error)}
        fake.assert_finished()
        (root / scenario['result']).write_text(json.dumps({
            'pid': os.getpid(), 'result': result, 'requests': fake.requests,
        }), encoding='utf-8')


if __name__ == '__main__':
    execute(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
