"""Opt-in version-1 reports preserve native scope without parsing labels."""
import json
import os
import socket
import subprocess

import pytest

from agentwrench.mcpconfig.cli import main
from agentwrench.mcpconfig.formats import ConfigError, FORMATS, new_document
from .test_bridge import invoke


def state(root):
    return {p.name: (p.lstat().st_mode, p.lstat().st_mtime_ns,
                     p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in root.iterdir()}


def source(tmp_path, servers, fmt='claude'):
    file = tmp_path / 'source'
    file.write_text(new_document(fmt, servers), encoding='utf-8')
    return file


def report(file, fmt='claude', to='codex', code=0):
    before = state(file.parent)
    result = invoke('report', '--from', fmt, '--to', to, file, '--json-v1')
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == ''
    assert state(file.parent) == before
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'findings', 'error'}
    assert data['schemaVersion'] == 1 and data['operation'] == 'report'
    assert data['status'] == {0: 'reported', 2: 'error', 3: 'unsupported'}[code]
    assert (data['error'] is None) == (code == 0)
    if code == 2:
        assert data['findings'] == []
    for f in data['findings']:
        assert set(f) == {'scope', 'server', 'path', 'field', 'kind', 'message'}
        assert f['scope'] in ('server', 'document')
        assert (f['server'] is None) == (f['scope'] == 'document')
        assert f['path'] and all(isinstance(p, str) or type(p) is int and p >= 0 for p in f['path'])
        assert all(isinstance(f[k], str) for k in ('field', 'kind', 'message'))
    return data


def test_legacy_ambiguity_requires_opt_in_native_coordinates(tmp_path):
    file = source(tmp_path, {'(document)': {'command': 'native-value', 'vendor': {
        'a.b': 'literal-value', 'a': {'b': 'nested-value'}, '': {'工具/': []},
        'rows': [{'0': False, '': None}], 'rows[0]': {'0': 42}}}})
    raw = json.loads(file.read_text()); raw.update({'vendor.a.b': None, '': {}})
    file.write_text(json.dumps(raw))
    legacy = invoke('report', '--from', 'claude', '--to', 'codex', file, '--json')
    assert legacy.returncode == 0 and legacy.stderr == ''
    old = json.loads(legacy.stdout)['findings']
    assert all(set(f) == {'server', 'field', 'kind', 'message'} for f in old)
    assert all(f['server'] == '(document)' for f in old)
    assert old[0] == old[1]  # two native locations, identical legacy object
    assert old[0]['field'] == old[-2]['field']  # document shares the display coordinate
    data = report(file)
    assert [(f['scope'], f['server'], f['path']) for f in data['findings']] == [
        ('server', '(document)', ['vendor', 'a.b']),
        ('server', '(document)', ['vendor', 'a', 'b']),
        ('server', '(document)', ['vendor', '', '工具/']),
        ('server', '(document)', ['vendor', 'rows', 0, '0']),
        ('server', '(document)', ['vendor', 'rows', 0, '']),
        ('server', '(document)', ['vendor', 'rows[0]', '0']),
        ('document', None, ['vendor.a.b']), ('document', None, [''])]
    assert all(v not in json.dumps(data) for v in ('native-value', 'literal-value', 'nested-value'))


@pytest.mark.parametrize('fmt', FORMATS)
def test_all_native_sources_cross_and_same_format(tmp_path, fmt):
    file = source(tmp_path, {'worker.工具': {'command': 'native-value', 'args': ['${LITERAL}'],
                                         'vendor.dot': {'': 'opaque-value'}}}, fmt)
    data = report(file, fmt, 'claude' if fmt == 'codex' else 'codex')
    assert [(f['server'], f['path'], f['kind']) for f in data['findings'] if f['scope'] == 'server'] == [
        ('worker.工具', ['args', 0], 'interpolation'), ('worker.工具', ['vendor.dot', ''], 'loss')]
    assert report(file, fmt, fmt)['findings'] == []


@pytest.mark.parametrize('fmt', FORMATS)
def test_empty_collection_is_supported(tmp_path, fmt):
    file = source(tmp_path, {}, fmt)
    assert report(file, fmt, fmt)['findings'] == []


def test_native_mapped_header_and_unmapped_field_spellings(tmp_path):
    file = source(tmp_path, {'s': {'type': 'streamable-http', 'url': 'https://example.test/mcp',
                                 'requestOptions': {'headers': {'X.Key': '${LITERAL}'}}}}, 'continue')
    data = report(file, 'continue')
    assert [(f['path'], f['kind']) for f in data['findings'] if f['scope'] == 'server'] == [
        (['requestOptions', 'headers', 'X.Key'], 'interpolation')]
    file = source(tmp_path, {'s': {'command': 'native-value', 'enabled_tools': ['tool-value'],
                                 'tool_timeout_sec': 10}}, 'codex')
    assert [f['path'] for f in report(file, 'codex', 'claude')['findings']] == [
        ['tool_timeout_sec'], ['enabled_tools']]


@pytest.mark.parametrize('fmt,raw,to,path', [
    ('claude', {'type': 'sse', 'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('cline', {'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('continue', {'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('codex', {'command': 'native-value', 'enabled': False}, 'claude', ['enabled']),
    ('cline', {'command': 'native-value', 'disabled': True}, 'claude', ['disabled']),
    ('claude', {'url': 'ws://example.test/socket'}, 'codex', ['url']),
    ('vscode', {'command': 'native-value', 'env': {'X': None}}, 'claude', ['env']),
])
@pytest.mark.parametrize('mixed', [False, True])
def test_unsupported_keeps_source_findings_without_config(tmp_path, fmt, raw, to, path, mixed):
    servers = {'(document)': raw}
    if mixed:
        servers['safe'] = {'command': 'native-value', 'vendor': {'a.b': 'opaque-value'}}
    data = report(source(tmp_path, servers, fmt), fmt, to, 3)
    assert data['error'] == 'unsupported server conversion; no files written'
    assert [(f['scope'], f['server'], f['path']) for f in data['findings'] if f['kind'] == 'error'] == [
        ('server', '(document)', path)]
    if mixed:
        assert any(f['server'] == 'safe' and f['path'] == ['vendor', 'a.b'] for f in data['findings'])
    assert all(v not in json.dumps(data) for v in ('native-value', 'opaque-value', 'example.test'))


def test_masks_names_paths_labels_document_secrets_controls_and_indices(tmp_path):
    token = 'ghp_' + 'a' * 30
    file = source(tmp_path, {token: {'command': 'native-value', 'vendor': {
        token: [{'control\n.key': '${LITERAL}', 'API_TOKEN': 'sensitive-value'}]}}})
    raw = json.loads(file.read_text()); raw['auth.dot'] = [{'API_TOKEN.' + token: 'document-value'}]
    file.write_text(json.dumps(raw))
    data = report(file)
    assert all(v not in json.dumps(data) for v in (token, 'sensitive-value', 'document-value', '${LITERAL}'))
    assert any(f['server'] == '[REDACTED]' and
               f['path'] == ['vendor', '[REDACTED]', 0, 'control\n.key'] for f in data['findings'])
    assert any(f['scope'] == 'document' and f['server'] is None and
               f['path'] == ['auth.dot', 0, 'API_TOKEN.[REDACTED]'] and f['kind'] == 'secret'
               for f in data['findings'])
    same = report(file, to='claude')
    assert same['findings'] and all(f['kind'] == 'secret' for f in same['findings'])


@pytest.mark.parametrize('key,value', [('', []), ('array[0]', [False, None]), ('a.b', {}), ('工具/', None)])
def test_passthrough_field_boundaries_are_literal(tmp_path, key, value):
    file = source(tmp_path, {'s': {'command': 'native-value', key: value}})
    assert [(f['path'], f['kind']) for f in report(file)['findings']] == [([key], 'loss')]


@pytest.mark.parametrize('fmt', FORMATS)
@pytest.mark.parametrize('invalid', ['syntax', 'schema', 'duplicate'])
def test_invalid_native_inputs_discard_findings(tmp_path, fmt, invalid):
    file = source(tmp_path, {'good': {'command': 'native-value', 'vendor': {'a.b': 1}},
                             'unsupported': {'url': 'https://example.test', 'type': 'unknown'},
                             'bad': {'command': 42}}, fmt)
    if invalid == 'syntax':
        file.write_text('mcp_servers = "sensitive-value' if fmt == 'codex' else
                        'mcpServers: ["sensitive-value' if fmt == 'continue' else '{"sensitive-value":')
    elif invalid == 'duplicate':
        file.write_text('model="sensitive-value"\nmodel="duplicate"\n' if fmt == 'codex' else
                        'name: sensitive-value\nname: duplicate\n' if fmt == 'continue' else
                        '{"sensitive-value":1,"sensitive-value":2}')
    data = report(file, fmt, 'claude' if fmt == 'codex' else 'codex', 2)
    assert all(v not in json.dumps(data) for v in ('sensitive-value', 'native-value', 'example.test'))


@pytest.mark.parametrize('invalid', ['missing', 'directory', 'symlink', 'fifo', 'utf8', 'size', 'depth', 'root'])
def test_read_and_native_bounds_have_empty_error_envelopes(tmp_path, invalid):
    file = tmp_path / 'source'
    if invalid == 'directory':
        file.mkdir()
    elif invalid == 'symlink':
        target = source(tmp_path, {})
        file = tmp_path / 'link'; file.symlink_to(target)
    elif invalid == 'fifo':
        os.mkfifo(file)
    elif invalid == 'utf8':
        file.write_bytes(b'\xffsensitive-value')
    elif invalid == 'size':
        file.write_bytes(b' ' * (1024 * 1024 + 1))
    elif invalid == 'depth':
        file.write_text('{"metadata":' + '[' * 70 + '0' + ']' * 70 + '}')
    elif invalid == 'root':
        file.write_text('["sensitive-value"]')
    data = report(file, code=2)
    assert 'sensitive-value' not in json.dumps(data) and str(file) not in json.dumps(data)


@pytest.mark.parametrize('exception', [ConfigError, ValueError, TypeError, OSError, RecursionError])
def test_runtime_errors_are_masked_without_partial_findings(tmp_path, monkeypatch, capsys, exception):
    file = source(tmp_path, {'s': {'command': 'native-value', 'vendor': {'a.b': 1}}})
    token = 'ghp_' + 'b' * 30
    def fail(*args):
        raise exception(token)
    # Fail after projection has already collected source findings.
    monkeypatch.setattr('agentwrench.mcpconfig.cli.document_fidelity', fail)
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file), '--json-v1']) == 2
    captured = capsys.readouterr()
    assert captured.err == '' and token not in captured.out
    data = json.loads(captured.out)
    assert data['status'] == 'error' and data['findings'] == [] and data['error']


def test_report_does_not_render_write_expand_execute_or_connect(tmp_path, monkeypatch, capsys):
    file = source(tmp_path, {'s': {'command': 'arbitrary-not-an-executable', 'args': ['${LITERAL}']}})
    (tmp_path / 'source.bak').write_text('existing-backup')
    before = state(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('report attempted a side effect or rendered a destination')
    for name in ('new_document', 'atomic_write', 'validate_write'):
        monkeypatch.setattr('agentwrench.mcpconfig.cli.' + name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setenv('LITERAL', 'must-not-expand')
    assert main(['report', '--from', 'claude', '--to', 'codex', str(file), '--json-v1']) == 0
    captured = capsys.readouterr()
    assert captured.err == '' and 'must-not-expand' not in captured.out
    assert json.loads(captured.out)['findings'][0]['path'] == ['args', 0]
    assert state(tmp_path) == before


@pytest.mark.parametrize('args', [
    ['report', '--from', 'claude', '--to', 'codex', 'missing', '--json', '--json-v1'],
    ['report', '--from', 'claude', '--to', 'codex', '--json-v1'],
    ['report', '--from', 'unknown', '--to', 'codex', 'missing', '--json-v1'],
    ['report', '--from', 'claude', '--to', 'codex', 'missing', '--json-v1', '-o', 'output'],
    ['check', '--from', 'claude', '--to', 'codex', '--source', 'missing', '--target', 'missing', '--json-v1'],
    ['convert', '--from', 'claude', '--to', 'codex', 'missing', '--json-v1'],
])
def test_parser_failures_stay_text_without_reading_files(args):
    result = invoke(*args)
    assert result.returncode == 2 and result.stdout == ''
    assert 'usage:' in result.stderr
