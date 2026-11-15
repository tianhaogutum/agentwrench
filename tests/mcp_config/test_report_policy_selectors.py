"""Exact policy selectors use original metadata, never masked/display locations."""
import json

from .test_bridge import invoke
from .test_json_report import source, state


def policy(tmp_path, rules):
    file = tmp_path / 'policy.json'
    file.write_text(json.dumps({'schemaVersion': 2, 'rules': rules}), encoding='utf-8')
    return file


def args(file, artifact, fmt='claude', to='codex'):
    return ['report', '--from', fmt, '--to', to, str(file),
            '--policy-json-v4', '--policy-file', str(artifact)]


def test_exact_server_path_can_accept_excluded_diagnostics_then_reject_match(tmp_path):
    file = source(tmp_path, {'safe': {'command': 'native-value'},
                             'other': {'command': 'native-value',
                                       'env': {'API_TOKEN': 'sensitive-value'}}})
    rule = {'kind': 'secret', 'scope': 'server', 'server': 'safe', 'path': ['env', 'API_TOKEN']}
    artifact = policy(tmp_path, [rule])
    before = state(tmp_path)
    global_pair = invoke('report', '--from', 'claude', '--to', 'codex', file,
                         '--policy-json-v3', '--fail-rule', 'secret:server')
    assert global_pair.returncode == 1
    result = invoke(*args(file, artifact))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data['schemaVersion'] == 4
    assert data['policy'] == {'rules': [rule], 'matchedRules': [], 'decision': 'accepted'}
    assert data['findings'] == json.loads(global_pair.stdout)['findings']
    assert result.stderr == '' and state(tmp_path) == before
    raw = json.loads(file.read_text())
    raw['mcpServers']['safe']['env'] = {'API_TOKEN': 'sensitive-value'}
    file.write_text(json.dumps(raw))
    result = invoke(*args(file, artifact))
    assert result.returncode == 1
    assert json.loads(result.stdout)['policy']['matchedRules'] == [rule]

import os
import socket
import subprocess

import pytest

from agentwrench.mcpconfig.cli import main, read_file
from agentwrench.mcpconfig.core import Finding
from agentwrench.mcpconfig.formats import ConfigError, FORMATS


def run(file, artifact, *, fmt='claude', to='codex', code=0):
    before = state(file.parent)
    result = invoke(*args(file, artifact, fmt, to))
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == '' and state(file.parent) == before
    assert all(v not in result.stdout for v in ('native-value', 'opaque-value', 'sensitive-value',
                                               '${LITERAL}', 'ghp_', 'must-not-expand'))
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'findings', 'policy', 'error'}
    assert data['schemaVersion'] == 4 and data['operation'] == 'report-policy'
    assert data['status'] == {0: 'accepted', 1: 'rejected', 2: 'error', 3: 'unsupported'}[code]
    assert (data['error'] is None) == (code in (0, 1))
    if code != 2:
        baseline = invoke('report', '--from', fmt, '--to', to, file, '--json-v1')
        assert baseline.returncode == (3 if code == 3 else 0)
        assert data['findings'] == json.loads(baseline.stdout)['findings']
    else:
        assert data['findings'] == [] and data['policy']['matchedRules'] is None
        assert data['policy']['decision'] == 'not-evaluated'
    return data


def mixed(tmp_path):
    file = source(tmp_path, {'(document)': {'command': 'native-value',
         'args': ['${LITERAL}'], 'env': {'API_TOKEN': 'sensitive-value'},
         'vendor': {'a.b': 'opaque-value', 'a': {'b': 'opaque-value'},
                    'rows': [{'0': 'opaque-value'}], '': {'工具\n/': 'opaque-value'}}},
         'other': {'command': 'native-value', 'vendor': {'else': 'opaque-value'}}})
    raw = json.loads(file.read_text()); raw['vendor.a.b'] = 'opaque-value'
    file.write_text(json.dumps(raw))
    return file


@pytest.mark.parametrize('selector,match', [
    ({}, True), ({'server': '(document)'}, True), ({'server': 'other'}, True),
    ({'server': 'missing'}, False), ({'server': '(DOCUMENT)'}, False), ({'server': ''}, False),
    ({'path': ['vendor', 'a.b']}, True), ({'path': ['vendor', 'a', 'b']}, True),
    ({'path': ['vendor', 'rows', 0, '0']}, True),
    ({'path': ['vendor', 'rows', '0', '0']}, False),
    ({'path': ['vendor', 'rows[0]', '0']}, False),
    ({'path': ['vendor', 'rows', 1, '0']}, False),
    ({'path': ['vendor']}, False), ({'path': []}, False),
    ({'path': ['vendor', '', '工具\n/']}, True),
    ({'server': 'other', 'path': ['vendor', 'a.b']}, False),
    ({'server': '(document)', 'path': ['vendor', 'else']}, False),
    ({'path': ['vendor', '*']}, False), ({'path': ['vendor.a.b']}, False),
])
def test_literal_selectors_require_all_members_of_one_finding(tmp_path, selector, match):
    file = mixed(tmp_path)
    rule = {'kind': 'loss', 'scope': 'server', **selector}
    data = run(file, policy(tmp_path, [rule]), code=int(match))
    assert data['policy'] == {'rules': [rule], 'matchedRules': [rule] if match else [],
                              'decision': 'rejected' if match else 'accepted'}


def test_document_null_identity_and_real_document_named_server_are_distinct(tmp_path):
    file = mixed(tmp_path)
    rules = [{'kind': 'loss', 'scope': 'document', 'path': ['vendor.a.b']},
             {'kind': 'loss', 'scope': 'server', 'server': '(document)', 'path': ['vendor.a.b']}]
    data = run(file, policy(tmp_path, rules[::-1]), code=1)
    assert data['policy']['rules'] == rules[::-1]
    assert data['policy']['matchedRules'] == [rules[0]]
    assert any(f['server'] == '(document)' for f in data['findings'])
    assert any(f['scope'] == 'document' and f['server'] is None for f in data['findings'])


def test_normalization_order_duplicates_overlaps_and_omitted_empty(tmp_path):
    file = mixed(tmp_path)
    selectors = [{}, {'path': []}, {'path': ['']}, {'path': ['a']}, {'path': ['a', 2]},
                 {'path': ['a', 10]}, {'path': [0]}, {'server': ''}, {'server': 'z'},
                 {'server': '工具', 'path': []}]
    expected = [{'kind': 'loss', 'scope': 'server', **s} for s in selectors]
    expected += [{'kind': 'loss', 'scope': 'document'}, {'kind': 'secret', 'scope': 'server'},
                 {'kind': 'interpolation', 'scope': 'document'}]
    data = run(file, policy(tmp_path, [*reversed(expected), *expected]), code=1)
    assert data['policy']['rules'] == expected
    assert data['policy']['matchedRules'] == [expected[0], expected[-3], expected[-2]]


@pytest.mark.parametrize('scope', ['server', 'document'])
def test_masked_collisions_match_original_metadata_only(tmp_path, monkeypatch, capsys, scope):
    token_a, token_b = 'ghp_' + 'a' * 30, 'ghp_' + 'b' * 30
    file = source(tmp_path, {})
    findings = [Finding(token_a, 'same.display', 'loss', 'synthetic', (token_a, 0), scope)]
    monkeypatch.setattr('agentwrench.mcpconfig.cli.project', lambda *a: ({}, findings))
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', lambda *a: [])
    rules = [{'kind': 'loss', 'scope': scope, 'path': [t, 0],
              **({'server': t} if scope == 'server' else {})}
             for t in (token_a, token_b, '[REDACTED]')]
    artifact = policy(tmp_path, rules)
    assert main(args(file, artifact)) == 1
    captured = capsys.readouterr(); data = json.loads(captured.out)
    assert 'ghp_' not in captured.out and captured.err == ''
    assert len(data['policy']['rules']) == 3  # raw identities stay distinct after output masking
    assert data['policy']['rules'][0] == data['policy']['rules'][1] == data['policy']['rules'][2]
    assert len(data['policy']['matchedRules']) == 1
    # A literal redaction marker is never a wildcard for a token-shaped original.
    artifact = policy(tmp_path, [rules[-1]])
    assert main(args(file, artifact)) == 0
    assert json.loads(capsys.readouterr().out)['policy']['matchedRules'] == []


@pytest.mark.parametrize('actual,selected,expected', [
    ((), [], True), (('x',), [], False), ((), None, True),
    (('a.b',), ['a', 'b'], False), ((0,), ['0'], False),
    (('a', 0), ['a'], False), (('', '\n工具'), ['', '\n工具'], True),
])
def test_empty_root_vs_omitted_path_uses_structured_coordinates(tmp_path, monkeypatch, capsys,
                                                            actual, selected, expected):
    file = source(tmp_path, {})
    finding = Finding('', 'unrelated.display', 'secret', 'synthetic', actual)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.project', lambda *a: ({}, [finding]))
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', lambda *a: [])
    rule = {'kind': 'secret', 'scope': 'server', 'server': ''}
    if selected is not None: rule['path'] = selected
    artifact = policy(tmp_path, [rule])
    assert main(args(file, artifact)) == int(expected)
    data = json.loads(capsys.readouterr().out)
    assert data['policy']['matchedRules'] == ([rule] if expected else [])


PAIR = {'kind': 'loss', 'scope': 'server'}
INVALID_RULES = [None, [], 'loss:server', {}, {'kind': 'loss'}, {'scope': 'server'},
    {**PAIR, 'unknown': 'sensitive-value'}, {**PAIR, 'kind': 'error'}, {**PAIR, 'scope': 'SERVER'},
    *[{**PAIR, 'server': v} for v in (None, 1, True, [], {})],
    *[{**PAIR, 'scope': 'document', 'server': v} for v in (None, '', '(document)')],
    *[{**PAIR, 'path': v} for v in (None, '', 'a.b', 0, True, {}, [None], [True], [False],
       [0.0], [-1], [2147483648], [[]], [{}], ['x'] * 65)],
]
INVALID_ARTIFACTS = ['', '{', 'null', '[]', '{}',
    *[json.dumps({'schemaVersion': v, 'rules': []}) for v in (1, 3, True, 2.0, '2', None)],
    *[json.dumps({'schemaVersion': 2, 'rules': v}) for v in (None, {}, '', 1)],
    json.dumps({'schemaVersion': 2, 'rules': [PAIR] * 129}),
    '{"schemaVersion":2,"rules":[],"rules":[]}',
    '{"schemaVersion":2,"rules":[{"kind":"loss","scope":"server","path":[],"p\\u0061th":[]}]}',
    '{"schemaVersion":2,"rules":[],"extra":null}',
    '{"schemaVersion":2,"rules":[{"kind":"loss","scope":"server","path":[NaN]}]}',
    '{"schemaVersion":2,"rules":[{"kind":"loss","scope":"server","path":[1e999]}]}',
    '//comment\n{"schemaVersion":2,"rules":[]}', '\ufeff{"schemaVersion":2,"rules":[]}',
    '{"schemaVersion":2,"rules":[],}', '{"schemaVersion":2,"rules":[]} null',
    '[' * 1100 + '0' + ']' * 1100,
    *[json.dumps({'schemaVersion': 2, 'rules': [PAIR, r]}) for r in INVALID_RULES]]


@pytest.mark.parametrize('text', INVALID_ARTIFACTS)
def test_invalid_selector_artifact_has_no_partial_rules_and_no_native_read(tmp_path, monkeypatch, capsys, text):
    artifact = tmp_path / 'policy.json'; artifact.write_text(text, encoding='utf-8')
    reads = []
    def read(path, **kw):
        reads.append(path); assert path == artifact
        return read_file(path, **kw)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', read)
    assert main(args(tmp_path / 'must-not-read', artifact)) == 2
    captured = capsys.readouterr(); data = json.loads(captured.out)
    assert reads == [artifact] and captured.err == '' and 'sensitive-value' not in captured.out
    assert data['schemaVersion'] == 4 and data['status'] == 'error' and data['error']
    assert data['findings'] == []
    assert data['policy'] == {'rules': [], 'matchedRules': None, 'decision': 'not-evaluated'}


@pytest.mark.parametrize('invalid', ['missing', 'directory', 'symlink', 'fifo', 'utf8', 'bytes'])
def test_policy_regular_utf8_byte_bounds_before_native(tmp_path, monkeypatch, capsys, invalid):
    artifact = policy(tmp_path, [])
    if invalid == 'missing': artifact.unlink()
    elif invalid == 'directory': artifact.unlink(); artifact.mkdir()
    elif invalid == 'symlink': artifact.unlink(); artifact.symlink_to(tmp_path / 'missing')
    elif invalid == 'fifo': artifact.unlink(); os.mkfifo(artifact)
    elif invalid == 'utf8': artifact.write_bytes(b'\xffsensitive-value')
    else: artifact.write_bytes(b' ' * 65537)
    def read(path, **kw):
        assert path == artifact
        return read_file(path, **kw)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', read)
    assert main(args(tmp_path / 'must-not-read', artifact)) == 2
    assert json.loads(capsys.readouterr().out)['policy']['rules'] == []


def test_exact_limits_accept_empty_and_extreme_literal_segments(tmp_path):
    file = source(tmp_path, {})
    rule = {**PAIR, 'server': '', 'path': ['', 2147483647] * 32}
    artifact = policy(tmp_path, [PAIR] * 127 + [rule])
    raw = artifact.read_bytes(); artifact.write_bytes(raw + b' ' * (65536 - len(raw)))
    data = run(file, artifact)
    assert data['policy'] == {'rules': [PAIR, rule], 'matchedRules': [], 'decision': 'accepted'}


@pytest.mark.parametrize('fmt', FORMATS)
@pytest.mark.parametrize('same', [False, True])
def test_all_native_formats_use_native_path_and_same_format_secrets(tmp_path, fmt, same):
    file = source(tmp_path, {'(document)': {'command': 'native-value',
                    'env': {'API_TOKEN': 'sensitive-value'}, 'vendor': 'opaque-value'}}, fmt)
    rule = {'kind': 'secret', 'scope': 'server', 'server': '(document)', 'path': ['env', 'API_TOKEN']}
    data = run(file, policy(tmp_path, [rule]), fmt=fmt,
               to=fmt if same else 'claude' if fmt == 'codex' else 'codex', code=1)
    assert data['policy']['matchedRules'] == [rule]
    if same: assert {f['kind'] for f in data['findings']} == {'secret'}


@pytest.mark.parametrize('rules', [[], [PAIR], [{'kind': 'interpolation', 'scope': 'server', 'server': 's'}]])
def test_unsupported_precedence_and_invalid_discards_findings(tmp_path, rules):
    file = source(tmp_path, {'bad': {'type': 'sse', 'url': 'https://example.test'},
                             's': {'command': 'native-value', 'args': ['${LITERAL}']}})
    artifact = policy(tmp_path, rules)
    data = run(file, artifact, code=3)
    assert data['policy']['decision'] == ('rejected' if rules and rules != [PAIR] else 'accepted')
    raw = json.loads(file.read_text()); raw['mcpServers']['invalid'] = {'command': 42}
    file.write_text(json.dumps(raw))
    data = run(file, artifact, code=2)
    assert data['policy']['rules'] == rules


@pytest.mark.parametrize('invalid', ['syntax', 'duplicate', 'utf8', 'missing', 'bytes', 'depth'])
def test_valid_selectors_retained_on_native_error(tmp_path, invalid):
    file = mixed(tmp_path); artifact = policy(tmp_path, [{**PAIR, 'path': []}])
    if invalid == 'syntax': file.write_text('{')
    elif invalid == 'duplicate': file.write_text('{"a":1,"a":2}')
    elif invalid == 'utf8': file.write_bytes(b'\xffsensitive-value')
    elif invalid == 'missing': file.unlink()
    elif invalid == 'bytes': file.write_bytes(b' ' * (1024 * 1024 + 1))
    else: file.write_text('{"a":' + '[' * 70 + '0' + ']' * 70 + '}')
    assert run(file, artifact, code=2)['policy']['rules'] == [{**PAIR, 'path': []}]


@pytest.mark.parametrize('exception', [ConfigError, OSError, ValueError, TypeError, RecursionError])
def test_processing_failure_discards_findings_retains_masked_selectors(tmp_path, monkeypatch, capsys, exception):
    file = mixed(tmp_path); artifact = policy(tmp_path, [{**PAIR, 'server': 'ghp_' + 'a' * 30}])
    def fail(*a): raise exception('ghp_' + 'b' * 30)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', fail)
    assert main(args(file, artifact)) == 2
    captured = capsys.readouterr(); data = json.loads(captured.out)
    assert captured.err == '' and 'ghp_' not in captured.out and data['findings'] == []
    assert data['policy'] == {'rules': [{**PAIR, 'server': '[REDACTED]'}],
                              'matchedRules': None, 'decision': 'not-evaluated'}


@pytest.mark.parametrize('extra', [
    ['--policy-json-v4'],
    *[['--policy-json-v4', '--policy-file', 'missing', '--' + flag] for flag in
      ('json', 'json-v1', 'policy-json-v1', 'policy-json-v2', 'policy-json-v3')],
    *[['--policy-json-v4', '--policy-file', 'missing', '--' + flag, value] for flag, value in
      [('fail-rule', 'loss:server'), ('fail-on', 'loss'), ('fail-scope', 'server')]],
    ['--policy-json-v4', '--policy-file'],
])
def test_v4_argument_conflicts_before_all_reads(tmp_path, monkeypatch, capsys, extra):
    def forbidden(*a, **kw): raise AssertionError('unexpected read')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', forbidden)
    with pytest.raises(SystemExit) as exc:
        main(['report', '--from', 'claude', '--to', 'codex', 'missing', *extra])
    captured = capsys.readouterr()
    assert exc.value.code == 2 and captured.out == '' and 'usage:' in captured.err


def test_v2_artifact_refused_by_old_mode_and_v1_refused_by_v4(tmp_path):
    file = source(tmp_path, {}); artifact = policy(tmp_path, [])
    old = invoke('report', '--from', 'claude', '--to', 'codex', file,
                 '--policy-json-v3', '--policy-file', artifact)
    assert old.returncode == 2 and json.loads(old.stdout)['policy']['rules'] == []
    artifact.write_text('{"schemaVersion":1,"rules":[]}')
    assert run(file, artifact, code=2)['policy']['rules'] == []


def test_no_native_writes_discovery_render_expansion_execution_or_network(tmp_path, monkeypatch, capsys):
    file = mixed(tmp_path); artifact = policy(tmp_path, [])
    (tmp_path / 'source.bak').write_text('existing-backup')
    before = state(tmp_path); reads = []
    def read(path, **kw):
        reads.append(path); return read_file(path, **kw)
    def forbidden(*a, **kw): raise AssertionError('unexpected side effect or rendering')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', read)
    for name in ('new_document', 'atomic_write', 'validate_write'):
        monkeypatch.setattr('agentwrench.mcpconfig.cli.' + name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setenv('LITERAL', 'must-not-expand')
    assert main(args(file, artifact)) == 0
    captured = capsys.readouterr()
    assert captured.err == '' and 'must-not-expand' not in captured.out and state(tmp_path) == before
    assert reads == [artifact, file]
    assert json.loads(captured.out)['findings']


@pytest.mark.parametrize('kind,path,match', [
    ('secret', ['metadata', 0, 'API_TOKEN'], True),
    ('loss', ['metadata'], True), ('loss', ['metadata', 0, 'API_TOKEN'], False),
])
def test_native_document_nested_secret_and_boundary_loss(tmp_path, kind, path, match):
    file = source(tmp_path, {})
    raw = json.loads(file.read_text()); raw['metadata'] = [{'API_TOKEN': 'sensitive-value'}]
    file.write_text(json.dumps(raw))
    rule = {'kind': kind, 'scope': 'document', 'path': path}
    assert run(file, policy(tmp_path, [rule]), code=int(match))['policy']['matchedRules'] == ([rule] if match else [])
