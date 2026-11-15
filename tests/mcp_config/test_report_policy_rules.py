"""Paired rules must not turn into a kind/scope cross product."""
import json

from .test_bridge import invoke
from .test_json_report import source, state


def test_server_loss_and_document_secret_do_not_cross(tmp_path):
    file = source(tmp_path, {'(document)': {'command': 'native-value',
                                          'env': {'API_TOKEN': 'sensitive-value'}}})
    raw = json.loads(file.read_text()); raw['metadata'] = 'opaque-value'
    file.write_text(json.dumps(raw))
    before = state(tmp_path)
    # V2 can only select all four pairs, and therefore rejects this fixture.
    old = invoke('report', '--from', 'claude', '--to', 'codex', file,
                 '--policy-json-v2', '--fail-on', 'loss', '--fail-on', 'secret',
                 '--fail-scope', 'server', '--fail-scope', 'document')
    assert old.returncode == 1
    result = invoke('report', '--from', 'claude', '--to', 'codex', file,
                    '--policy-json-v3', '--fail-rule', 'loss:server',
                    '--fail-rule', 'secret:document')
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data['schemaVersion'] == 3 and data['status'] == 'accepted'
    assert data['policy'] == {'rules': [{'kind': 'loss', 'scope': 'server'},
                                      {'kind': 'secret', 'scope': 'document'}],
                              'matchedRules': [], 'decision': 'accepted'}
    assert data['findings'] == json.loads(old.stdout)['findings']
    assert {(f['kind'], f['scope']) for f in data['findings']} == {
        ('secret', 'server'), ('loss', 'document')}
    assert any(f['scope'] == 'server' and f['server'] == '(document)' for f in data['findings'])
    assert result.stderr == '' and state(tmp_path) == before

import itertools
import socket
import subprocess

import pytest

from agentwrench.mcpconfig.cli import main
from agentwrench.mcpconfig.core import Finding
from agentwrench.mcpconfig.formats import ConfigError, FORMATS

KINDS = ('loss', 'secret', 'interpolation')
SCOPES = ('server', 'document')
RULES = tuple(f'{k}:{s}' for k in KINDS for s in SCOPES)
SELECTIONS = [r for n in range(7) for r in itertools.combinations(RULES, n)]


def paired(file, rules=(), *, fmt='claude', to='codex', code=0):
    before = state(file.parent)
    result = invoke('report', '--from', fmt, '--to', to, file, '--policy-json-v3',
                    *[a for r in rules for a in ('--fail-rule', r)])
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == '' and state(file.parent) == before
    assert all(v not in result.stdout for v in ('native-value', 'opaque-value', 'sensitive-value',
                                               '${LITERAL}', 'must-not-expand'))
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'findings', 'policy', 'error'}
    assert data['schemaVersion'] == 3 and data['operation'] == 'report-policy'
    assert data['status'] == {0: 'accepted', 1: 'rejected', 2: 'error', 3: 'unsupported'}[code]
    assert (data['error'] is None) == (code in (0, 1))
    normalized = [dict(zip(('kind', 'scope'), r.split(':'))) for r in RULES if r in rules]
    matches = None if code == 2 else [r for r in normalized if any(
        f['kind'] == r['kind'] and f['scope'] == r['scope'] for f in data['findings'])]
    assert data['policy'] == {'rules': normalized, 'matchedRules': matches,
                              'decision': 'not-evaluated' if code == 2 else 'rejected' if matches else 'accepted'}
    if code == 2:
        assert data['findings'] == []
    else:
        baseline = invoke('report', '--from', fmt, '--to', to, file, '--json-v1')
        assert baseline.returncode == (3 if code == 3 else 0) and baseline.stderr == ''
        assert data['findings'] == json.loads(baseline.stdout)['findings']
    return data


def mixed(tmp_path):
    token = 'ghp_' + 'a' * 30
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'args': ['${LITERAL}'],
                       'env': {'API_TOKEN': 'sensitive-value'},
                       'vendor': {'a.b': 'opaque-value', 'a': {'b': 'opaque-value'},
                                  token: [{'control\n.key': 'opaque-value'}]}}})
    raw = json.loads(file.read_text()); raw['auth.' + token] = [{'API_TOKEN': 'sensitive-value'}]
    file.write_text(json.dumps(raw))
    return file


@pytest.mark.parametrize('rules', SELECTIONS)
def test_all_rule_combinations_normalize_duplicates_and_keep_every_finding(tmp_path, rules):
    data = paired(mixed(tmp_path), (*reversed(rules), *rules),
                  code=int(any(r != 'interpolation:document' for r in rules)))
    assert 'ghp_' not in json.dumps(data)
    assert {(f['kind'], f['scope']) for f in data['findings']} == {
        ('loss', 'server'), ('secret', 'server'), ('interpolation', 'server'),
        ('loss', 'document'), ('secret', 'document')}
    assert any(f['path'] == ['vendor', 'a.b'] for f in data['findings'])
    assert any(f['path'] == ['vendor', 'a', 'b'] for f in data['findings'])
    assert any(f['path'] == ['vendor', '[REDACTED]', 0, 'control\n.key'] for f in data['findings'])
    assert any(f['scope'] == 'document' and f['server'] is None and
               f['path'] == ['auth.[REDACTED]', 0, 'API_TOKEN'] for f in data['findings'])


@pytest.mark.parametrize('actual', RULES)
@pytest.mark.parametrize('selected', RULES)
def test_synthetic_metadata_requires_both_members_of_the_same_pair(tmp_path, monkeypatch, capsys,
                                                                   actual, selected):
    # Exercise the public pipeline with every possible emitted pair, including
    # document interpolation (currently not emitted by native formats).
    kind, scope = actual.split(':')
    finding = Finding('(document)', 'ambiguous.label', kind, 'synthetic diagnostic', ('a.b',), scope)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.project', lambda *a: ({}, [finding]))
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', lambda *a: [])
    file = source(tmp_path, {})
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file), '--policy-json-v3',
                 '--fail-rule', selected]) == int(actual == selected)
    captured = capsys.readouterr(); data = json.loads(captured.out)
    assert captured.err == '' and len(data['findings']) == 1
    assert data['policy']['matchedRules'] == ([dict(zip(('kind', 'scope'), selected.split(':')))]
                                              if actual == selected else [])
    assert data['findings'][0]['scope'] == scope
    assert data['findings'][0]['server'] == ('(document)' if scope == 'server' else None)


@pytest.mark.parametrize('fmt', FORMATS)
@pytest.mark.parametrize('same', [False, True])
def test_all_native_formats_same_format_secrets_and_v2_equivalent_rectangle(tmp_path, fmt, same):
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'args': ['${LITERAL}'],
                    'env': {'API_TOKEN': 'sensitive-value'}, 'vendor': 'opaque-value'}}, fmt)
    to = fmt if same else 'claude' if fmt == 'codex' else 'codex'
    data = paired(file, RULES, fmt=fmt, to=to, code=1)
    v2 = invoke('report', '--from', fmt, '--to', to, file, '--policy-json-v2',
                *[a for k in KINDS for a in ('--fail-on', k)])
    assert v2.returncode == 1 and v2.stderr == ''
    old = json.loads(v2.stdout)
    assert data['findings'] == old['findings']
    assert data['policy']['decision'] == old['policy']['decision']
    if same:
        assert {f['kind'] for f in data['findings']} == {'secret'}


@pytest.mark.parametrize('rules', [(), ('loss:server', 'secret:document'), ('interpolation:document',)])
@pytest.mark.parametrize('empty', [True, False])
def test_no_findings_and_empty_rules(tmp_path, rules, empty):
    file = source(tmp_path, {} if empty else {'s': {'command': 'native-value'}})
    assert paired(file, rules)['findings'] == []


@pytest.mark.parametrize('rules', [(), ('loss:server',), ('interpolation:server',)])
def test_unsupported_precedes_evaluated_acceptance_or_rejection(tmp_path, rules):
    file = source(tmp_path, {'bad': {'type': 'sse', 'url': 'https://example.test'},
                             's': {'command': 'native-value', 'args': ['${LITERAL}']}})
    data = paired(file, rules, code=3)
    assert data['error'] == 'unsupported server conversion; no files written'
    assert {f['kind'] for f in data['findings']} == {'error', 'interpolation'}


@pytest.mark.parametrize('invalid', ['schema', 'syntax', 'duplicate', 'utf8', 'missing', 'depth', 'size'])
def test_invalid_input_discards_partial_findings_before_unsupported_or_policy(tmp_path, invalid):
    file = source(tmp_path, {'safe': {'command': 'native-value', 'vendor': 1},
                'bad': {'type': 'sse', 'url': 'https://example.test'}, 'invalid': {'command': 42}})
    if invalid == 'syntax': file.write_text('{"broken":"sensitive-value"')
    elif invalid == 'duplicate': file.write_text('{"sensitive-value":1,"sensitive-value":2}')
    elif invalid == 'utf8': file.write_bytes(b'\xffsensitive-value')
    elif invalid == 'missing': file.unlink()
    elif invalid == 'depth': file.write_text('{"meta":' + '[' * 70 + '0' + ']' * 70 + '}')
    elif invalid == 'size': file.write_text(' ' * (1024 * 1024 + 1))
    paired(file, ('secret:document', 'loss:server', 'loss:server'), code=2)


@pytest.mark.parametrize('exception', [ConfigError, OSError, ValueError, TypeError, RecursionError])
def test_processing_failure_has_normalized_rules_without_evaluation(tmp_path, monkeypatch, capsys, exception):
    file = mixed(tmp_path)
    def fail(*a): raise exception('ghp_' + 'b' * 30)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', fail)
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file), '--policy-json-v3',
                 '--fail-rule', 'secret:document', '--fail-rule', 'loss:server']) == 2
    captured = capsys.readouterr(); data = json.loads(captured.out)
    assert captured.err == '' and 'ghp_' not in captured.out
    assert data['schemaVersion'] == 3 and data['status'] == 'error' and data['findings'] == []
    assert data['error'] and data['policy'] == {
        'rules': [{'kind': 'loss', 'scope': 'server'}, {'kind': 'secret', 'scope': 'document'}],
        'matchedRules': None, 'decision': 'not-evaluated'}


@pytest.mark.parametrize('extra', [
    *[['--fail-rule', 'loss:server', *(['--' + mode] if mode else [])]
      for mode in ('', 'json', 'json-v1', 'policy-json-v1', 'policy-json-v2')],
    *[['--policy-json-v3', '--' + mode] for mode in ('json', 'json-v1', 'policy-json-v1', 'policy-json-v2')],
    ['--policy-json-v3', '--fail-on', 'loss'], ['--policy-json-v3', '--fail-scope', 'server'],
    *[['--policy-json-v3', '--fail-rule', v] for v in
      ('', 'loss', 'server', 'loss:', ':server', 'loss:server:document', 'loss,secret:server',
       'LOSS:server', 'loss:SERVER', 'loss: server', ' loss:server', 'loss:server ',
       'loss:*', '*:server', 'error:server', 'secret:(document)', '["loss","server"]')],
    ['--policy-json-v3', '--fail-rule'], ['--policy-json-v3', '-o', 'output'],
])
def test_malformed_or_incompatible_options_fail_before_read(extra, monkeypatch, capsys):
    def forbidden(*a): raise AssertionError('read before valid arguments')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', forbidden)
    with pytest.raises(SystemExit) as exc:
        main(['report', '--from', 'claude', '--to', 'codex', 'missing', *extra])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'usage:' in captured.err


@pytest.mark.parametrize('args', [
    ['report', '--from', 'claude', '--to', 'codex', '--policy-json-v3'],
    ['report', '--from', 'unknown', '--to', 'codex', 'missing', '--policy-json-v3'],
    ['check', '--from', 'claude', '--to', 'codex', '--source', 'missing', '--target', 'missing',
     '--policy-json-v3', '--fail-rule', 'loss:server'],
    ['convert', '--from', 'claude', '--to', 'codex', 'missing', '--fail-rule', 'loss:server'],
])
def test_input_formats_required_and_rule_options_report_only(args):
    result = invoke(*args)
    assert result.returncode == 2 and result.stdout == '' and 'usage:' in result.stderr


def test_no_render_write_expansion_execution_or_network(tmp_path, monkeypatch, capsys):
    file = mixed(tmp_path)
    (tmp_path / 'source.bak').write_text('existing-backup'); before = state(tmp_path)
    def forbidden(*a, **kw): raise AssertionError('unexpected side effect or rendering')
    for name in ('new_document', 'atomic_write', 'validate_write'):
        monkeypatch.setattr('agentwrench.mcpconfig.cli.' + name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setenv('LITERAL', 'must-not-expand')
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file), '--policy-json-v3']) == 0
    captured = capsys.readouterr()
    assert captured.err == '' and 'must-not-expand' not in captured.out
    assert {f['scope'] for f in json.loads(captured.out)['findings']} == set(SCOPES)
    assert state(tmp_path) == before
