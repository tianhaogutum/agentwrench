"""Port of test/discovery-cwd.test.js (--server-cwd)."""

import asyncio
import json
import os
import re
import shutil
import urllib.parse

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.discover import run_discover

from .conftest import PYTHON, SLIM_EXPECTED, TimerSpy, fixture, run as run_cli, signal_handlers, spawn_marker

FIXTURE = fixture('discovery_cwd_server.py')
CONFIG = {'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']}
SNAPSHOT = SLIM_EXPECTED


@pytest.fixture
def setup(tmp_dir):
    server = tmp_dir / 'server space 🦊'
    server.mkdir()
    (server / 'cwd-marker.json').write_text(json.dumps({'cwd': str(server), 'env': 'inherited-🦊'}))
    shutil.copyfile(FIXTURE, server / 'server.py')
    shutil.copyfile(fixture('discovery_server.py'), server / 'discovery_server.py')
    (tmp_dir / 'config.json').write_text(json.dumps(CONFIG))
    # A tempting child-relative config/output must never be used by the CLI.
    (server / 'config.json').write_text('{"allow":[]}')
    (server / 'catalogue.json').write_text('child sentinel')
    env = {**os.environ, 'MCP_SLIM_CWD_MARKER': 'inherited-🦊'}

    def run(*args):
        return run_cli(*args, cwd=tmp_dir, env=env)

    return tmp_dir, server, env, run


def test_server_cwd_runtime_selects_directory_before_relative_child_file_access_without_changing_parent_cwd(setup):
    directory, server, _, _ = setup
    before = os.getcwd()
    # Absolute entry point, but relative marker read can only succeed in server cwd.
    (server / 'cwd-marker.json').write_text(json.dumps({'cwd': str(server)}))
    assert asyncio.run(run_discover([PYTHON, FIXTURE, 'normal', str(directory / 'trace')], CONFIG,
                                    server_cwd=os.path.relpath(server, before))) == SNAPSHOT
    assert json.loads((directory / 'trace').read_text())['cwd'] == str(server)
    assert os.getcwd() == before


def test_server_cwd_invalid_runtime_values_reject_before_spawning_or_arming_timers(setup, monkeypatch):
    directory = setup[0]
    marker = directory / 'spawned'
    spy = TimerSpy(monkeypatch)
    listeners = signal_handlers()
    for server_cwd in ['', 0, False, {}, [], bytes(str(directory), 'utf-8'), urllib.parse.urlparse(f'file://{directory}'),
                       'bad\0cwd', f'{directory}/missing', f'{directory}/config.json', f'{directory}/config.json/']:
        with pytest.raises(InputError, match='--server-cwd:'):
            asyncio.run(run_discover(spawn_marker(marker), {}, server_cwd=server_cwd, discovery_timeout_ms=5000))
    assert not marker.exists()
    assert spy.timers == []
    assert signal_handlers() == listeners


def test_server_cwd_cli_relative_absolute_selection_keeps_invoking_config_output_paths_and_exact_stdout_bytes(setup):
    directory, server, _, run = setup
    for cwd in (server.name, str(server)):
        result = run('discover', '--server-cwd', cwd, '--config', 'config.json', '--', PYTHON, './server.py', 'normal', str(directory / 'trace'))
        assert result.returncode == 0, result.stderr
        assert result.stdout == SNAPSHOT
        assert json.loads((directory / 'trace').read_text())['cwd'] == str(server)
    (directory / 'catalogue.json').write_text('invoking sentinel')
    result = run('discover', '--server-cwd', server.name, '--config', 'config.json', '--output', 'catalogue.json', '--', PYTHON, './server.py')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert (directory / 'catalogue.json').read_text(encoding='utf-8') == SNAPSHOT
    assert (server / 'catalogue.json').read_text() == 'child sentinel'


def test_server_cwd_omission_inherits_invoking_cwd_and_is_byte_identical_to_explicit_dot(setup):
    _, server, env, _ = setup

    def run(*options):
        return run_cli('discover', *options, '--', PYTHON, './server.py', cwd=server, env=env)

    inherited, selected = run(), run('--server-cwd', '.')
    assert inherited.returncode == 0, inherited.stderr
    assert selected.returncode == 0, selected.stderr
    assert selected.stdout == inherited.stdout
    assert len(json.loads(inherited.stdout)['tools']) == 5


def test_server_cwd_path_environment_are_inherited_and_shell_metacharacters_stay_literal(setup):
    directory, server, env, _ = setup
    bin_dir = directory / 'bin'
    bin_dir.mkdir()
    os.symlink(PYTHON, bin_dir / 'cwd-test-python')
    literal = '$(touch SHOULD_NOT_EXIST); * $MCP_SLIM_CWD_MARKER'
    env['PATH'] = f"{bin_dir}:{env['PATH']}"
    result = run_cli('discover', '--server-cwd', server.name, '--', 'cwd-test-python', './server.py', 'normal', str(directory / 'trace'), literal,
                     cwd=directory, env=env)
    assert result.returncode == 0, result.stderr
    trace = json.loads((directory / 'trace').read_text())
    assert trace['cwd'] == str(server)
    assert trace['env'] == env['MCP_SLIM_CWD_MARKER']
    assert trace['path'] == env['PATH']
    assert trace['literal'] == literal
    assert not (server / 'SHOULD_NOT_EXIST').exists()


def test_server_cwd_cli_invalid_missing_duplicate_disallowed_values_reject_before_spawn_and_preserve_output(setup):
    directory, _, _, run = setup
    marker = directory / 'spawned'
    command = ['--', *spawn_marker(marker)]
    (directory / 'catalogue.json').write_text('sentinel')
    for values in [[], [''], ['missing'], ['config.json'], ['config.json/'], ['.', '--server-cwd', '.'], ['--output', 'other.json']]:
        result = run('discover', '--output', 'catalogue.json', '--server-cwd', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--server-cwd:', result.stderr)
        assert (directory / 'catalogue.json').read_text() == 'sentinel'
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--server-cwd', '.', *command)
        assert result.returncode == 2, result.stderr
        assert re.search('--server-cwd: unsupported option', result.stderr)
    assert not marker.exists()
    assert re.search('discover .*--server-cwd', run('--help').stdout)


def test_server_cwd_selected_directory_discovery_failures_preserve_invoking_existing_absent_destinations(setup):
    directory, server, _, run = setup
    before = sorted(os.listdir(directory))
    for exists in (True, False):
        if exists:
            (directory / 'catalogue.json').write_text('sentinel')
        else:
            (directory / 'catalogue.json').unlink()
        for mode, options in [('cycle', []), ('timeout', ['--request-timeout-ms', '100']),
                              ('timeout', ['--request-timeout-ms', '5000', '--discovery-timeout-ms', '100']),
                              ('normal', ['--max-pages', '1']), ('normal', ['--max-tools', '1']), ('normal', ['--max-output-bytes', '13']),
                              ('normal', ['--max-wire-bytes', '1']), ('stubborn', ['--shutdown-timeout-ms', '100'])]:
            result = run('discover', '--server-cwd', server.name, '--config', 'config.json', '--output', 'catalogue.json', *options,
                         '--', PYTHON, './server.py', mode)
            assert result.returncode == 1, f'{mode}: {result.stderr}'
            assert result.stdout == ''
            if exists:
                assert (directory / 'catalogue.json').read_text() == 'sentinel'
            else:
                assert not (directory / 'catalogue.json').exists()
            assert sorted(os.listdir(directory)) == sorted([*before, *(['catalogue.json'] if exists else [])])
            assert (server / 'catalogue.json').read_text() == 'child sentinel'


def test_server_cwd_symlink_traversal_and_directory_spelling_follow_filesystem_semantics(setup):
    directory, server, _, run = setup
    (server / 'nested').mkdir()
    os.symlink(server / 'nested', directory / 'link', target_is_directory=True)
    os.symlink(server, directory / 'direct-link', target_is_directory=True)
    for cwd in ('direct-link', 'link/..', f'{server.name}/', f'{server.name}/.'):
        result = run('discover', '--server-cwd', cwd, '--config', 'config.json', '--', PYTHON, './server.py')
        assert result.returncode == 0, result.stderr
        assert result.stdout == SNAPSHOT
    result = run('discover', '--server-cwd', f'missing/../{server.name}', '--', '/does/not/exist')
    assert result.returncode == 2, result.stderr
    assert re.search('--server-cwd:', result.stderr)
