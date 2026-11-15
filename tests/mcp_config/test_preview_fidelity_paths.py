"""Native source/base coordinates in schema-2 previews, never parsed labels."""
import json

import pytest

from agentwrench.mcpconfig.formats import FORMATS, new_document
from .test_bridge import invoke
from .test_three_way import snapshots


def files_state(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in root.iterdir()}


def preview(tmp_path, args, code=0):
    before = files_state(tmp_path)
    result = invoke(*args, '--dry-run', '--json')
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == ''
    assert files_state(tmp_path) == before
    data = json.loads(result.stdout)
    for group in data['findings'].values():
        for f in group:
            assert set(f) == {'scope', 'server', 'path', 'field', 'kind', 'message'}
            assert f['scope'] in ('server', 'document')
            assert (f['server'] is None) == (f['scope'] == 'document')
            assert f['path'] and all(isinstance(p, str) or type(p) is int and p >= 0 for p in f['path'])
            assert all(isinstance(f[k], str) for k in ('field', 'kind', 'message'))
    assert data['schemaVersion'] == 2
    if code:
        assert data['changes'] == [] and data['servers'] is None and data['error']
        if data['status'] == 'error':
            assert data['conflicts'] == []
    return data


def cross_format(tmp_path, base, source, target=None, fmt='claude', to='codex'):
    files, args = snapshots(tmp_path, fmt, base, source, {})
    args[args.index('--to') + 1] = to
    files[2].write_text(new_document(to, target or {}))
    return files, args


def test_literal_nested_and_document_coordinates_on_both_snapshots(tmp_path):
    servers = {'(document)': {'command': 'native-value', 'vendor': {
        'a.b': 'opaque-value', 'a': {'b': 'opaque-value'}, '': {'工具/': []},
        'rows': [{'0': False, '': None}], 'rows[0]': {'0': 42}}}}
    files, args = cross_format(tmp_path, servers, servers, {'(document)': {'command': 'native-value'}})
    for file in files[:2]:
        raw = json.loads(file.read_text()); raw.update({'a.b': {'工具': 1}, '': {}})
        file.write_text(json.dumps(raw))
    data = preview(tmp_path, args)
    expected = [
        ('server', '(document)', ['vendor', 'a.b'], 'vendor.a.b'),
        ('server', '(document)', ['vendor', 'a', 'b'], 'vendor.a.b'),
        ('server', '(document)', ['vendor', '', '工具/'], 'vendor..工具/'),
        ('server', '(document)', ['vendor', 'rows', 0, '0'], 'vendor.rows[0].0'),
        ('server', '(document)', ['vendor', 'rows', 0, ''], 'vendor.rows[0].'),
        ('server', '(document)', ['vendor', 'rows[0]', '0'], 'vendor.rows[0].0'),
        ('document', None, ['a.b'], 'a.b'), ('document', None, [''], '')]
    for group in data['findings'].values():
        assert [(f['scope'], f['server'], f['path'], f['field']) for f in group] == expected
    assert data['changes'] == []
    assert all(v not in json.dumps(data) for v in ('native-value', 'opaque-value'))


@pytest.mark.parametrize('fmt', FORMATS)
def test_both_snapshots_keep_native_spelling_and_index_types(tmp_path, fmt):
    raw = {'worker.工具': {'command': 'native-value', 'vendor.dot': {'': 'opaque-value'},
                         'args': ['${LITERAL}']}}
    to = 'claude' if fmt == 'codex' else 'codex'
    _, args = cross_format(tmp_path, raw, raw, fmt=fmt, to=to)
    data = preview(tmp_path, args)
    for group in data['findings'].values():
        assert [(f['path'], f['kind']) for f in group if f['scope'] == 'server'] == [
            (['args', 0], 'interpolation'), (['vendor.dot', ''], 'loss')]
        assert all(f['server'] == 'worker.工具' for f in group if f['scope'] == 'server')


def test_continue_headers_use_source_coordinates_and_target_changes(tmp_path):
    base = {'s': {'type': 'streamable-http', 'url': 'https://example.test/mcp',
                  'requestOptions': {'headers': {'X.Key': '${BASE}'}}}}
    source = {'s': {**base['s'], 'requestOptions': {'headers': {'X.Key': '${SOURCE}'}}}}
    target = {'s': {'url': 'https://example.test/mcp', 'http_headers': {'X.Key': '${BASE}'}}}
    _, args = cross_format(tmp_path, base, source, target, fmt='continue')
    data = preview(tmp_path, args)
    assert data['changes'] == [{'path': ['s', 'http_headers', 'X.Key'], 'kind': 'change'}]
    for group in data['findings'].values():
        assert [(f['path'], f['kind']) for f in group if f['scope'] == 'server'] == [
            (['requestOptions', 'headers', 'X.Key'], 'interpolation')]
    assert '${BASE}' not in json.dumps(data) and '${SOURCE}' not in json.dumps(data)


@pytest.mark.parametrize('side', ['source', 'base'])
@pytest.mark.parametrize('fmt,raw,to,path', [
    ('claude', {'type': 'sse', 'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('cline', {'url': 'https://example.test/sse'}, 'codex', ['type']),
    ('codex', {'command': 'value', 'enabled': False}, 'claude', ['enabled']),
    ('vscode', {'command': 'value', 'env': {'X': None}}, 'claude', ['env']),
])
def test_unsupported_snapshot_retains_coordinates_but_no_partial_plan(tmp_path, side, fmt, raw, to, path):
    base = {'safe': {'command': 'old-value'}}
    source = {'safe': {'command': 'new-value'}}
    (base if side == 'base' else source)['(document)'] = raw
    _, args = cross_format(tmp_path, base, source, base, fmt=fmt, to=to)
    data = preview(tmp_path, args, 3)
    assert data['status'] == 'error'
    assert [(f['scope'], f['server'], f['path'], f['kind']) for f in data['findings'][side]
            if f['kind'] == 'error'] == [('server', '(document)', path, 'error')]


def test_conflict_retains_scoped_findings_without_safe_partial_changes(tmp_path):
    base = {'s': {'command': 'base-value', 'vendor': {'a.b': 1}}, 'safe': {'command': 'old-value'}}
    source = {'s': {**base['s'], 'command': 'incoming-value'}, 'safe': {'command': 'new-value'}}
    target = {'s': {'command': 'target-value'}, 'safe': base['safe']}
    _, args = cross_format(tmp_path, base, source, target)
    data = preview(tmp_path, args, 2)
    assert data['status'] == 'conflict'
    assert data['conflicts'] == [{'path': ['s', 'command'], 'incoming': 'change', 'target': 'change'}]
    for group in data['findings'].values():
        assert [(f['server'], f['path']) for f in group] == [('s', ['vendor', 'a.b'])]
    assert all(v not in json.dumps(data) for v in ('base-value', 'incoming-value', 'target-value'))


@pytest.mark.parametrize('side', ['source', 'base', 'target'])
@pytest.mark.parametrize('invalid', ['syntax', 'server'])
def test_invalid_snapshot_discards_precollected_fidelity(tmp_path, side, invalid):
    raw = {'s': {'command': 'native-value', 'vendor': {'a.b': 1}}}
    files, args = cross_format(tmp_path, raw, raw)
    file = files[{'base': 0, 'source': 1, 'target': 2}[side]]
    file.write_text('{"broken":"sensitive-value"' if invalid == 'syntax' else new_document(
        'codex' if side == 'target' else 'claude', {'s': {'command': 42}}))
    data = preview(tmp_path, args, 2)
    assert data['status'] == 'error' and data['findings'] == {'source': [], 'base': []}
    assert 'sensitive-value' not in json.dumps(data)


def test_base_token_coordinates_document_secrets_and_source_refusal(tmp_path):
    token = 'ghp_' + 'a' * 30
    base = {token: {'command': 'native-value', 'vendor': {
        token: [{'control\n.key': '${LITERAL}', 'API_TOKEN': 'sensitive-value'}]}}}
    files, args = cross_format(tmp_path, base, {})
    raw = json.loads(files[0].read_text())
    raw['auth.dot'] = [{'API_TOKEN.' + token: 'document-sensitive-value'}]
    files[0].write_text(json.dumps(raw))
    data = preview(tmp_path, args)
    group = data['findings']['base']
    assert any(f['scope'] == 'server' and f['server'] == '[REDACTED]' and
               f['path'] == ['vendor', '[REDACTED]', 0, 'control\n.key'] for f in group)
    assert any(f['scope'] == 'document' and f['server'] is None and
               f['path'] == ['auth.dot', 0, 'API_TOKEN.[REDACTED]'] and f['kind'] == 'secret' for f in group)
    assert all(v not in json.dumps(data) for v in (token, 'sensitive-value', 'document-sensitive-value'))
    files[1].write_bytes(files[0].read_bytes())
    data = preview(tmp_path, args, 2)
    assert data['findings'] == {'source': [], 'base': []}


def test_legacy_report_and_human_preview_remain_unchanged(tmp_path):
    raw = {'(document)': {'command': 'native-value', 'vendor': {'a.b': 1, 'a': {'b': 2}}}}
    files, args = cross_format(tmp_path, raw, raw)
    for file in files[:2]:
        data = json.loads(file.read_text()); data['root.dot'] = None; file.write_text(json.dumps(data))
    data = preview(tmp_path, args)
    before = files_state(tmp_path)
    legacy = invoke('report', '--from', 'claude', '--to', 'codex', files[1], '--json')
    assert legacy.returncode == 0 and legacy.stderr == ''
    records = json.loads(legacy.stdout)['findings']
    assert all(set(f) == {'server', 'field', 'kind', 'message'} for f in records)
    assert all(f['server'] == '(document)' for f in records)
    assert [f['field'] for f in records] == [f['field'] for f in data['findings']['source']]
    human = invoke(*args, '--dry-run')
    assert human.returncode == 0 and 'PLAN:' in human.stdout
    assert 'Base snapshot fidelity:' in human.stderr and 'vendor.a.b' in human.stderr
    # New base document annotations are JSON-only; legacy base labels stay server-only.
    assert 'root.dot' not in human.stderr.split('Base snapshot fidelity:')[1]
    assert files_state(tmp_path) == before


def test_same_format_base_document_secret_is_annotated_without_loss_or_change(tmp_path):
    raw = {'s': {'command': 'native-value'}}
    files, args = snapshots(tmp_path, 'claude', raw, raw, raw)
    data = json.loads(files[0].read_text())
    data['auth.dot'] = [{'API_TOKEN': 'sensitive-value'}]
    files[0].write_text(json.dumps(data))
    (tmp_path / 'target.bak').write_text('existing-backup')
    data = preview(tmp_path, args)
    assert data['changes'] == [] and data['findings']['source'] == []
    assert [(f['scope'], f['server'], f['path'], f['kind']) for f in data['findings']['base']] == [
        ('document', None, ['auth.dot', 0, 'API_TOKEN'], 'secret')]
    assert 'sensitive-value' not in json.dumps(data)
