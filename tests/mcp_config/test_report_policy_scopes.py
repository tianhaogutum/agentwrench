"""Scope policies gate selected metadata without filtering the diagnostic list."""
import json

from .test_bridge import invoke
from .test_json_report import source, state


def test_document_only_warning_can_be_excluded_without_hiding_it(tmp_path):
    file = source(tmp_path, {})
    file.write_text(json.dumps({'mcpServers': {}, 'metadata': 'opaque-value'}))
    before = state(tmp_path)
    result = invoke('report', '--from', 'claude', '--to', 'codex', file,
                    '--policy-json-v2', '--fail-on', 'loss', '--fail-scope', 'server')
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data['schemaVersion'] == 2 and data['status'] == 'accepted'
    assert data['policy'] == {'failOn': ['loss'], 'scopes': ['server'],
                              'matchedKinds': [], 'decision': 'accepted'}
    assert len(data['findings']) == 1
    assert data['findings'][0]['scope'] == 'document'
    assert data['findings'][0]['server'] is None
    assert result.stderr == '' and state(tmp_path) == before

import itertools
import socket
import subprocess

import pytest

from agentwrench.mcpconfig.cli import main
from agentwrench.mcpconfig.formats import ConfigError, FORMATS
from .test_report_policy import KINDS, SELECTIONS

SCOPES = ('server', 'document')
SCOPE_SELECTIONS = [None, ('server',), ('document',), ('document', 'server', 'document')]


def scoped(file, selected=(), scopes=None, fmt='claude', to='codex', code=0):
    before = state(file.parent)
    result = invoke('report', '--from', fmt, '--to', to, file, '--policy-json-v2',
                    *[a for k in selected for a in ('--fail-on', k)],
                    *[a for s in scopes or () for a in ('--fail-scope', s)])
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == '' and state(file.parent) == before
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'findings', 'policy', 'error'}
    assert data['schemaVersion'] == 2 and data['operation'] == 'report-policy'
    assert data['status'] == {0: 'accepted', 1: 'rejected', 2: 'error', 3: 'unsupported'}[code]
    assert (data['error'] is None) == (code in (0, 1))
    normalized = [s for s in SCOPES if scopes is None or s in scopes]
    matches = None if code == 2 else [k for k in KINDS if k in selected and any(
        f['kind'] == k and f['scope'] in normalized for f in data['findings'])]
    assert data['policy'] == {'failOn': [k for k in KINDS if k in selected], 'scopes': normalized,
                              'matchedKinds': matches,
                              'decision': 'not-evaluated' if code == 2 else 'rejected' if matches else 'accepted'}
    if code == 2:
        assert data['findings'] == []
    else:
        unfiltered = invoke('report', '--from', fmt, '--to', to, file, '--json-v1')
        assert unfiltered.returncode == (3 if code == 3 else 0) and unfiltered.stderr == ''
        assert data['findings'] == json.loads(unfiltered.stdout)['findings']
    assert all(v not in result.stdout for v in ('native-value', 'opaque-value', 'sensitive-value',
                                               '${LITERAL}', 'must-not-expand'))
    for f in data['findings']:
        assert set(f) == {'scope', 'server', 'path', 'field', 'kind', 'message'}
        assert (f['server'] is None) == (f['scope'] == 'document')
    return data


def mixed(tmp_path):
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'args': ['${LITERAL}'],
                                          'vendor.dot': {'': 'opaque-value'}}})
    raw = json.loads(file.read_text())
    raw['auth.dot'] = [{'API_TOKEN': 'sensitive-value'}]
    file.write_text(json.dumps(raw))
    return file


@pytest.mark.parametrize('scopes', SCOPE_SELECTIONS)
@pytest.mark.parametrize('selected', SELECTIONS)
def test_mixed_kinds_match_in_the_same_finding_as_selected_scope(tmp_path, selected, scopes):
    file = mixed(tmp_path)
    actual = {'server': {'loss', 'interpolation'}, 'document': {'loss', 'secret'}}
    included = SCOPES if scopes is None else scopes
    expected = any(k in actual[s] for s in included for k in selected)
    data = scoped(file, (*reversed(selected), *selected), scopes, code=int(expected))
    assert any(f['server'] == '(document)' and f['scope'] == 'server' for f in data['findings'])
    assert any(f['server'] is None and f['scope'] == 'document' for f in data['findings'])


@pytest.mark.parametrize('scope,kind', [('server', k) for k in KINDS] +
                         [('document', 'loss'), ('document', 'secret')])
@pytest.mark.parametrize('selected_scope', SCOPES)
def test_one_actual_scope_is_not_inferred_from_document_display_label(tmp_path, scope, kind, selected_scope):
    field = {'loss': {'vendor': 'opaque-value'}, 'secret': {'env': {'API_TOKEN': 'sensitive-value'}},
             'interpolation': {'args': ['${LITERAL}']}}[kind]
    file = source(tmp_path, {'(document)': {'command': 'native-value', **field}} if scope == 'server' else {})
    if scope == 'document':
        raw = json.loads(file.read_text())
        raw.update({'vendor': 'opaque-value'} if kind == 'loss' else {'API_TOKEN': 'sensitive-value'})
        file.write_text(json.dumps(raw))
    data = scoped(file, (kind,), (selected_scope,), code=int(scope == selected_scope))
    assert all(f['scope'] == scope for f in data['findings'])
    assert any(f['kind'] == kind for f in data['findings'])


@pytest.mark.parametrize('fmt', FORMATS)
@pytest.mark.parametrize('same', [False, True])
def test_native_formats_default_scopes_equal_unfiltered_v1_policy(tmp_path, fmt, same):
    file = source(tmp_path, {'worker.工具': {'command': 'native-value', 'args': ['${LITERAL}'],
                                         'env': {'API_TOKEN': 'sensitive-value'},
                                         'vendor.dot': {'': 'opaque-value'}}}, fmt)
    to = fmt if same else 'claude' if fmt == 'codex' else 'codex'
    v2 = scoped(file, KINDS, fmt=fmt, to=to, code=1)
    v1 = invoke('report', '--from', fmt, '--to', to, file, '--policy-json-v1',
                *[a for k in KINDS for a in ('--fail-on', k)])
    assert v1.returncode == 1 and v1.stderr == ''
    v2['schemaVersion'] = 1
    del v2['policy']['scopes']
    assert v2 == json.loads(v1.stdout)
    if same:
        assert {f['kind'] for f in v2['findings']} == {'secret'}


def test_masked_literal_coordinates_remain_in_nonselected_scope(tmp_path):
    token = 'ghp_' + 'a' * 30
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'vendor': {
        'a.b': 'opaque-value', 'a': {'b': 'opaque-value'},
        token: [{'control\n.key': '${LITERAL}'}]}}})
    raw = json.loads(file.read_text()); raw['auth.' + token] = [{'API_TOKEN': 'sensitive-value'}]
    file.write_text(json.dumps(raw))
    data = scoped(file, ('secret',), ('server',))
    assert token not in json.dumps(data)
    assert any(f['path'] == ['vendor', 'a.b'] for f in data['findings'])
    assert any(f['path'] == ['vendor', 'a', 'b'] for f in data['findings'])
    assert any(f['path'] == ['vendor', '[REDACTED]', 0, 'control\n.key'] for f in data['findings'])
    assert any(f['scope'] == 'document' and f['path'] == ['auth.[REDACTED]', 0, 'API_TOKEN']
               and f['kind'] == 'secret' for f in data['findings'])


@pytest.mark.parametrize('scopes', SCOPE_SELECTIONS)
@pytest.mark.parametrize('selected', [(), ('interpolation',)])
def test_unsupported_exit_precedes_acceptance_and_rejection(tmp_path, selected, scopes):
    file = source(tmp_path, {'bad': {'type': 'sse', 'url': 'https://example.test/sse'},
                             '(document)': {'command': 'native-value', 'args': ['${LITERAL}']}})
    data = scoped(file, selected, scopes, code=3)
    assert data['error'] == 'unsupported server conversion; no files written'
    assert {f['kind'] for f in data['findings']} == {'error', 'interpolation'}


@pytest.mark.parametrize('invalid', ['syntax', 'schema', 'duplicate', 'missing', 'utf8', 'depth'])
def test_invalid_native_input_has_no_partial_findings_or_policy_claim(tmp_path, invalid):
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
    scoped(file, (*reversed(KINDS), 'loss'), ('document', 'server', 'document'), code=2)


@pytest.mark.parametrize('exception', [ConfigError, ValueError, TypeError, OSError, RecursionError])
def test_processing_failure_discards_precollected_findings(tmp_path, monkeypatch, capsys, exception):
    file = mixed(tmp_path)
    def fail(*args):
        raise exception('ghp_' + 'b' * 30)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', fail)
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v2', '--fail-on', 'loss', '--fail-scope', 'server']) == 2
    captured = capsys.readouterr(); data = json.loads(captured.out)
    assert captured.err == '' and 'ghp_' not in captured.out
    assert data['status'] == 'error' and data['findings'] == [] and data['error']
    assert data['schemaVersion'] == 2
    assert data['policy'] == {'failOn': ['loss'], 'scopes': ['server'],
                              'matchedKinds': None, 'decision': 'not-evaluated'}


@pytest.mark.parametrize('scopes', SCOPE_SELECTIONS)
@pytest.mark.parametrize('selected', [(), KINDS])
@pytest.mark.parametrize('empty', [True, False])
def test_no_findings_and_empty_kind_selection(tmp_path, selected, scopes, empty):
    file = source(tmp_path, {} if empty else {'s': {'command': 'native-value'}})
    assert scoped(file, selected, scopes)['findings'] == []


def test_scoped_policy_has_no_render_write_expansion_execution_or_network(tmp_path, monkeypatch, capsys):
    file = mixed(tmp_path)
    (tmp_path / 'source.bak').write_text('existing-backup'); before = state(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected side effect or rendering')
    for name in ('new_document', 'atomic_write', 'validate_write'):
        monkeypatch.setattr('agentwrench.mcpconfig.cli.' + name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setenv('LITERAL', 'must-not-expand')
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v2', '--fail-on', 'secret', '--fail-scope', 'server']) == 0
    captured = capsys.readouterr()
    assert captured.err == '' and 'must-not-expand' not in captured.out
    assert {f['scope'] for f in json.loads(captured.out)['findings']} == set(SCOPES)
    assert state(tmp_path) == before


@pytest.mark.parametrize('extra', [
    ['--fail-scope', 'server'], ['--json', '--fail-scope', 'server'],
    ['--json-v1', '--fail-scope', 'server'], ['--policy-json-v1', '--fail-scope', 'server'],
    *[['--policy-json-v2', mode] for mode in ('--json', '--json-v1', '--policy-json-v1')],
    *[['--policy-json-v2', '--fail-scope', v] for v in
      ('', 'SERVER', 'server,document', '*', '(document)', '["server"]', ' server', 'document ', 'loss')],
    ['--policy-json-v2', '--fail-scope'], ['--policy-json-v2', '--fail-on', 'error'],
    ['--policy-json-v2', '--fail-on', ''], ['--policy-json-v2', '-o', 'output'],
])
def test_invalid_selectors_are_usage_errors_before_input_read(extra, monkeypatch, capsys):
    def forbidden(*args):
        raise AssertionError('read before valid arguments')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', forbidden)
    with pytest.raises(SystemExit) as exc:
        main(['report', '--from', 'claude', '--to', 'codex', 'missing', *extra])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'usage:' in captured.err


@pytest.mark.parametrize('args', [
    ['report', '--from', 'claude', '--to', 'codex', '--policy-json-v2'],
    ['report', '--from', 'unknown', '--to', 'codex', 'missing', '--policy-json-v2'],
    ['check', '--from', 'claude', '--to', 'codex', '--source', 'missing', '--target', 'missing',
     '--policy-json-v2', '--fail-scope', 'server'],
    ['convert', '--from', 'claude', '--to', 'codex', 'missing', '--fail-scope', 'server'],
])
def test_required_input_formats_and_report_only_options(args):
    result = invoke(*args)
    assert result.returncode == 2 and result.stdout == '' and 'usage:' in result.stderr
