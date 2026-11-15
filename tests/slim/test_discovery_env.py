"""Port of test/discovery-env.test.js (--server-env)."""

import asyncio
import collections
import json
import os
import re
import shutil

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.discover import run_discover

from .conftest import EXAMPLE_CONFIG, PYTHON, SLIM_EXPECTED, TimerSpy, fixture, run as run_cli, signal_handlers, spawn_marker

CONFIG = {'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']}
SNAPSHOT = SLIM_EXPECTED
LITERAL = '  🦊 x=y== $(touch SHOULD_NOT_EXIST); * $MCP_SLIM_ENV_KEEP\nsecond line  '


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
        self.env = {**os.environ, 'MCP_SLIM_ENV_KEEP': 'inherited keep 🦊', 'MCP_SLIM_ENV_REPLACE': 'inherited replace', 'MCP_SLIM_ENV_EMPTY': 'nonempty'}
        self.mark({'MCP_SLIM_ENV_KEEP': self.env['MCP_SLIM_ENV_KEEP'], 'MCP_SLIM_ENV_REPLACE': LITERAL, 'MCP_SLIM_ENV_EMPTY': '', 'MCP_SLIM_ENV_NEW': 'new=value'})
        self.options = ['--server-env', f'MCP_SLIM_ENV_REPLACE={LITERAL}', '--server-env', 'MCP_SLIM_ENV_EMPTY=', '--server-env', 'MCP_SLIM_ENV_NEW=new=value']

    def mark(self, values):
        (self.server / 'env-marker.json').write_text(json.dumps({'cwd': str(self.server), 'expected': values}))

    def run(self, *args):
        return run_cli(*args, cwd=self.dir, env=self.env)

    def discover(self, mode='normal', extra=(), executable=PYTHON):
        return self.run('discover', '--server-cwd', self.server.name, '--config', 'config.json', *self.options, *extra,
                        '--', executable, './discovery_env_server.py', mode, str(self.dir / 'trace'), LITERAL)


@pytest.fixture
def setup(tmp_dir):
    return Setup(tmp_dir)


def test_server_env_runtime_overlays_inherited_values_without_mutating_parent_environment_or_cwd(setup, monkeypatch):
    cwd = os.getcwd()
    monkeypatch.setenv('MCP_SLIM_ENV_REPLACE', 'parent synthetic sentinel')
    before = dict(os.environ)
    # Inherited values come from the actual invoking runtime in this direct API case.
    setup.mark({'PATH': os.environ['PATH'], 'MCP_SLIM_ENV_REPLACE': LITERAL, 'MCP_SLIM_ENV_EMPTY': '', '_MCP_SLIM_15': 'value=tail',
                'mcp_slim_env_replace': 'case distinct', '__proto__': 'portable-name', 'constructor': 'also-portable'})
    assert asyncio.run(run_discover([PYTHON, './discovery_env_server.py', 'normal', str(setup.dir / 'trace'), LITERAL], CONFIG,
                                    server_cwd=str(setup.server),
                                    server_env=[f'MCP_SLIM_ENV_REPLACE={LITERAL}', 'MCP_SLIM_ENV_EMPTY=', '_MCP_SLIM_15=value=tail',
                                                'mcp_slim_env_replace=case distinct', '__proto__=portable-name', 'constructor=also-portable'])) == SNAPSHOT
    assert dict(os.environ) == before
    assert os.getcwd() == cwd
    assert json.loads((setup.dir / 'trace').read_text())['literal'] == LITERAL


def test_server_env_runtime_invalid_inputs_reject_before_spawn_timers_and_signal_handlers(setup, monkeypatch):
    marker = setup.dir / 'spawned'
    before = dict(os.environ)
    spy = TimerSpy(monkeypatch)
    listeners = signal_handlers()
    for server_env in [0, False, {}, collections.OrderedDict(), 'A=PRIVATE_VALUE', [None], [1], [{}], [collections.UserString('A=PRIVATE_VALUE')],
                       ['PRIVATE_VALUE'], ['=PRIVATE_VALUE'], ['1BAD=PRIVATE_VALUE'], ['A-B=PRIVATE_VALUE'], ['A.B=PRIVATE_VALUE'], [' A=PRIVATE_VALUE'],
                       ['A =PRIVATE_VALUE'], ['A\n=PRIVATE_VALUE'], ['A\r=PRIVATE_VALUE'], ['A\t=PRIVATE_VALUE'], ['🦊=PRIVATE_VALUE'], ['A\0B=PRIVATE_VALUE'],
                       ['A=PRIVATE\0_VALUE'], ['A=PRIVATE_VALUE', 'A=second'], [None] * 1]:
        with pytest.raises(InputError) as caught:
            asyncio.run(run_discover(spawn_marker(marker), {}, server_env=server_env, discovery_timeout_ms=5000))
        assert re.search('--server-env:', str(caught.value)) and not re.search('PRIVATE_VALUE|second', str(caught.value))
    assert not marker.exists()
    assert spy.timers == []
    assert signal_handlers() == listeners
    assert dict(os.environ) == before


def test_server_env_cli_repeatable_literal_overrides_preserve_cwd_invoking_config_output_and_exact_bytes(setup):
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


def test_server_env_omission_and_explicit_empty_runtime_overlay_inherit_unchanged_byte_identical_output(setup):
    env = setup.env
    setup.mark({'MCP_SLIM_ENV_KEEP': env['MCP_SLIM_ENV_KEEP'], 'MCP_SLIM_ENV_REPLACE': env['MCP_SLIM_ENV_REPLACE'],
                'MCP_SLIM_ENV_EMPTY': env['MCP_SLIM_ENV_EMPTY'], 'PATH': env['PATH']})

    def run(*options):
        return run_cli('discover', '--config', str(EXAMPLE_CONFIG), *options, '--', PYTHON, './discovery_env_server.py', cwd=setup.server, env=env)

    inherited, explicit = run(), run('--server-env', f"MCP_SLIM_ENV_KEEP={env['MCP_SLIM_ENV_KEEP']}")
    assert inherited.returncode == 0, inherited.stderr
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout == inherited.stdout
    setup.mark({'PATH': os.environ['PATH']})
    command = [PYTHON, './discovery_env_server.py']
    baseline = asyncio.run(run_discover(command, CONFIG, server_cwd=str(setup.server)))
    assert baseline == SNAPSHOT
    assert asyncio.run(run_discover(command, CONFIG, server_cwd=str(setup.server), server_env=[])) == baseline


def test_server_env_inherited_and_overridden_path_drive_executable_lookup_including_empty_path(setup):
    directory = setup.dir
    (directory / 'inherited-bin').mkdir()
    (directory / 'override-bin').mkdir()
    os.symlink(PYTHON, directory / 'inherited-bin' / 'env-inherited-python')
    os.symlink(PYTHON, directory / 'override-bin' / 'env-overridden-python')
    setup.env['PATH'] = f"{directory / 'inherited-bin'}:{setup.env['PATH']}"
    assert setup.discover('normal', [], 'env-inherited-python').returncode == 0
    setup.mark({'PATH': str(directory / 'override-bin')})
    over = setup.discover('normal', ['--server-env', f"PATH={directory / 'override-bin'}"], 'env-overridden-python')
    assert over.returncode == 0, over.stderr
    assert over.stdout == SNAPSHOT
    assert json.loads((directory / 'trace').read_text())['path'] == str(directory / 'override-bin')
    missing = setup.discover('normal', ['--server-env', f"PATH={directory / 'override-bin'}"], 'env-inherited-python')
    assert missing.returncode == 1
    assert missing.stdout == ''
    assert re.search('cannot start server', missing.stderr)
    setup.mark({'PATH': ''})
    empty = setup.discover('normal', ['--server-env', 'PATH='])
    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == SNAPSHOT
    assert json.loads((directory / 'trace').read_text())['path'] == ''
    assert (setup.server / 'catalogue.json').read_text() == 'child sentinel'


def test_server_env_cli_invalid_missing_duplicate_disallowed_values_reject_before_spawn_without_echoing_values(setup):
    marker = setup.dir / 'spawned'
    command = ['--', *spawn_marker(marker)]
    invalid = [[], [''], ['PRIVATE_VALUE'], ['=PRIVATE_VALUE'], ['1BAD=PRIVATE_VALUE'], ['A-B=PRIVATE_VALUE'], ['A.B=PRIVATE_VALUE'],
               [' A=PRIVATE_VALUE'], ['A =PRIVATE_VALUE'], ['A\n=PRIVATE_VALUE'], ['A\r=PRIVATE_VALUE'], ['A\t=PRIVATE_VALUE'], ['🦊=PRIVATE_VALUE'],
               ['A=PRIVATE_VALUE', '--server-env', 'A=other'], ['--output', 'other.json']]
    output = setup.dir / 'catalogue.json'
    for exists in (True, False):
        if exists:
            output.write_text('sentinel')
        elif output.exists():
            output.unlink()
        for values in invalid:
            result = setup.run('discover', '--output', 'catalogue.json', '--server-env', *values, *command)
            assert result.returncode == 2, result.stderr
            assert result.stdout == ''
            assert re.search('--server-env:', result.stderr)
            assert not re.search('PRIVATE_VALUE|A=other', result.stderr)
            if exists:
                assert output.read_text() == 'sentinel'
            else:
                assert not output.exists()
    for mode in ('proxy', 'measure', 'plan'):
        result = setup.run(mode, '--server-env', 'A=PRIVATE_VALUE', *command)
        assert result.returncode == 2, result.stderr
        assert re.search('--server-env: unsupported option', result.stderr)
        assert not re.search('PRIVATE_VALUE', result.stderr)
    joined = setup.run('discover', '--server-env=A=PRIVATE_VALUE', *command)
    assert joined.returncode == 2
    assert not re.search('PRIVATE_VALUE', joined.stderr)
    assert not marker.exists()
    assert re.search('discover .*--server-env', setup.run('--help').stdout)


def test_server_env_overlay_discovery_failures_preserve_existing_absent_atomic_output_and_child_path_semantics(setup):
    output = setup.dir / 'catalogue.json'
    for exists in (True, False):
        if exists:
            output.write_text('sentinel')
        elif output.exists():
            output.unlink()
        # The trace is produced by the child, before collection; publication leaves no temporary files.
        (setup.dir / 'trace').write_text('')
        before = sorted(os.listdir(setup.dir))
        for mode, options in [('cycle', []), ('duplicate', []), ('wrongid', []), ('timeout', ['--request-timeout-ms', '100']),
                              ('timeout', ['--request-timeout-ms', '5000', '--discovery-timeout-ms', '100']), ('normal', ['--max-pages', '1']),
                              ('normal', ['--max-tools', '1']), ('normal', ['--max-output-bytes', '13']), ('normal', ['--max-wire-bytes', '1']),
                              ('stubborn', ['--shutdown-timeout-ms', '100'])]:
            result = setup.discover(mode, ['--output', 'catalogue.json', *options])
            assert result.returncode == 1, f'{mode}: {result.stderr}'
            assert result.stdout == ''
            assert not re.search('MCP_SLIM_ENV|inherited keep|second line|new=value', result.stderr)
            if exists:
                assert output.read_text() == 'sentinel'
            else:
                assert not output.exists()
            assert sorted(os.listdir(setup.dir)) == before
            assert (setup.server / 'catalogue.json').read_text() == 'child sentinel'
