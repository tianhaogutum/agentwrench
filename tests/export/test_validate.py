"""Validate complete raw JSONL using the same parser/adapters as conversion."""
import json

import pytest

import agentwrench.export as trajport
from .conftest import SESSIONS, codex_item, user
from .test_cli import run
from .test_strict_jsonl import TEXT, session_bytes
from agentwrench.export.model import InputError


def validate(*args, **kwargs):
    # Resolve at test time so prior implementations fail individual contracts.
    return trajport.validate_session(*args, **kwargs)


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('final_ending', [False, True])
def test_complete_session_counts_api_and_cli(tmp_path, monkeypatch, fmt, ending, final_ending):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'session.jsonl'
    path.write_bytes(session_bytes(fmt, ending, final_ending=final_ending)[0])
    expected = {'format': fmt, 'records': 2, 'steps': 2, 'warnings': {}, 'warning_count': 0}
    before = path.read_bytes()
    assert validate(path) == expected
    assert validate(path, fmt) == expected
    doc = trajport.load_session(path, strict_jsonl=True)
    assert len(doc['steps']) == expected['steps']
    assert doc['steps'][0]['message'] == TEXT
    result = run('validate', path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert result.stderr == 'agentwrench export: ' + json.dumps(expected, sort_keys=True) + '\n'
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == before


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('tail', ['{"private":"' + TEXT, '{"x":1,"x":2}', '{"x":NaN}', '[]'])
def test_corrupt_tail_is_strict_and_reports_physical_line(tmp_path, fmt, ending, tail):
    path = tmp_path / 'private-session.jsonl'
    data, _ = session_bytes(fmt, ending)
    path.write_bytes(data + tail.encode('utf-8'))
    error = ('JSONL line 4 must be an object' if tail == '[]' else 'invalid JSON at line 4')
    with pytest.raises(InputError, match='^' + error + '$'):
        validate(path)
    result = run('validate', path)
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr == 'agentwrench export: error: ' + error + '\n'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('data', [b'', b' \t\r\n\r\n', b'\xef\xbb\xbf'])
def test_empty_is_not_a_session(tmp_path, data):
    path = tmp_path / 'empty.jsonl'
    path.write_bytes(data)
    for fmt in ('auto', 'codex', 'claude'):
        with pytest.raises(InputError):
            validate(path, fmt)
        result = run('validate', path, '--from', fmt)
        assert result.returncode == 2 and result.stdout == ''
        assert 'error:' in result.stderr and 'Traceback' not in result.stderr
    assert list(tmp_path.iterdir()) == [path]


def test_counts_include_metadata_ignored_unknown_and_skipped_sidechains(make_jsonl):
    rows = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'hello'}},
        {'type': 'progress'},
        {'type': 'future_record'},
        {'type': 'future_record'},
        {'type': 'assistant', 'isSidechain': True,
         'message': {'role': 'assistant', 'content': 'sidechain'}},
    ]
    path = make_jsonl(rows)
    for include in (False, True):
        expected = {'format': 'claude', 'records': 5, 'steps': 2 if include else 1,
                    'warnings': {'unknown_record': 2}, 'warning_count': 2}
        assert validate(path, include_sidechains=include) == expected
        result = run('validate', path, *(['--include-sidechains'] if include else []))
        assert result.returncode == 0 and result.stdout == ''
        assert json.loads(result.stderr.removeprefix('agentwrench export: ')) == expected
    path = make_jsonl([{'type': 'session_meta', 'payload': {'id': 's'}},
                       user(), {'type': 'future_record'}])
    assert validate(path) == {'format': 'codex', 'records': 3, 'steps': 1,
                              'warnings': {'unknown_record': 1}, 'warning_count': 1}


def test_validation_uses_adapter_errors_and_warnings(make_jsonl):
    call = codex_item({'type': 'function_call', 'name': 'echo', 'call_id': 'same'})
    path = make_jsonl([user(), call, call])
    with pytest.raises(InputError, match='^duplicate tool call ID in included session$'):
        validate(path)
    result = run('validate', path)
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr == 'agentwrench export: error: duplicate tool call ID in included session\n'
    path = make_jsonl([user(), codex_item({'type': 'function_call', 'name': 'echo'})])
    assert validate(path)['warnings'] == {'missing_call_id': 1}
    # Unknown-only input is not a supported session, even with explicit format.
    path = make_jsonl([{'type': 'future_record'}])
    with pytest.raises(InputError, match='^no supported conversation or tool events in session$'):
        validate(path, 'codex')


def test_syntax_is_checked_even_in_excluded_sidechains(make_jsonl):
    path = make_jsonl([{'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}],
                      tail='{"isSidechain":true,')
    with pytest.raises(InputError, match='^invalid JSON at line 2$'):
        validate(path)


def test_only_raw_jsonl_is_accepted_and_no_export_options(tmp_path):
    for fmt in ('auto', 'codex', 'claude', 'cline'):
        with pytest.raises(InputError):
            validate(SESSIONS['cline'], fmt)
    with pytest.raises(InputError, match='unsupported validation format'):
        validate(SESSIONS['codex'], 'cline')
    result = run('validate', SESSIONS['cline'])
    assert result.returncode == 2 and result.stdout == ''
    for extra in (['-o', tmp_path / 'output'], ['--to', 'jsonl'],
                  ['--from', 'cline'], ['--redact', 'off']):
        result = run('validate', SESSIONS['codex'], *extra)
        assert result.returncode == 2 and result.stdout == ''
    assert list(tmp_path.iterdir()) == []
