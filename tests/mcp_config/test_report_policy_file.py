"""Reusable paired-rule artifacts share the inline v3 result contract."""
import json

from .test_bridge import invoke
from .test_json_report import source, state


def policy(tmp_path, rules):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'schemaVersion': 1, 'rules': [
        dict(zip(('kind', 'scope'), rule.split(':'))) for rule in rules]}), encoding='utf-8')
    return path


def test_reusable_policy_equals_inline_and_keeps_excluded_findings(tmp_path):
    file = source(tmp_path, {'(document)': {'command': 'native-value',
                                          'env': {'API_TOKEN': 'sensitive-value'}}})
    raw = json.loads(file.read_text()); raw['metadata'] = 'opaque-value'
    file.write_text(json.dumps(raw))
    artifact = policy(tmp_path, ('secret:document', 'loss:server', 'loss:server'))
    before = state(tmp_path)
    result = invoke('report', '--from', 'claude', '--to', 'codex', file,
                    '--policy-json-v3', '--policy-file', artifact)
    assert result.returncode == 0, result.stdout + result.stderr
    inline = invoke('report', '--from', 'claude', '--to', 'codex', file,
                    '--policy-json-v3', '--fail-rule', 'loss:server', '--fail-rule', 'secret:document')
    assert (result.stdout, result.stderr) == (inline.stdout, inline.stderr)
    data = json.loads(result.stdout)
    assert data['policy']['matchedRules'] == []
    assert {(f['kind'], f['scope']) for f in data['findings']} == {
        ('secret', 'server'), ('loss', 'document')}
    assert any(f['scope'] == 'server' and f['server'] == '(document)' for f in data['findings'])
    assert state(tmp_path) == before

import itertools
import os
import socket
import subprocess

import pytest

from agentwrench.mcpconfig.cli import main, read_file
from agentwrench.mcpconfig.formats import ConfigError, FORMATS
from agentwrench.mcpconfig.policy import POLICY_MAX_BYTES, POLICY_RULES
from .test_report_policy_rules import mixed


SELECTIONS = [r for n in range(7) for r in itertools.combinations(POLICY_RULES, n)]


@pytest.mark.parametrize('rules', SELECTIONS)
def test_every_file_selection_matches_inline_bytes(tmp_path, rules):
    file = mixed(tmp_path)
    artifact = policy(tmp_path, (*reversed(rules), *rules))
    before = state(tmp_path)
    args = ('report', '--from', 'claude', '--to', 'codex', file, '--policy-json-v3')
    actual = invoke(*args, '--policy-file', artifact)
    inline = invoke(*args, *[a for r in rules for a in ('--fail-rule', r)])
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        inline.returncode, inline.stdout, inline.stderr)
    assert actual.returncode in (0, 1) and actual.stderr == ''
    assert state(tmp_path) == before
    assert all(v not in actual.stdout for v in ('native-value', 'opaque-value', 'sensitive-value',
                                               '${LITERAL}', 'ghp_', 'must-not-expand'))


VALID = {'schemaVersion': 1, 'rules': [{'kind': 'loss', 'scope': 'server'}]}
INVALID_ARTIFACTS = [
    '', '{', 'null', '[]', 'true', '1', '"sensitive-value"', '{}',
    json.dumps({'rules': []}), json.dumps({'schemaVersion': 1}),
    *[json.dumps({'schemaVersion': v, 'rules': []}) for v in (0, 2, 3, True, 1.0, '1', None)],
    json.dumps({**VALID, 'sensitive-value': 'sensitive-value'}),
    *[json.dumps({'schemaVersion': 1, 'rules': v}) for v in ({}, None, '', 1, True)],
    *[json.dumps({'schemaVersion': 1, 'rules': [VALID['rules'][0], v]}) for v in (
        None, [], 'loss:server', 1, {}, {'kind': 'loss'}, {'scope': 'server'},
        {'kind': 'loss', 'scope': 'server', 'extra': 'sensitive-value'},
        *[{'kind': k, 'scope': s} for k, s in (
            ('error', 'server'), ('LOSS', 'server'), ('loss', 'SERVER'), ('loss', '(document)'),
            ('loss', '*'), (' loss', 'server'), ('loss', 'server '), ('', ''),
            (None, 'server'), (['loss'], 'server'), ('loss', {}), ('loss', 1))])],
    '{"schemaVersion":1,"schemaVersion":1,"rules":[]}',
    '{"schemaVersion":1,"rules":[{"kind":"secret","kind":"loss","scope":"server"}]}',
    '{"schemaVersion":1,"rules":[{"kind":"loss","scope":"server","scope":"document"}]}',
    '{"schemaVersion":1,"rules":[],"rul\\u0065s":[]}',
    '{"schemaVersion":1,"rules":[],}', '// comment\n' + json.dumps(VALID),
    '\ufeff' + json.dumps(VALID), '\v' + json.dumps(VALID),
    *[json.dumps(VALID)[:-1] + ',"extra":' + v + '}' for v in ('NaN', 'Infinity', '-Infinity')],
    '{"schemaVersion":1,"rules":[' + '[' * 1100 + '0' + ']' * 1100 + ']}',
    '{"schemaVersion":' + '1' * 5000 + ',"rules":[]}',
    json.dumps({'schemaVersion': 1, 'rules': VALID['rules'] * 129}),
]


def assert_error(captured, rules=()):
    assert captured.err == ''
    assert 'sensitive-value' not in captured.out and 'ghp_' not in captured.out
    data = json.loads(captured.out)
    assert data['schemaVersion'] == 3 and data['status'] == 'error' and data['findings'] == []
    assert data['error'] and data['policy'] == {
        'rules': [dict(zip(('kind', 'scope'), r.split(':'))) for r in POLICY_RULES if r in rules],
        'matchedRules': None, 'decision': 'not-evaluated'}


@pytest.mark.parametrize('text', INVALID_ARTIFACTS)
def test_entire_invalid_artifact_refused_before_native_read(tmp_path, text, monkeypatch, capsys):
    artifact = tmp_path / 'policy.json'; artifact.write_text(text)
    reads = []
    def read(path, **kwargs):
        reads.append(path)
        assert path == artifact, 'native input read before policy validated'
        return read_file(path, **kwargs)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', read)
    assert main(['report', '--from', 'claude', '--to', 'codex', 'must-not-read',
                 '--policy-json-v3', '--policy-file', str(artifact)]) == 2
    assert reads == [artifact]
    assert_error(capsys.readouterr())


@pytest.mark.parametrize('invalid', ['missing', 'directory', 'symlink', 'fifo', 'utf8', 'oversize'])
def test_artifact_file_read_boundaries_before_native(tmp_path, invalid, monkeypatch, capsys):
    artifact = policy(tmp_path, ('loss:server',))
    if invalid == 'missing': artifact.unlink()
    elif invalid == 'directory': artifact.unlink(); artifact.mkdir()
    elif invalid == 'symlink':
        target = tmp_path / 'real.json'; artifact.rename(target); artifact.symlink_to(target)
    elif invalid == 'fifo': artifact.unlink(); os.mkfifo(artifact)
    elif invalid == 'utf8': artifact.write_bytes(b'\xffsensitive-value')
    elif invalid == 'oversize': artifact.write_bytes(b' ' * (POLICY_MAX_BYTES + 1))
    def read(path, **kwargs):
        assert path == artifact
        return read_file(path, **kwargs)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', read)
    assert main(['report', '--from', 'claude', '--to', 'codex', 'must-not-read',
                 '--policy-json-v3', '--policy-file', str(artifact)]) == 2
    assert_error(capsys.readouterr())


def test_exact_byte_and_entry_limits_and_relative_filename(tmp_path, monkeypatch, capsys):
    file = source(tmp_path, {})
    artifact = policy(tmp_path, ('loss:server',) * 128)
    raw = artifact.read_bytes(); artifact.write_bytes(raw + b' ' * (POLICY_MAX_BYTES - len(raw)))
    monkeypatch.chdir(tmp_path)
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v3', '--policy-file', artifact.name]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['policy'] == {'rules': VALID['rules'], 'matchedRules': [], 'decision': 'accepted'}


@pytest.mark.parametrize('fmt', FORMATS)
@pytest.mark.parametrize('same', [False, True])
def test_all_native_formats_and_same_format(tmp_path, fmt, same):
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'args': ['${LITERAL}'],
                    'env': {'API_TOKEN': 'sensitive-value'}, 'vendor': 'opaque-value'}}, fmt)
    artifact = policy(tmp_path, POLICY_RULES)
    to = fmt if same else 'claude' if fmt == 'codex' else 'codex'
    args = ('report', '--from', fmt, '--to', to, file, '--policy-json-v3')
    result = invoke(*args, '--policy-file', artifact)
    inline = invoke(*args, *[a for r in POLICY_RULES for a in ('--fail-rule', r)])
    assert (result.returncode, result.stdout, result.stderr) == (1, inline.stdout, '')


@pytest.mark.parametrize('rules', [(), ('loss:server',), ('interpolation:server',)])
def test_unsupported_precedes_evaluated_policy_and_invalid_precedes_both(tmp_path, rules):
    file = source(tmp_path, {'bad': {'type': 'sse', 'url': 'https://example.test'},
                             's': {'command': 'native-value', 'args': ['${LITERAL}']}})
    artifact = policy(tmp_path, rules)
    args = ('report', '--from', 'claude', '--to', 'codex', file, '--policy-json-v3')
    result = invoke(*args, '--policy-file', artifact)
    inline = invoke(*args, *[a for r in rules for a in ('--fail-rule', r)])
    assert (result.returncode, result.stdout, result.stderr) == (3, inline.stdout, '')
    raw = json.loads(file.read_text()); raw['mcpServers']['invalid'] = {'command': 42}
    file.write_text(json.dumps(raw))
    result = invoke(*args, '--policy-file', artifact)
    assert result.returncode == 2
    from types import SimpleNamespace
    assert_error(SimpleNamespace(out=result.stdout, err=result.stderr), rules)


@pytest.mark.parametrize('invalid', ['syntax', 'duplicate', 'utf8', 'missing', 'size', 'depth'])
def test_valid_policy_retained_on_native_input_error(tmp_path, invalid, capsys):
    file = source(tmp_path, {})
    artifact = policy(tmp_path, ('secret:document', 'loss:server'))
    if invalid == 'syntax': file.write_text('{"sensitive-value":')
    elif invalid == 'duplicate': file.write_text('{"a":1,"a":2}')
    elif invalid == 'utf8': file.write_bytes(b'\xffsensitive-value')
    elif invalid == 'missing': file.unlink()
    elif invalid == 'size': file.write_text(' ' * (1024 * 1024 + 1))
    elif invalid == 'depth': file.write_text('{"m":' + '[' * 70 + '0' + ']' * 70 + '}')
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v3', '--policy-file', str(artifact)]) == 2
    assert_error(capsys.readouterr(), ('loss:server', 'secret:document'))


@pytest.mark.parametrize('extra', [
    [], ['--json'], ['--json-v1'], ['--policy-json-v1'], ['--policy-json-v2'],
    ['--policy-json-v3', '--fail-rule', 'loss:server'],
    ['--policy-json-v3', '--fail-on', 'loss'], ['--policy-json-v3', '--fail-scope', 'server'],
])
def test_incompatible_selectors_refused_before_any_read(extra, monkeypatch, capsys):
    def forbidden(*a, **kw): raise AssertionError('read before valid arguments')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', forbidden)
    with pytest.raises(SystemExit) as exc:
        main(['report', '--from', 'claude', '--to', 'codex', 'missing',
              '--policy-file', 'missing', *extra])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'usage:' in captured.err


@pytest.mark.parametrize('args', [
    ['report', '--from', 'claude', '--to', 'codex', '--policy-json-v3', '--policy-file', 'missing'],
    ['report', '--from', 'unknown', '--to', 'codex', 'missing', '--policy-json-v3', '--policy-file', 'missing'],
    ['report', '--from', 'claude', '--to', 'codex', 'missing', '--policy-json-v3', '--policy-file'],
    ['convert', '--from', 'claude', '--to', 'codex', 'missing', '--policy-file', 'missing'],
    ['check', '--from', 'claude', '--to', 'codex', '--source', 'missing', '--target', 'missing',
     '--policy-file', 'missing'],
])
def test_required_input_formats_file_operand_and_report_only(args):
    result = invoke(*args)
    assert result.returncode == 2 and result.stdout == '' and 'usage:' in result.stderr


@pytest.mark.parametrize('exception', [ConfigError, OSError, ValueError, TypeError, RecursionError])
def test_processing_failure_retains_valid_policy_only(tmp_path, monkeypatch, capsys, exception):
    file = mixed(tmp_path); artifact = policy(tmp_path, ('loss:server', 'secret:document'))
    def fail(*a): raise exception('ghp_' + 'b' * 30)
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', fail)
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v3', '--policy-file', str(artifact)]) == 2
    assert_error(capsys.readouterr(), ('loss:server', 'secret:document'))


def test_no_discovery_render_write_expansion_execution_or_network(tmp_path, monkeypatch, capsys):
    file = mixed(tmp_path); artifact = policy(tmp_path, ())
    (tmp_path / 'source.bak').write_text('existing-backup')
    (tmp_path / '.mcpconf-policy.json').write_text('invalid discovered policy')
    before = state(tmp_path); reads = []
    def read(path, **kwargs):
        reads.append(path)
        return read_file(path, **kwargs)
    def forbidden(*a, **kw): raise AssertionError('unexpected side effect or rendering')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.read_file', read)
    for name in ('new_document', 'atomic_write', 'validate_write'):
        monkeypatch.setattr('agentwrench.mcpconfig.cli.' + name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setenv('LITERAL', 'must-not-expand')
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file),
                 '--policy-json-v3', '--policy-file', str(artifact)]) == 0
    captured = capsys.readouterr()
    assert reads == [artifact, file] and captured.err == '' and 'must-not-expand' not in captured.out
    assert json.loads(captured.out)['policy'] == {'rules': [], 'matchedRules': [], 'decision': 'accepted'}
    assert state(tmp_path) == before
