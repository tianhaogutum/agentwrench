"""``discover --accept-protocol-version``: strict 2025-11-25 by default, older revisions opt-in."""

import json
import re

import pytest

from .conftest import HERE, PYTHON, run, spawn_marker

OLD_SERVER = [PYTHON, str(HERE.parent / 'scan' / 'fixtures' / 'old_protocol_server.py')]  # negotiates 2025-06-18
CURRENT_SERVER = [PYTHON, str(HERE.parents[1] / 'examples' / 'slim' / 'fake_server.py'), 'static']


def test_default_rejects_an_older_protocol_and_names_the_opt_in():
    result = run('discover', '--', *OLD_SERVER)
    assert result.returncode == 1
    assert result.stdout == ''
    assert ('discovery requires tools capability and protocol 2025-11-25 (server negotiated 2025-06-18; '
            'to accept it, add --accept-protocol-version 2025-06-18)') in result.stderr


def test_accepting_the_negotiated_version_discovers_the_catalogue():
    result = run('discover', '--accept-protocol-version', '2025-06-18', '--', *OLD_SERVER)
    assert result.returncode == 0, result.stderr
    assert [tool['name'] for tool in json.loads(result.stdout)['tools']] == ['ping_db']
    other = run('discover', '--accept-protocol-version', '2025-03-26', '--', *OLD_SERVER)
    assert other.returncode == 1
    assert 'protocol 2025-03-26, 2025-11-25 (server negotiated 2025-06-18' in other.stderr


def test_repeatable_and_current_servers_are_unaffected():
    options = ['--accept-protocol-version', '2024-11-05', '--accept-protocol-version', '2025-06-18']
    assert run('discover', *options, '--', *OLD_SERVER).returncode == 0
    strict = run('discover', '--', *CURRENT_SERVER)
    relaxed = run('discover', *options, '--', *CURRENT_SERVER)
    assert strict.returncode == relaxed.returncode == 0, strict.stderr + relaxed.stderr
    assert strict.stdout == relaxed.stdout


@pytest.mark.parametrize('options,message', [
    (['--accept-protocol-version'], 'missing value'),
    (['--accept-protocol-version', 'latest'], 'expected an MCP protocol revision'),
    (['--accept-protocol-version', '2025-06-18', '--accept-protocol-version', '2025-06-18'], 'duplicate version'),
])
def test_invalid_values_exit_2_before_spawning(tmp_dir, options, message):
    marker = tmp_dir / 'spawned'
    result = run('discover', *options, '--', *spawn_marker(marker))
    assert result.returncode == 2
    assert f'--accept-protocol-version: {message}' in result.stderr
    assert not marker.exists()


def test_option_is_discover_only_and_documented():
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--accept-protocol-version', '2025-06-18')
        assert result.returncode == 2
        assert '--accept-protocol-version: unsupported option' in result.stderr
    usage = run('--help').stdout
    assert re.search(r'discover .*\[--accept-protocol-version <YYYY-MM-DD>\]\.\.\.', usage)
    assert 'docs/slim.md' in usage and 'README' not in usage
