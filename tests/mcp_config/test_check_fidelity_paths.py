"""Schema-2 fidelity coordinates must originate in native structure, not labels."""
import json

import pytest

from agentwrench.mcpconfig.formats import FORMATS
from .test_bridge import invoke
from .test_json_check import inputs, tree


def check(args, code=1):
    result = invoke(*args, '--json')
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == ''
    data = json.loads(result.stdout)
    assert data['schemaVersion'] == 2
    for f in data['findings']:
        assert set(f) == {'scope', 'server', 'path', 'field', 'kind', 'message'}
        assert f['scope'] in ('server', 'document')
        assert (f['server'] is None) == (f['scope'] == 'document')
        assert f['path'] and all(isinstance(p, str) or type(p) is int and p >= 0 for p in f['path'])
        assert all(isinstance(f[k], str) for k in ('field', 'kind', 'message'))
    return data


def test_literal_and_nested_fidelity_labels_are_not_coordinates(tmp_path):
    args = inputs(tmp_path, {'s': {'command': 'value', 'vendor': {
        'a.b': 'literal-value', 'a': {'b': 'nested-value'}, '': {'工具/': []},
        'rows': [{'0': False, '': None}], 'rows[0]': {'0': 42}}}},
        {'s': {'command': 'value'}}, 'claude', 'codex')
    before = tree(tmp_path)
    data = check(args)
    assert data['drift'] == []
    assert [(f['path'], f['field'], f['kind']) for f in data['findings']] == [
        (['vendor', 'a.b'], 'vendor.a.b', 'loss'),
        (['vendor', 'a', 'b'], 'vendor.a.b', 'loss'),
        (['vendor', '', '工具/'], 'vendor..工具/', 'loss'),
        (['vendor', 'rows', 0, '0'], 'vendor.rows[0].0', 'loss'),
        (['vendor', 'rows', 0, ''], 'vendor.rows[0].', 'loss'),
        (['vendor', 'rows[0]', '0'], 'vendor.rows[0].0', 'loss')]
    assert all(f['scope'] == 'server' and f['server'] == 's' for f in data['findings'])
    assert all(v not in json.dumps(data) for v in ('literal-value', 'nested-value'))
    assert tree(tmp_path) == before


def test_document_scope_and_real_document_named_server(tmp_path):
    args = inputs(tmp_path, {'(document)': {'command': 'value', 'a.b': {'': 1}}},
                  {'(document)': {'command': 'value'}}, 'claude', 'codex')
    source = tmp_path / 'source'
    data = json.loads(source.read_text())
    data.update({'a.b': {'工具': 1}, '': {}, 'auth': [{'API_TOKEN': 'synthetic-secret'}]})
    source.write_text(json.dumps(data))
    before = tree(tmp_path)
    result = check(args)
    assert [(f['scope'], f['server'], f['path'], f['kind']) for f in result['findings']] == [
        ('server', '(document)', ['a.b', ''], 'loss'),
        ('document', None, ['a.b'], 'loss'),
        ('document', None, [''], 'loss'),
        ('document', None, ['auth'], 'loss'),
        ('document', None, ['auth', 0, 'API_TOKEN'], 'secret')]
    legacy = invoke('report', '--from', 'claude', '--to', 'codex', source, '--json')
    assert legacy.returncode == 0
    records = json.loads(legacy.stdout)['findings']
    assert all(set(f) == {'server', 'field', 'kind', 'message'} for f in records)
    assert all(f['server'] == '(document)' for f in records)
    assert [f['field'] for f in records] == [f['field'] for f in result['findings']]
    human = invoke(*args)
    assert human.returncode == 1 and '(unrepresentable)' in human.stdout
    assert 'synthetic-secret' not in legacy.stdout + human.stdout + human.stderr + json.dumps(result)
    assert tree(tmp_path) == before


@pytest.mark.parametrize('fmt', FORMATS)
def test_all_native_sources_use_literal_coordinates(tmp_path, fmt):
    target = 'claude' if fmt == 'codex' else 'codex'
    args = inputs(tmp_path, {'worker.工具': {'command': 'value', 'vendor.dot': {'': 'value'},
                                         'args': ['${LITERAL}']}},
                  {'worker.工具': {'command': 'value', 'args': ['${LITERAL}']}}, fmt, target)
    result = check(args)
    assert [(f['path'], f['kind']) for f in result['findings'] if f['scope'] == 'server'] == [
        (['args', 0], 'interpolation'), (['vendor.dot', ''], 'loss')]
    assert all(f['server'] == 'worker.工具' and f['scope'] == 'server' for f in result['findings'] if f['scope'] == 'server')


@pytest.mark.parametrize('fmt,raw,target,path', [
    ('cline', {'command': 'value', 'disabled': True}, 'claude', ['disabled']),
    ('codex', {'command': 'value', 'enabled': False}, 'claude', ['enabled']),
    ('claude', {'type': 'sse', 'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('cline', {'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('claude', {'url': 'ws://example.test/socket'}, 'codex', ['url']),
    ('vscode', {'command': 'value', 'env': {'X': None}}, 'claude', ['env']),
])
def test_omitted_servers_have_native_error_coordinates(tmp_path, fmt, raw, target, path):
    result = check(inputs(tmp_path, {'s': raw}, {}, fmt, target))
    assert result['drift'] == []
    assert len(result['findings']) == 1
    f = result['findings'][0]
    assert (f['scope'], f['server'], f['path'], f['kind']) == ('server', 's', path, 'error')


def test_mapped_fields_report_source_spelling(tmp_path):
    result = check(inputs(tmp_path, {'s': {'command': 'value', 'enabled_tools': ['a'],
                                         'tool_timeout_sec': 10}},
                          {'s': {'command': 'value'}}, 'codex', 'claude'))
    assert [f['path'] for f in result['findings']] == [['tool_timeout_sec'], ['enabled_tools']]
    args = inputs(tmp_path, {'s': {'type': 'streamable-http', 'url': 'https://example.test/mcp',
                                 'requestOptions': {'headers': {'X.Key': '${LITERAL}'}}}},
                  {'s': {'url': 'https://example.test/mcp', 'headers': {'X.Key': '${LITERAL}'}}},
                  'continue', 'claude')
    data = check(args, 0)
    assert [(f['path'], f['kind']) for f in data['findings'] if f['scope'] == 'server'] == [
        (['requestOptions', 'headers', 'X.Key'], 'interpolation')]


def test_finding_tokens_controls_and_typed_indices_are_masked(tmp_path):
    token = 'ghp_' + 'a' * 30
    args = inputs(tmp_path, {token: {'command': 'value', 'vendor': {
        token: [{'control\n.key': '${LITERAL}', 'API_TOKEN': 'sensitive-value'}]}}},
                  {token: {'command': 'value'}}, 'claude', 'codex')
    before = tree(tmp_path)
    data = check(args)
    assert token not in json.dumps(data) and 'sensitive-value' not in json.dumps(data)
    assert all(f['server'] == '[REDACTED]' for f in data['findings'])
    assert any(f['path'] == ['vendor', '[REDACTED]', 0, 'control\n.key'] for f in data['findings'])
    assert any(f['path'] == ['vendor', '[REDACTED]', 0, 'API_TOKEN'] and f['kind'] == 'secret'
               for f in data['findings'])
    assert tree(tmp_path) == before


@pytest.mark.parametrize('side', ['source', 'target'])
def test_invalid_input_discards_even_precollected_fidelity(tmp_path, side):
    args = inputs(tmp_path, {'s': {'command': 'value', 'vendor': {'a.b': 1}}},
                  {}, 'claude', 'codex')
    (tmp_path / side).write_text('{"broken":"sensitive-value"')
    before = tree(tmp_path)
    data = check(args, 2)
    assert data['drift'] == data['findings'] == [] and data['error']
    assert 'sensitive-value' not in json.dumps(data)
    assert tree(tmp_path) == before


def test_document_losses_and_warnings_do_not_imply_server_drift(tmp_path):
    args = inputs(tmp_path, {'s': {'command': 'value', 'args': ['${LITERAL}']}},
                  {'s': {'command': 'value', 'args': ['${LITERAL}']}, 'extra': {'command': 'ignored'}},
                  'claude', 'codex')
    src = tmp_path / 'source'
    raw = json.loads(src.read_text()); raw['root.dot'] = None; src.write_text(json.dumps(raw))
    data = check(args, 0)
    assert data['drift'] == []
    assert [(f['scope'], f['path'], f['kind']) for f in data['findings']] == [
        ('server', ['args', 0], 'interpolation'), ('document', ['root.dot'], 'loss')]


@pytest.mark.parametrize('key,value', [('', []), ('array[0]', [False, None]), ('a.b', {}), ('工具/', None)])
def test_passthrough_boundaries_keep_literal_empty_and_array_keys(tmp_path, key, value):
    result = check(inputs(tmp_path, {'s': {'command': 'value', key: value}},
                          {'s': {'command': 'value'}}, 'claude', 'codex'))
    assert [(f['path'], f['kind']) for f in result['findings']] == [([key], 'loss')]


def test_same_format_document_secret_has_root_coordinates(tmp_path):
    token = 'ghp_' + 'b' * 30
    args = inputs(tmp_path, {'s': {'command': 'value'}}, {'s': {'command': 'value'}})
    src = tmp_path / 'source'
    raw = json.loads(src.read_text()); raw['auth.dot'] = [{'API_TOKEN.' + token: 'sensitive-value'}]
    src.write_text(json.dumps(raw))
    before = tree(tmp_path)
    data = check(args, 0)
    assert len(data['findings']) == 1
    f = data['findings'][0]
    assert (f['scope'], f['server'], f['path'], f['kind']) == (
        'document', None, ['auth.dot', 0, 'API_TOKEN.[REDACTED]'], 'secret')
    assert token not in json.dumps(data) and 'sensitive-value' not in json.dumps(data)
    assert tree(tmp_path) == before


def test_invalid_extra_target_server_is_not_hidden_by_ignored_semantics(tmp_path):
    args = inputs(tmp_path, {'s': {'command': 'value', 'lost': {'a.b': 1}}},
                  {'s': {'command': 'value'}, 'extra': {'command': 42}}, 'claude', 'codex')
    data = check(args, 2)
    assert data['drift'] == data['findings'] == []
