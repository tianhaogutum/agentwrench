"""Opt-in JSONL validation must finish before any CLI output is published."""
import json

import pytest

from .conftest import SESSIONS, user
from .test_cli import run
from agentwrench.export import load_session
from agentwrench.export.adapters import read_jsonl
from agentwrench.export.model import InputError


TEXT = 'alpha\u2028beta\u0085gamma\u2029delta'


def session_bytes(fmt, ending, *, final_ending=True):
    rows = ([user(TEXT), user('last good record')] if fmt == 'codex' else
            [{'type': 'user', 'message': {'role': 'user', 'content': text}}
             for text in (TEXT, 'last good record')])
    # A blank physical line verifies error line numbers without Unicode splitting.
    data = ending.join(['', *(json.dumps(row, ensure_ascii=False) for row in rows)])
    return (data + (ending if final_ending else '')).encode('utf-8'), rows


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('final_ending', [False, True])
def test_strict_api_accepts_complete_records(tmp_path, fmt, ending, final_ending):
    path = tmp_path / 'valid.jsonl'
    data, rows = session_bytes(fmt, ending, final_ending=final_ending)
    path.write_bytes(data)
    assert read_jsonl(path, strict_jsonl=True) == (rows, False)
    strict = load_session(path, strict_jsonl=True)
    assert strict == load_session(path)
    assert [step['message'] for step in strict['steps']] == [TEXT, 'last good record']
    assert strict['extra']['warnings'] == {}


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('tail', ['{"private":"' + TEXT, '{broken}', '{"x":1,"x":2}', '{"x":NaN}'])
def test_strict_api_rejects_corrupt_tail_default_keeps_prior_records(tmp_path, fmt, ending, tail):
    path = tmp_path / 'corrupt.jsonl'
    data, rows = session_bytes(fmt, ending)
    path.write_bytes(data + tail.encode('utf-8'))
    assert read_jsonl(path) == (rows, True)
    default = load_session(path)
    assert [step['message'] for step in default['steps']] == [TEXT, 'last good record']
    assert default['extra']['warnings'] == {'truncated_last_line': 1}
    for read in (read_jsonl, load_session):
        with pytest.raises(InputError, match='^invalid JSON at line 4$'):
            read(path, strict_jsonl=True)


@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('tail_ending', ['\n', '\r\n', '\r'])
def test_strict_api_rejects_malformed_terminated_records_and_bare_cr_tail(tmp_path, ending, tail_ending):
    path = tmp_path / 'corrupt.jsonl'
    path.write_bytes(session_bytes('codex', ending)[0] + ('{broken}' + tail_ending).encode())
    with pytest.raises(InputError, match='^invalid JSON at line 4$'):
        load_session(path, strict_jsonl=True)


@pytest.mark.parametrize('content', [b'', b' \t\r\n\r\n', b'\xef\xbb\xbf'])
def test_strict_api_empty_has_no_truncation_but_is_not_a_session(tmp_path, content):
    path = tmp_path / 'empty.jsonl'
    path.write_bytes(content)
    assert read_jsonl(path, strict_jsonl=True) == ([], False)
    for fmt in ('auto', 'codex', 'claude'):
        with pytest.raises(InputError):
            load_session(path, fmt, strict_jsonl=True)


@pytest.mark.parametrize('action', ['convert', 'summary'])
@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_strict_cli_tail_failure_publishes_nothing(tmp_path, action, fmt, ending):
    path = tmp_path / 'private-input.jsonl'
    data, _ = session_bytes(fmt, ending)
    path.write_bytes(data + ('{"private":"' + TEXT).encode('utf-8'))
    command = [action, path, '--redact', 'off']
    if action == 'convert':
        command += ['--to', 'jsonl']
    default = run(*command, '--stats-json')
    assert default.returncode == 0 and default.stdout
    assert json.loads(default.stderr.removeprefix('agentwrench export: '))['warnings'] == {'truncated_last_line': 1}
    for output in ([], ['-o', tmp_path / 'new.jsonl']):
        strict = run(*command, '--strict-jsonl', *output)
        assert strict.returncode == 2 and strict.stdout == ''
        assert strict.stderr == 'agentwrench export: error: invalid JSON at line 4\n'
        assert not (tmp_path / 'new.jsonl').exists()


@pytest.mark.parametrize('action', ['convert', 'summary'])
@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_strict_cli_complete_unterminated_record_succeeds(tmp_path, action, fmt, ending):
    path = tmp_path / 'valid.jsonl'
    path.write_bytes(session_bytes(fmt, ending, final_ending=False)[0])
    output = tmp_path / 'out'
    command = [action, path, '--strict-jsonl', '--redact', 'off', '-o', output, '--stats-json']
    if action == 'convert':
        command += ['--to', 'atif']
    result = run(*command)
    assert result.returncode == 0 and not result.stdout
    assert output.stat().st_mode & 0o777 == 0o600
    content = output.read_text(encoding='utf-8')
    if action == 'convert':
        assert [step['message'] for step in json.loads(content)['steps']] == [TEXT, 'last good record']
    else:
        assert TEXT in content and 'last good record' in content
    assert json.loads(result.stderr.removeprefix('agentwrench export: '))['warnings'] == {}


@pytest.mark.parametrize('action', ['convert', 'summary'])
def test_strict_cli_preserves_existing_outputs_and_symlinks(tmp_path, action):
    path = tmp_path / 'corrupt.jsonl'
    path.write_bytes(session_bytes('codex', '\n')[0] + b'{broken}')
    output = tmp_path / 'existing'
    output.write_bytes(b'original')
    link = tmp_path / 'symlink'
    link.symlink_to(output)
    for destination in (output, link):
        command = [action, path, '--strict-jsonl', '-o', destination]
        if action == 'convert':
            command += ['--to', 'atif']
        result = run(*command)
        assert result.returncode == 2 and not result.stdout
        assert result.stderr == 'agentwrench export: error: invalid JSON at line 4\n'
        assert output.read_bytes() == b'original'
        assert link.is_symlink()


@pytest.mark.parametrize('action', ['convert', 'summary'])
def test_strict_cli_empty_creates_no_output(tmp_path, action):
    path = tmp_path / 'empty.jsonl'
    path.write_bytes(b'')
    output = tmp_path / 'out'
    command = [action, path, '--strict-jsonl', '-o', output]
    if action == 'convert':
        command += ['--to', 'atif']
    result = run(*command)
    assert result.returncode == 2 and not result.stdout
    assert result.stderr == 'agentwrench export: error: format not detectable or ambiguous; use --from\n'
    assert not output.exists()


def test_strict_jsonl_option_leaves_cline_json_validation_unchanged():
    assert load_session(SESSIONS['cline'], strict_jsonl=True) == load_session(SESSIONS['cline'])
    result = run('summary', SESSIONS['cline'], '--strict-jsonl')
    assert result.returncode == 0 and '# Hand-off brief' in result.stdout
