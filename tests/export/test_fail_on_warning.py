"""Warning policy affects only CLI status; the strict API still returns counts."""
import json

import pytest

from .conftest import codex_item, user
from .test_cli import run
from agentwrench.export import validate_session
from agentwrench.export.model import InputError


def message(fmt):
    return user() if fmt == 'codex' else {
        'type': 'user', 'message': {'role': 'user', 'content': 'hello'}}


def missing_id(fmt):
    return (codex_item({'type': 'function_call', 'name': 'echo'}) if fmt == 'codex' else
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'name': 'echo', 'input': {}}]}})


def write(path, rows, ending):
    # Include a blank physical line and an unterminated final record.
    path.write_bytes((ending + ending.join(json.dumps(row) for row in rows)).encode('utf-8'))


def check_policy(path, expected, *options):
    before = path.read_bytes()
    assert validate_session(path, include_sidechains='--include-sidechains' in options) == expected
    for flag in ([], ['--fail-on-warning']):
        result = run('validate', path, *options, *flag)
        assert result.stdout == ''
        assert result.stderr == 'agentwrench export: ' + json.dumps(expected, sort_keys=True) + '\n'
        assert result.returncode == (2 if flag and expected['warning_count'] else 0)
        assert list(path.parent.iterdir()) == [path]
        assert path.read_bytes() == before


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('warning', ['unknown', 'missing-id', 'mixed', 'none'])
def test_warning_policy_preserves_counts_and_no_output(tmp_path, fmt, ending, warning):
    rows = [message(fmt)]
    warnings = {}
    if warning in ('unknown', 'mixed'):
        rows += [{'type': 'future_record'}, {'type': 'future_record'}]
        warnings['unknown_record'] = 2
    if warning in ('missing-id', 'mixed'):
        rows += [missing_id(fmt)]
        warnings['missing_call_id'] = 1
    path = tmp_path / 'session.jsonl'
    write(path, rows, ending)
    check_policy(path, {'format': fmt, 'records': len(rows),
                        'steps': 1 + ('missing_call_id' in warnings),
                        'warnings': warnings, 'warning_count': sum(warnings.values())})


@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_warning_policy_follows_selected_sidechains(tmp_path, ending):
    rows = [message('claude'), {'type': 'future_record', 'isSidechain': True},
            dict(missing_id('claude'), isSidechain=True)]
    path = tmp_path / 'session.jsonl'
    write(path, rows, ending)
    for include in (False, True):
        check_policy(path, {'format': 'claude', 'records': 3, 'steps': 2 if include else 1,
                            'warnings': {'unknown_record': 1, 'missing_call_id': 1} if include else {},
                            'warning_count': 2 if include else 0},
                     *(['--include-sidechains'] if include else []))


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('tail,error', [('{"private":', 'invalid JSON at line 4'),
                                       ('{"x":1,"x":2}', 'invalid JSON at line 4'),
                                       ('[]', 'JSONL line 4 must be an object')])
def test_warning_policy_does_not_mask_corrupt_input(tmp_path, fmt, ending, tail, error):
    path = tmp_path / 'session.jsonl'
    write(path, [message(fmt), {'type': 'future_record'}], ending)
    path.write_bytes(path.read_bytes() + ending.encode() + tail.encode())
    with pytest.raises(InputError, match='^' + error + '$'):
        validate_session(path)
    for flag in ([], ['--fail-on-warning']):
        result = run('validate', path, *flag)
        assert result.returncode == 2 and result.stdout == ''
        assert result.stderr == 'agentwrench export: error: ' + error + '\n'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('data', [b'', b' \t\r\n\r\n', b'\xef\xbb\xbf'])
def test_warning_policy_does_not_mask_empty_input(tmp_path, data):
    path = tmp_path / 'session.jsonl'
    path.write_bytes(data)
    with pytest.raises(InputError) as error:
        validate_session(path)
    result = run('validate', path, '--fail-on-warning')
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr == f'agentwrench export: error: {error.value}\n'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('options', [['-o', 'output'], ['--to', 'jsonl'], ['--redact', 'off']])
def test_warning_policy_does_not_add_export_options(tmp_path, options):
    path = tmp_path / 'session.jsonl'
    write(path, [message('claude')], '\n')
    result = run('validate', path, '--fail-on-warning', *options)
    assert result.returncode == 2 and result.stdout == ''
    assert 'unrecognized arguments: ' + ' '.join(options) in result.stderr
    assert list(tmp_path.iterdir()) == [path]
