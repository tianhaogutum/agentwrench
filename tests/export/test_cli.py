import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from .conftest import SESSIONS, FIXTURES, codex_item, user

ROOT = Path(__file__).resolve().parents[2]  # repository root


def run(*args):
    return subprocess.run([sys.executable, '-m', 'agentwrench', 'export', *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('fmt', SESSIONS)
@pytest.mark.parametrize('target', ['atif', 'md', 'jsonl'])
def test_cli_success(fmt, target):
    result = run('convert', SESSIONS[fmt], '--to', target, '--stats-json')
    assert result.returncode == 0, result.stderr
    assert result.stdout
    diag = json.loads(result.stderr.removeprefix('agentwrench export: '))
    assert diag['warnings'] == {}
    if target == 'atif':
        assert json.loads(result.stdout)['schema_version'] == 'ATIF-v1.8'


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('target', ['atif', 'jsonl'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_unicode_jsonl_data_roundtrip(tmp_path, fmt, target, ending):
    text = 'alpha\u2028beta\u0085gamma\u2029delta'
    if fmt == 'codex':
        rows = [user(text),
                codex_item({'type': 'function_call', 'name': 'echo', 'call_id': 't',
                            'arguments': {text: text}}),
                codex_item({'type': 'function_call_output', 'call_id': 't', 'output': text})]
    else:
        rows = [{'type': 'user', 'message': {'role': 'user', 'content': text}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'echo', 'id': 't', 'input': {text: text}}]}},
                {'type': 'user', 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 't', 'content': text}]}}]
    path = tmp_path / 'unicode.jsonl'
    outputs = []
    for escaped in (False, True):
        path.write_bytes((''.join(json.dumps(row, ensure_ascii=escaped) + ending
                                 for row in rows)).encode('utf-8'))
        result = run('convert', path, '--to', target, '--redact', 'off', '--stats-json')
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stderr.removeprefix('agentwrench export: '))['warnings'] == {}
        # Only LF delimits exported JSONL, too: splitlines() corrupts these values.
        steps = (json.loads(result.stdout)['steps'] if target == 'atif' else
                 [json.loads(line) for line in result.stdout.split('\n')[1:-1]])
        assert len(steps) == 2
        assert steps[0]['message'] == text
        assert steps[0]['extra']['record'] == rows[0]
        assert steps[1]['tool_calls'][0]['arguments'] == {text: text}
        assert steps[1]['observation']['results'][0]['content'] == text
        assert steps[1]['observation']['results'][0]['extra']['record'] == rows[2]
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_private_output_and_no_overwrite(tmp_path):
    output = tmp_path / 'out.json'
    result = run('convert', SESSIONS['claude'], '--to', 'atif', '-o', output)
    assert result.returncode == 0 and not result.stdout
    assert output.stat().st_mode & 0o777 == 0o600
    old = output.read_bytes()
    result = run('convert', SESSIONS['codex'], '--to', 'atif', '-o', output)
    assert result.returncode == 2
    assert output.read_bytes() == old


def test_output_symlink_not_followed(tmp_path):
    target = tmp_path / 'target'
    target.write_text('original')
    link = tmp_path / 'link'
    link.symlink_to(target)
    result = run('summary', SESSIONS['cline'], '-o', link)
    assert result.returncode == 2
    assert target.read_text() == 'original'


def test_input_never_overwritten():
    path = SESSIONS['codex']
    original = path.read_bytes()
    result = run('convert', path, '--to', 'md', '-o', path)
    assert result.returncode == 2
    assert path.read_bytes() == original


@pytest.mark.parametrize('tail', ['{}\n', '[]\n', '{"type":"unknown"}\n', ''])
def test_undetectable_exit2(tmp_path, tail):
    path = tmp_path / 'input.jsonl'
    path.write_text(tail)
    result = run('convert', path, '--to', 'md')
    assert result.returncode == 2 and not result.stdout


def test_missing_input_is_named_with_tokens_redacted(tmp_path):
    result = run('summary', tmp_path / 'missing-session.jsonl')
    assert result.returncode == 2 and not result.stdout
    assert result.stderr == f"agentwrench export: error: cannot read {tmp_path / 'missing-session.jsonl'}: no such file\n"
    result = run('summary', tmp_path / 'sk-abcdefghijklmnop1234.jsonl')
    assert result.returncode == 2 and 'abcdefghijklmnop1234' not in result.stderr and '[REDACTED]' in result.stderr


def test_summary_stderr_is_one_readable_line_and_json_on_request(tmp_path):
    out = tmp_path / 'handoff.md'
    result = run('summary', FIXTURES / 'codex/rollout-secrets.jsonl', '-o', out)
    assert result.returncode == 0 and out.exists()
    assert result.stderr == (f"agentwrench export: wrote {out}: 2 steps, 1 warning (unknown_record 1), "
                             "25 values redacted\n")
    result = run('convert', FIXTURES / 'codex/rollout-secrets.jsonl', '--to', 'md', '--redact', 'off')
    assert result.stderr.endswith(", redaction off\n") and '{' not in result.stderr
    result = run('summary', FIXTURES / 'codex/rollout-secrets.jsonl', '--stats-json')
    stats = json.loads(result.stderr.removeprefix('agentwrench export: '))
    assert stats['redaction']['total'] == 25 and stats['warnings'] == {'unknown_record': 1}


def test_source_commands_are_inert(make_jsonl, tmp_path):
    marker = tmp_path / 'MUST_NOT_EXIST'
    rows = [user(), codex_item({'type': 'function_call', 'name': 'exec_command', 'call_id': 't',
                              'arguments': {'cmd': f'touch {marker}', 'path': '../../escape'}})]
    result = run('summary', make_jsonl(rows))
    assert result.returncode == 0
    assert not marker.exists()


def test_secret_fixture_stdout_and_stderr():
    result = run('convert', FIXTURES / 'codex/rollout-secrets.jsonl', '--to', 'md')
    assert result.returncode == 0
    assert 'sk-' not in result.stdout + result.stderr
    assert 'synthetic-json-secret' not in result.stdout + result.stderr
    assert '25 values redacted' in result.stderr


def test_summary_cli_and_explicit_format():
    result = run('summary', SESSIONS['cline'], '--from', 'cline')
    assert result.returncode == 0 and '# Hand-off brief' in result.stdout


@pytest.mark.parametrize('arguments', [('--to', 'bad'), ('--to', 'md', '--max-output-chars', '-1'),
                                       ('--to', 'md', '--from', 'cline'), ('--to', 'md', '--redact', 'bad')])
def test_cli_options_failure(arguments):
    result = run('convert', SESSIONS['codex'], *arguments)
    assert result.returncode == 2 and not result.stdout


def test_fifo_input_rejected_without_blocking(tmp_path):
    fifo = tmp_path / 'pipe'
    os.mkfifo(fifo)
    result = run('convert', fifo, '--to', 'md')
    assert result.returncode == 2


def test_malformed_shapes_do_not_traceback(make_jsonl):
    for row in [{'type': 'assistant', 'message': []},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [None]}},
                codex_item({'type': 'function_call', 'arguments': [], 'name': None})]:
        result = run('convert', make_jsonl([row]), '--to', 'atif')
        assert result.returncode == 2
        assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('bad', ['{"type":"response_item","type":"event_msg","payload":{}}\n',
                                  '{"type":"session_meta","payload":{"x":1e999}}\n'])
def test_duplicate_fields_and_nonfinite_numbers_rejected(tmp_path, bad):
    path = tmp_path / 'bad.jsonl'
    path.write_text(bad)
    result = run('convert', path, '--from', 'codex', '--to', 'md')
    assert result.returncode == 2 and not result.stdout
