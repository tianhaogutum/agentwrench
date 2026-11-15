"""Port of test/discovery-unset-env.test.js (--server-unset-env)."""

import asyncio
import collections
import json
import os
import re
import shutil
import sys

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.discover import run_discover

from .conftest import PYTHON, SLIM_EXPECTED, TimerSpy, fixture, run as run_cli, signal_handlers, spawn_marker

CONFIG = {'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']}
SNAPSHOT = SLIM_EXPECTED
LITERAL = '$(touch SHOULD_NOT_EXIST); * $MCP_SLIM_UNSET_KEEP'


class Setup:
    def __init__(self, directory):
        self.dir = directory
        self.server = directory / 'server space 🦊'
        self.server.mkdir()
        for name in ('discovery_env_server.py', 'discovery_server.py'):
            shutil.copyfile(fixture(name), self.server / name)
        (directory / 'config.json').write_text(json.dumps(CONFIG))
        (self.server / 'config.json').write_text('{"allow":[]}')
        (self.server / 'catalogue.json').write_text('child sentinel')
        self.env = {**os.environ, 'MCP_SLIM_UNSET_A': 'synthetic inherited', 'MCP_SLIM_UNSET_B': 'synthetic second',
                    'MCP_SLIM_UNSET_EMPTY': 'nonempty', 'MCP_SLIM_UNSET_KEEP': 'keep 🦊'}
        self.mark({'MCP_SLIM_UNSET_KEEP': self.env['MCP_SLIM_UNSET_KEEP'], 'MCP_SLIM_UNSET_EMPTY': '', 'MCP_SLIM_UNSET_NEW': LITERAL},
                  ['MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B', 'MCP_SLIM_UNSET_NOT_PRESENT'])
        self.options = ['--server-unset-env', 'MCP_SLIM_UNSET_A', '--server-unset-env', 'MCP_SLIM_UNSET_B', '--server-unset-env', 'MCP_SLIM_UNSET_NOT_PRESENT',
                        '--server-env', 'MCP_SLIM_UNSET_EMPTY=', '--server-env', f'MCP_SLIM_UNSET_NEW={LITERAL}']

    def mark(self, expected, absent=()):
        (self.server / 'env-marker.json').write_text(json.dumps({'cwd': str(self.server), 'expected': expected, 'absent': list(absent)}))

    def run(self, *args):
        return run_cli(*args, cwd=self.dir, env=self.env)

    def discover(self, mode='normal', extra=(), executable=PYTHON):
        return self.run('discover', '--server-cwd', self.server.name, '--config', 'config.json', *self.options, *extra,
                        '--', executable, './discovery_env_server.py', mode, str(self.dir / 'trace'), LITERAL)


@pytest.fixture
def setup(tmp_dir):
    return Setup(tmp_dir)


def test_server_unset_env_runtime_removes_own_inherited_keys_preserves_empty_values_and_parent_environment(setup, monkeypatch):
    cwd = os.getcwd()
    names = ['MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B', 'MCP_SLIM_UNSET_KEEP', 'MCP_SLIM_UNSET_EMPTY', '__proto__', 'constructor']
    for name in names:
        monkeypatch.setenv(name, 'parent sentinel')
    before = dict(os.environ)
    setup.mark({'MCP_SLIM_UNSET_KEEP': before['MCP_SLIM_UNSET_KEEP'], 'MCP_SLIM_UNSET_EMPTY': '', 'MCP_SLIM_UNSET_NEW': LITERAL},
               ['MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B', '__proto__', 'constructor', 'MCP_SLIM_UNSET_NOT_PRESENT'])
    assert asyncio.run(run_discover([PYTHON, './discovery_env_server.py', 'normal', str(setup.dir / 'trace'), LITERAL], CONFIG,
                                    server_cwd=str(setup.server),
                                    server_unset_env=['MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B', '__proto__', 'constructor', 'MCP_SLIM_UNSET_NOT_PRESENT'],
                                    server_env=['MCP_SLIM_UNSET_EMPTY=', f'MCP_SLIM_UNSET_NEW={LITERAL}'])) == SNAPSHOT
    assert dict(os.environ) == before
    assert os.getcwd() == cwd
    assert json.loads((setup.dir / 'trace').read_text())['literal'] == LITERAL
    setup.mark({'MCP_SLIM_UNSET_KEEP': before['MCP_SLIM_UNSET_KEEP'], 'MCP_SLIM_UNSET_EMPTY': before['MCP_SLIM_UNSET_EMPTY'], 'PATH': before['PATH']},
               ['MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B', '__proto__', 'constructor'])
    assert asyncio.run(run_discover([PYTHON, './discovery_env_server.py'], CONFIG, server_cwd=str(setup.server),
                                    server_unset_env=['MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B', '__proto__', 'constructor'])) == SNAPSHOT
    assert dict(os.environ) == before


def test_server_unset_env_runtime_invalid_inputs_and_override_conflicts_reject_before_spawn_timers_and_listeners(setup, monkeypatch):
    marker = setup.dir / 'spawned'
    before = dict(os.environ)
    spy = TimerSpy(monkeypatch)
    listeners = signal_handlers()
    invalid = [0, False, {}, collections.OrderedDict(), {'A'}, 'PRIVATE_VALUE', [None], [1], [{}], [collections.UserString('A')], [''],
               ['A=PRIVATE_VALUE'], ['1BAD'], ['A-B'], ['A.B'], [' A'], ['A '], ['A\n'], ['A\r'], ['A\t'], ['🦊'], ['A\0B'], ['A', 'A'], [None] * 1]
    cases = [{'server_unset_env': value} for value in invalid] + [
        {'server_unset_env': ['A'], 'server_env': ['A=PRIVATE_VALUE']}, {'server_unset_env': ['A'], 'server_env': ['A=']},
        {'server_unset_env': ['__proto__'], 'server_env': ['__proto__=PRIVATE_VALUE']}]
    for options in cases:
        with pytest.raises(InputError) as caught:
            asyncio.run(run_discover(spawn_marker(marker), {}, **options, discovery_timeout_ms=5000))
        assert re.search('--server-unset-env:', str(caught.value)) and not re.search('PRIVATE_VALUE', str(caught.value))
    assert not marker.exists()
    assert spy.timers == []
    assert signal_handlers() == listeners
    assert dict(os.environ) == before


def test_server_unset_env_cli_repeatable_removal_keeps_cwd_invoking_config_output_and_exact_catalogue_bytes(setup):
    before = dict(setup.env)
    result = setup.discover()
    assert result.returncode == 0, result.stderr
    assert result.stdout == SNAPSHOT
    trace = json.loads((setup.dir / 'trace').read_text())
    assert trace['cwd'] == str(setup.server)
    assert trace['path'] == setup.env['PATH']
    assert trace['literal'] == LITERAL
    assert setup.env == before
    assert not (setup.server / 'SHOULD_NOT_EXIST').exists()
    for exists in (False, True):
        if exists:
            (setup.dir / 'catalogue.json').write_text('invoking sentinel')
        file = setup.discover('normal', ['--output', 'catalogue.json'])
        assert file.returncode == 0, file.stderr
        assert file.stdout == ''
        assert (setup.dir / 'catalogue.json').read_text(encoding='utf-8') == SNAPSHOT
        assert (setup.server / 'catalogue.json').read_text() == 'child sentinel'
    env = setup.env
    setup.mark({'MCP_SLIM_UNSET_KEEP': env['MCP_SLIM_UNSET_KEEP'], 'MCP_SLIM_UNSET_B': env['MCP_SLIM_UNSET_B'],
                'MCP_SLIM_UNSET_EMPTY': env['MCP_SLIM_UNSET_EMPTY'], 'PATH': env['PATH']}, ['MCP_SLIM_UNSET_A'])
    only_removal = setup.run('discover', '--server-cwd', str(setup.server), '--config', 'config.json', '--server-unset-env', 'MCP_SLIM_UNSET_A',
                             '--', PYTHON, './discovery_env_server.py')
    assert only_removal.returncode == 0, only_removal.stderr
    assert only_removal.stdout == SNAPSHOT
    assert setup.env == before


def test_server_unset_env_omission_and_empty_runtime_list_retain_inherited_keys_and_existing_overlays_byte_identically(setup):
    env = setup.env
    setup.mark({'MCP_SLIM_UNSET_A': env['MCP_SLIM_UNSET_A'], 'MCP_SLIM_UNSET_EMPTY': env['MCP_SLIM_UNSET_EMPTY'], 'PATH': env['PATH']})
    inherited = setup.run('discover', '--server-cwd', str(setup.server), '--config', 'config.json', '--', PYTHON, './discovery_env_server.py')
    assert inherited.returncode == 0, inherited.stderr
    assert inherited.stdout == SNAPSHOT
    setup.mark({'PATH': os.environ['PATH'], 'MCP_SLIM_UNSET_EMPTY': '', 'MCP_SLIM_UNSET_NEW': LITERAL})
    command = [PYTHON, './discovery_env_server.py', 'normal', str(setup.dir / 'trace')]
    options = {'server_cwd': str(setup.server), 'server_env': ['MCP_SLIM_UNSET_EMPTY=', f'MCP_SLIM_UNSET_NEW={LITERAL}']}
    baseline = asyncio.run(run_discover(command, CONFIG, **options))
    assert baseline == SNAPSHOT
    assert asyncio.run(run_discover(command, CONFIG, **options, server_unset_env=[])) == baseline


def test_server_unset_env_removing_path_permits_absolute_executable_and_uses_native_absent_path_lookup(setup):
    env = setup.env
    setup.mark({'MCP_SLIM_UNSET_EMPTY': '', 'MCP_SLIM_UNSET_NEW': LITERAL, 'MCP_SLIM_UNSET_KEEP': env['MCP_SLIM_UNSET_KEEP']},
               ['PATH', 'MCP_SLIM_UNSET_A', 'MCP_SLIM_UNSET_B'])
    absolute = setup.discover('normal', ['--server-unset-env', 'PATH'])
    assert absolute.returncode == 0, absolute.stderr
    assert absolute.stdout == SNAPSHOT
    assert 'path' not in json.loads((setup.dir / 'trace').read_text())
    # A private inherited PATH entry must stop participating in executable lookup.
    (setup.dir / 'bin').mkdir()
    os.symlink(PYTHON, setup.dir / 'bin' / 'unique-unset-test-python')
    env['PATH'] = str(setup.dir / 'bin')
    absent = setup.discover('normal', ['--server-unset-env', 'PATH'], 'unique-unset-test-python')
    assert absent.returncode == 1
    assert absent.stdout == ''
    assert re.search('cannot start server', absent.stderr)
    # On Unix the native fallback finds env in /bin:/usr/bin, then runs the absolute interpreter.
    if sys.platform != 'win32':
        native = setup.run('discover', '--server-cwd', str(setup.server), '--config', 'config.json', *setup.options, '--server-unset-env', 'PATH',
                           '--', 'env', PYTHON, './discovery_env_server.py')
        assert native.returncode == 0, native.stderr
        assert native.stdout == SNAPSHOT


def test_server_unset_env_cli_invalid_missing_duplicate_disallowed_conflicting_names_reject_before_spawn_without_echo(setup):
    marker = setup.dir / 'spawned'
    command = ['--', *spawn_marker(marker)]
    invalid = [[], [''], ['A=PRIVATE_VALUE'], ['1BAD'], ['A-B'], ['A.B'], [' A'], ['A '], ['A\n'], ['A\r'], ['A\t'], ['🦊'],
               ['A', '--server-unset-env', 'A'], ['--output', 'other.json'], ['A', '--server-env', 'A=PRIVATE_VALUE'], ['A', '--server-env', 'A=']]
    output = setup.dir / 'catalogue.json'
    for exists in (True, False):
        if exists:
            output.write_text('sentinel')
        elif output.exists():
            output.unlink()
        for values in invalid:
            result = setup.run('discover', '--output', 'catalogue.json', '--server-unset-env', *values, *command)
            assert result.returncode == 2, result.stderr
            assert result.stdout == ''
            assert re.search('--server-unset-env:', result.stderr)
            assert not re.search('PRIVATE_VALUE', result.stderr)
            if exists:
                assert output.read_text() == 'sentinel'
            else:
                assert not output.exists()
        reverse = setup.run('discover', '--server-env', 'A=PRIVATE_VALUE', '--server-unset-env', 'A', '--output', 'catalogue.json', *command)
        assert reverse.returncode == 2, reverse.stderr
        assert re.search('conflict', reverse.stderr)
        assert not re.search('PRIVATE_VALUE', reverse.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = setup.run(mode, '--server-unset-env', 'A=PRIVATE_VALUE', *command)
        assert result.returncode == 2, result.stderr
        assert re.search('--server-unset-env: unsupported option', result.stderr)
        assert not re.search('PRIVATE_VALUE', result.stderr)
    joined = setup.run('discover', '--server-unset-env=A=PRIVATE_VALUE', *command)
    assert joined.returncode == 2
    assert not re.search('PRIVATE_VALUE', joined.stderr)
    assert not marker.exists()
    assert re.search('discover .*--server-unset-env', setup.run('--help').stdout)


def test_server_unset_env_removal_failures_retain_existing_absent_output_and_all_discovery_guards(setup):
    output = setup.dir / 'catalogue.json'
    for exists in (True, False):
        if exists:
            output.write_text('sentinel')
        elif output.exists():
            output.unlink()
        (setup.dir / 'trace').write_text('')
        before = sorted(os.listdir(setup.dir))
        for mode, options in [('cycle', []), ('duplicate', []), ('wrongid', []), ('timeout', ['--request-timeout-ms', '100']),
                              ('timeout', ['--request-timeout-ms', '5000', '--discovery-timeout-ms', '100']), ('normal', ['--max-pages', '1']),
                              ('normal', ['--max-tools', '1']), ('normal', ['--max-output-bytes', '13']), ('normal', ['--max-wire-bytes', '1']),
                              ('stubborn', ['--shutdown-timeout-ms', '100'])]:
            result = setup.discover(mode, ['--output', 'catalogue.json', *options])
            assert result.returncode == 1, f'{mode}: {result.stderr}'
            assert result.stdout == ''
            assert not re.search('synthetic inherited|keep 🦊|MCP_SLIM_UNSET_NEW', result.stderr)
            if exists:
                assert output.read_text() == 'sentinel'
            else:
                assert not output.exists()
            assert sorted(os.listdir(setup.dir)) == before
            assert (setup.server / 'catalogue.json').read_text() == 'child sentinel'
