"""Opt-in CI policy gates emitted kinds without changing default reports."""
import itertools
import json
import socket
import subprocess

import pytest

from agentwrench.mcpconfig.cli import main
from agentwrench.mcpconfig.formats import ConfigError, FORMATS
from .test_bridge import invoke
from .test_json_report import source, state


KINDS = ('loss', 'secret', 'interpolation')
SELECTIONS = [c for n in range(4) for c in itertools.combinations(KINDS, n)]


def policy(file, selected=(), fmt='claude', to='codex', code=0):
    before = state(file.parent)
    result = invoke('report', '--from', fmt, '--to', to, file, '--policy-json-v1',
                    *[arg for kind in selected for arg in ('--fail-on', kind)])
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == '' and state(file.parent) == before
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'findings', 'policy', 'error'}
    assert data['schemaVersion'] == 1 and data['operation'] == 'report-policy'
    assert data['status'] == {0: 'accepted', 1: 'rejected', 2: 'error', 3: 'unsupported'}[code]
    assert (data['error'] is None) == (code in (0, 1))
    assert set(data['policy']) == {'failOn', 'matchedKinds', 'decision'}
    assert data['policy']['failOn'] == [k for k in KINDS if k in selected]
    if code == 2:
        assert data['findings'] == []
        assert data['policy']['matchedKinds'] is None
        assert data['policy']['decision'] == 'not-evaluated'
    else:
        matches = [k for k in KINDS if k in selected and any(f['kind'] == k for f in data['findings'])]
        assert data['policy']['matchedKinds'] == matches
        assert data['policy']['decision'] == ('rejected' if matches else 'accepted')
    for f in data['findings']:
        assert set(f) == {'scope', 'server', 'path', 'field', 'kind', 'message'}
        assert (f['server'] is None) == (f['scope'] == 'document')
        assert f['path'] and all(isinstance(p, str) or type(p) is int and p >= 0 for p in f['path'])
    assert all(v not in result.stdout for v in ('native-value', 'opaque-value', 'sensitive-value', '${LITERAL}'))
    return data


def test_selected_warning_rejects_instead_of_reporting_success(tmp_path):
    file = source(tmp_path, {'s': {'command': 'native-value', 'args': ['${LITERAL}']}})
    default = invoke('report', '--from', 'claude', '--to', 'codex', file, '--json-v1')
    assert default.returncode == 0
    data = policy(file, ('interpolation',), code=1)
    assert data['findings'] == json.loads(default.stdout)['findings']


@pytest.mark.parametrize('actual', KINDS)
@pytest.mark.parametrize('selected', SELECTIONS)
def test_selected_kinds_and_nonselected_warnings(tmp_path, actual, selected):
    field = {'loss': {'vendor.dot': {'': 'opaque-value'}},
             'secret': {'env': {'API_TOKEN': 'sensitive-value'}},
             'interpolation': {'args': ['${LITERAL}']}}[actual]
    file = source(tmp_path, {'s': {'command': 'native-value', **field}})
    data = policy(file, selected, code=int(actual in selected))
    assert [f['kind'] for f in data['findings']] == [actual]


@pytest.mark.parametrize('selected', SELECTIONS)
def test_combinations_and_duplicate_argument_order_are_deterministic(tmp_path, selected):
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'args': ['${LITERAL}'],
                                         'env': {'API_TOKEN': 'sensitive-value'},
                                         'vendor': {'a.b': 1, 'a': {'b': 2}}}})
    data = policy(file, selected, code=int(bool(selected)))
    assert {f['kind'] for f in data['findings']} == set(KINDS)
    assert data == policy(file, (*reversed(selected), *selected), code=int(bool(selected)))
    assert [f['path'] for f in data['findings'] if f['kind'] == 'loss'] == [
        ['vendor', 'a.b'], ['vendor', 'a', 'b']]


@pytest.mark.parametrize('fmt', FORMATS)
def test_all_native_formats_and_same_format_secrets(tmp_path, fmt):
    file = source(tmp_path, {'worker.工具': {'command': 'native-value', 'args': ['${LITERAL}'],
                                         'env': {'API_TOKEN': 'sensitive-value'},
                                         'vendor.dot': {'': 'opaque-value'}}}, fmt)
    data = policy(file, KINDS, fmt, 'claude' if fmt == 'codex' else 'codex', 1)
    assert [f['path'] for f in data['findings'] if f['scope'] == 'server'] == [
        ['env', 'API_TOKEN'], ['args', 0], ['vendor.dot', '']]
    same = policy(file, KINDS, fmt, fmt, 1)
    assert [f['kind'] for f in same['findings']] == ['secret']


def test_document_scope_masking_controls_and_typed_indices(tmp_path):
    token = 'ghp_' + 'a' * 30
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'vendor': {
        token: [{'control\n.key': '${LITERAL}', 'API_TOKEN': 'sensitive-value'}],
        'rows[0]': {'0': None}}}, token: {'command': 'native-value'}})
    raw = json.loads(file.read_text()); raw['auth.dot'] = [{'API_TOKEN.' + token: 'sensitive-value'}]
    raw[''] = None; file.write_text(json.dumps(raw))
    data = policy(file, KINDS, code=1)
    assert token not in json.dumps(data)
    assert any(f['scope'] == 'server' and f['server'] == '(document)' and
               f['path'] == ['vendor', '[REDACTED]', 0, 'control\n.key'] for f in data['findings'])
    assert any(f['scope'] == 'document' and f['server'] is None and
               f['path'] == ['auth.dot', 0, 'API_TOKEN.[REDACTED]'] and f['kind'] == 'secret'
               for f in data['findings'])
    assert any(f['path'] == ['vendor', 'rows[0]', '0'] for f in data['findings'])
    assert any(f['scope'] == 'document' and f['path'] == [''] for f in data['findings'])
    # Document-only findings can reject; same-format suppresses losses, not secrets.
    raw['mcpServers'] = {}; file.write_text(json.dumps(raw))
    data = policy(file, ('secret',), to='claude', code=1)
    assert all(f['scope'] == 'document' for f in data['findings'])


@pytest.mark.parametrize('selected', SELECTIONS)
def test_unsupported_precedes_policy_rejection_and_retains_complete_findings(tmp_path, selected):
    file = source(tmp_path, {'bad': {'type': 'sse', 'url': 'https://example.test/sse'},
                             'safe': {'command': 'native-value', 'args': ['${LITERAL}'],
                                      'env': {'API_TOKEN': 'sensitive-value'}, 'vendor': 1}})
    data = policy(file, selected, code=3)
    assert data['error'] == 'unsupported server conversion; no files written'
    assert {f['kind'] for f in data['findings']} == {*KINDS, 'error'}


@pytest.mark.parametrize('invalid', ['syntax', 'schema', 'duplicate', 'missing', 'utf8', 'depth'])
def test_invalid_precedes_unsupported_and_policy_without_partial_findings(tmp_path, invalid):
    file = source(tmp_path, {'safe': {'command': 'native-value', 'vendor': 1},
                             'bad': {'type': 'sse', 'url': 'https://example.test'},
                             'invalid': {'command': 42}})
    if invalid == 'syntax':
        file.write_text('{"broken":"sensitive-value"')
    elif invalid == 'duplicate':
        file.write_text('{"sensitive-value":1,"sensitive-value":2}')
    elif invalid == 'missing':
        file.unlink()
    elif invalid == 'utf8':
        file.write_bytes(b'\xffsensitive-value')
    elif invalid == 'depth':
        file.write_text('{"meta":' + '[' * 70 + '0' + ']' * 70 + '}')
    policy(file, (*reversed(KINDS), 'loss'), code=2)


@pytest.mark.parametrize('exception', [ConfigError, ValueError, TypeError, OSError, RecursionError])
def test_processing_failure_discards_collected_findings(tmp_path, monkeypatch, capsys, exception):
    file = source(tmp_path, {'s': {'command': 'native-value', 'vendor': 1}})
    token = 'ghp_' + 'b' * 30
    def fail(*args):
        raise exception(token)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', fail)
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v1', '--fail-on', 'loss']) == 2
    captured = capsys.readouterr()
    assert captured.err == '' and token not in captured.out
    data = json.loads(captured.out)
    assert data['status'] == 'error' and data['findings'] == [] and data['error']
    assert data['policy'] == {'failOn': ['loss'], 'matchedKinds': None, 'decision': 'not-evaluated'}


@pytest.mark.parametrize('selected', [(), KINDS])
@pytest.mark.parametrize('empty', [True, False])
def test_no_findings_is_accepted_including_empty_collection(tmp_path, selected, empty):
    file = source(tmp_path, {} if empty else {'s': {'command': 'native-value'}})
    assert policy(file, selected)['findings'] == []


def test_policy_never_renders_writes_expands_executes_or_connects(tmp_path, monkeypatch, capsys):
    file = source(tmp_path, {'s': {'command': 'native-value', 'args': ['${LITERAL}']}})
    (tmp_path / 'source.bak').write_text('existing-backup')
    before = state(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected side effect or rendering')
    for name in ('new_document', 'atomic_write', 'validate_write'):
        monkeypatch.setattr('agentwrench.mcpconfig.cli.' + name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setenv('LITERAL', 'must-not-expand')
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v1', '--fail-on', 'interpolation']) == 1
    captured = capsys.readouterr()
    assert captured.err == '' and 'must-not-expand' not in captured.out
    assert json.loads(captured.out)['findings'][0]['path'] == ['args', 0]
    assert state(tmp_path) == before


@pytest.mark.parametrize('extra', [
    ['--fail-on', 'loss'], ['--json-v1', '--fail-on', 'secret'], ['--json', '--fail-on', 'loss'],
    ['--policy-json-v1', '--json'], ['--policy-json-v1', '--json-v1'],
    ['--policy-json-v1', '--fail-on', 'error'], ['--policy-json-v1', '--fail-on', 'LOSS'],
    ['--policy-json-v1', '--fail-on', 'loss,secret'], ['--policy-json-v1', '--fail-on', ''],
    ['--policy-json-v1', '--fail-on'], ['--policy-json-v1', '-o', 'output'],
])
def test_invalid_policy_arguments_are_usage_errors_before_read(extra, monkeypatch, capsys):
    def forbidden(*args):
        raise AssertionError('read before valid policy arguments')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', forbidden)
    with pytest.raises(SystemExit) as exc:
        main(['report', '--from', 'claude', '--to', 'codex', 'missing', *extra])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'usage:' in captured.err


@pytest.mark.parametrize('args', [
    ['report', '--from', 'claude', '--to', 'codex', '--policy-json-v1'],
    ['report', '--from', 'unknown', '--to', 'codex', 'missing', '--policy-json-v1'],
    ['check', '--from', 'claude', '--to', 'codex', '--source', 'missing', '--target', 'missing', '--policy-json-v1'],
    ['convert', '--from', 'claude', '--to', 'codex', 'missing', '--policy-json-v1'],
])
def test_required_input_and_report_only_options(args):
    result = invoke(*args)
    assert result.returncode == 2 and result.stdout == '' and 'usage:' in result.stderr
