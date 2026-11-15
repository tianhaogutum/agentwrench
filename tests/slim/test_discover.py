"""Port of test/discover.test.js."""

import json
import os
import re
import stat

import pytest

from .conftest import PYTHON, run

SLIM_CONFIG = {'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']}


@pytest.fixture
def setup(tmp_dir):
    config = tmp_dir / 'slim.json'
    config.write_text(json.dumps(SLIM_CONFIG))

    def discover(mode='normal', options=()):
        return run('discover', '--config', str(config), *options, '--', PYTHON, 'fixtures/discovery_server.py', mode)

    return tmp_dir, config, discover


def test_discover_follows_empty_opaque_cursors_and_filtered_empty_pages_into_one_offline_catalogue(setup):
    directory, _, discover = setup
    result = discover()
    assert result.returncode == 0, result.stderr
    catalogue = json.loads(result.stdout)
    assert list(catalogue) == ['tools']
    assert [t['name'] for t in catalogue['tools']] == ['read_z', 'read_a', 'read_tail']
    for tool in catalogue['tools']:
        assert tool['description'] == 'Read.'
        assert tool['inputSchema'] == {'type': 'object', 'properties': {'id': {'type': 'integer'}}}
        assert tool['annotations'] == {'readOnlyHint': True}
    path = directory / 'catalogue.json'
    path.write_text(result.stdout)
    measured = run('measure', str(path), '--json')
    assert measured.returncode == 0, measured.stderr
    assert json.loads(measured.stdout)['tools'] == 3
    planned = run('plan', str(path), '--budget', '1000')
    assert planned.returncode == 0, planned.stderr
    assert 'deny' not in json.loads(planned.stdout)


def test_discover_default_config_preserves_every_tool_and_supports_an_entirely_filtered_catalogue(setup):
    _, config, discover = setup
    unchanged = run('discover', '--', PYTHON, 'fixtures/discovery_server.py')
    assert unchanged.returncode == 0, unchanged.stderr
    assert [t['name'] for t in json.loads(unchanged.stdout)['tools']] == ['delete_first', 'read_z', 'read_admin', 'read_a', 'read_tail']
    assert json.loads(unchanged.stdout)['tools'][0]['description'] == 'Read. Extra instructions.'
    config.write_text('{"allow":[]}')
    empty = discover()
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == {'tools': []}


@pytest.mark.parametrize('mode', ['self', 'cycle', 'cursor', 'badtool', 'cache', 'noncomplete-tools', 'duplicate', 'hidden-duplicate',
                                  'error', 'early', 'crash', 'changed', 'request', 'noise', 'wrongid', 'unsafe', 'exit-after', 'version'])
def test_discover_later_page_failures_never_publish_a_partial_catalogue_or_remote_diagnostic_data(setup, mode):
    result = setup[2](mode)
    assert result.returncode == 1, result.stderr
    assert result.stdout == '', mode
    assert not re.search('PRIVATE|tail🦊|delete_first|read_a', result.stderr)


def test_discover_request_deadlines_and_bounded_shutdown_reject_incomplete_output(setup):
    discover = setup[2]
    timeout = discover('timeout', ['--request-timeout-ms', '500'])
    assert timeout.returncode == 1, timeout.stderr
    assert timeout.stdout == ''
    assert re.search('timed out', timeout.stderr)
    stubborn = discover('stubborn')
    assert stubborn.returncode == 1, stubborn.stderr
    assert stubborn.stdout == ''
    assert re.search('shutdown', stubborn.stderr)


def test_discover_response_cannot_disarm_the_deadline_for_a_backpressured_request_write(setup):
    result = setup[2]('backpressure', ['--request-timeout-ms', '500'])
    assert result.returncode == 1, result.stderr
    assert result.stdout == ''
    assert re.search('timed out', result.stderr)


def test_discover_cli_rejects_invalid_options_config_before_starting_its_explicit_executable(setup):
    _, config, _ = setup
    for options in [['--request-timeout-ms', '0'], ['--request-timeout-ms', '1.5'], ['--request-timeout-ms'],
                    ['--request-timeout-ms', '2147483648'], ['--request-timeout-ms', '1', '--request-timeout-ms', '2'],
                    ['--config', str(config), '--config', str(config)], ['--json'], ['unexpected.json']]:
        result = run('discover', *options, '--', '/does/not/exist')
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert not re.search('cannot start server', result.stderr)
    assert run('discover').returncode == 2
    config.write_text('{"unknown":true}')
    invalid = run('discover', '--config', str(config), '--', '/does/not/exist')
    assert invalid.returncode == 2
    assert re.search(r'config\.unknown', invalid.stderr)
    missing = run('discover', '--', '/does/not/exist')
    assert missing.returncode == 1
    assert re.search('cannot start server', missing.stderr)
    assert missing.stdout == ''


def test_discover_output_atomically_replaces_a_sentinel_with_the_ordered_offline_catalogue(setup):
    directory, _, discover = setup
    output = directory / 'catalogue with spaces.json'
    output.write_text('sentinel')
    result = discover('normal', ['--output', str(output)])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    data = output.read_text(encoding='utf-8')
    assert data == discover().stdout
    assert [tool['name'] for tool in json.loads(data)['tools']] == ['read_z', 'read_a', 'read_tail']
    assert os.lstat(output).st_mode & 0o777 == 0o600
    assert sorted(os.listdir(directory)) == ['catalogue with spaces.json', 'slim.json']
    measured = run('measure', str(output), '--json')
    assert measured.returncode == 0, measured.stderr
    assert json.loads(measured.stdout)['tools'] == 3
    planned = run('plan', str(output), '--budget', '1000')
    assert planned.returncode == 0, planned.stderr
    assert 'deny' not in json.loads(planned.stdout)


def test_discover_output_preserves_existing_and_absent_destinations_on_later_page_failures(setup):
    directory, _, discover = setup
    output = directory / 'catalogue.json'
    for exists in (True, False):
        if exists:
            output.write_text('sentinel')
        else:
            output.unlink()
        for mode in ['self', 'cycle', 'cursor', 'badtool', 'error', 'timeout']:
            result = discover(mode, ['--output', str(output), '--request-timeout-ms', '500'])
            assert result.returncode == 1, f'{mode}: {result.stderr}'
            assert result.stdout == ''
            if exists:
                assert output.read_text() == 'sentinel'
            else:
                assert not os.path.lexists(output)
            assert sorted(os.listdir(directory)) == (['catalogue.json', 'slim.json'] if exists else ['slim.json'])


def test_discover_output_creates_files_replaces_symlinks_and_cleans_up_failed_renames(setup):
    directory, _, discover = setup
    target, link = directory / 'target.json', directory / 'link.json'
    target.write_text('referent')
    os.symlink(target, link)
    replaced = discover('normal', ['--output', str(link)])
    assert replaced.returncode == 0, replaced.stderr
    assert replaced.stdout == ''
    assert not stat.S_ISLNK(os.lstat(link).st_mode)
    assert target.read_text() == 'referent'
    (directory / 'directory').mkdir()
    (directory / 'directory' / 'sentinel').write_text('keep')
    before = sorted(os.listdir(directory))
    failed = discover('normal', ['--output', str(directory / 'directory')])
    assert failed.returncode == 1, failed.stderr
    assert failed.stdout == ''
    assert re.search('output', failed.stderr)
    assert (directory / 'directory' / 'sentinel').read_text() == 'keep'
    assert sorted(os.listdir(directory)) == before
    missing_parent = discover('normal', ['--output', str(directory / 'absent' / 'catalogue.json')])
    assert missing_parent.returncode == 1, missing_parent.stderr
    assert missing_parent.stdout == ''
    assert re.search('output.*ENOENT', missing_parent.stderr)
    assert sorted(os.listdir(directory)) == before
    created = discover('normal', ['--output', str(directory / 'new.json')])
    assert created.returncode == 0, created.stderr
    assert created.stdout == ''
    assert (directory / 'new.json').read_text() == link.read_text()


def test_discover_output_validates_option_values_before_spawning_and_is_discovery_only():
    for options in [['--output'], ['--output', ''], ['--output', 'a', '--output', 'b']]:
        result = run('discover', *options, '--', '/does/not/exist')
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--output: (missing value|duplicate option)', result.stderr)
        assert not re.search('cannot start server', result.stderr)
    for mode in ['proxy', 'measure', 'plan']:
        result = run(mode, '--output', 'a')
        assert result.returncode == 2, result.stderr
        assert re.search('--output: unsupported', result.stderr)
    assert re.search(r'discover \[--output', run('--help').stdout)


@pytest.mark.parametrize('suffix', ['catalogue.json/', 'catalogue.json/.', 'missing/../catalogue.json'])
def test_discover_output_path_traversal_errors_preserve_the_sentinel_and_leave_no_temporary_file(setup, suffix):
    directory, _, discover = setup
    output = directory / 'catalogue.json'
    output.write_text('sentinel')
    before = sorted(os.listdir(directory))
    result = discover('normal', ['--output', f'{directory}/{suffix}'])
    assert result.returncode == 1, result.stderr
    assert result.stdout == ''
    assert re.search('discovery output failed', result.stderr)
    assert output.read_text() == 'sentinel'
    assert sorted(os.listdir(directory)) == before


def test_discover_output_path_follows_a_symlink_parent_before_traversing_dot_dot(setup):
    directory, _, discover = setup
    os.makedirs(directory / 'actual' / 'child')
    os.symlink(directory / 'actual' / 'child', directory / 'link', target_is_directory=True)
    (directory / 'catalogue.json').write_text('lexical sentinel')
    (directory / 'actual' / 'catalogue.json').write_text('destination sentinel')
    result = discover('normal', ['--output', f'{directory}/link/../catalogue.json'])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert (directory / 'catalogue.json').read_text() == 'lexical sentinel'
    assert (directory / 'actual' / 'catalogue.json').read_text(encoding='utf-8') == discover().stdout
    assert sorted(os.listdir(directory)) == ['actual', 'catalogue.json', 'link', 'slim.json']
    assert sorted(os.listdir(directory / 'actual')) == ['catalogue.json', 'child']
    assert os.listdir(directory / 'actual' / 'child') == []
    assert stat.S_ISLNK(os.lstat(directory / 'link').st_mode)
