"""Value-free CI drift contract; all inputs are synthetic native documents."""
import json

import pytest

from agentwrench.mcpconfig.cli import main
from agentwrench.mcpconfig.formats import FORMATS, new_document
from .test_bridge import invoke


def inputs(tmp_path, source, target, source_format='claude', target_format=None):
    target_format = target_format or source_format
    src, dst = tmp_path / 'source', tmp_path / 'target'
    src.write_text(new_document(source_format, source))
    dst.write_text(new_document(target_format, target))
    return ['check', '--source', src, '--from', source_format,
            '--target', dst, '--to', target_format]


def tree(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode) for p in root.iterdir()}


def record(args, code=0):
    result = invoke(*args, '--json')
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stderr == ''
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'drift', 'findings', 'error'}
    assert data['schemaVersion'] == 2 and data['operation'] == 'check'
    assert data['status'] == {0: 'in-sync', 1: 'drift', 2: 'error'}[code]
    assert data['error'] is None if code != 2 else bool(data['error'])
    for item in data['drift']:
        assert set(item) == {'path', 'kind', 'basis'}
        assert item['kind'] in ('add', 'change', 'delete')
        assert item['basis'] in ('normalized', 'native')
    for finding in data['findings']:
        assert set(finding) == {'scope', 'server', 'path', 'field', 'kind', 'message'}
    return data


@pytest.mark.parametrize('fmt', FORMATS)
def test_json_check_classifies_literal_segmented_paths(tmp_path, fmt):
    name = 'worker.工具/one'
    source = {name: {'command': 'new-value', 'args': ['b', 'a'],
                     'env': {'ADD.KEY': 'source-value'},
                     'opaque': {'': False, 'added.map': {'k': 'source-value'}}},
              'added': {'command': 'source-value'}}
    target = {name: {'command': 'old-value', 'args': ['a', 'b'],
                     'env': {'DROP.KEY': 'target-value'},
                     'opaque': {'': 0, 'deleted.map': {'k': 'target-value'}}},
              'unrelated': {'command': 'target-value'}}
    args = inputs(tmp_path, source, target, fmt)
    (tmp_path / 'target.bak').write_bytes(b'existing backup')
    before = tree(tmp_path)
    data = record(args, 1)
    assert data['drift'] == [
        {'path': ['added'], 'kind': 'add', 'basis': 'normalized'},
        *[{'path': [name, key], 'kind': 'change', 'basis': 'normalized'}
          for key in ('args', 'command')],
        {'path': [name, 'env', 'ADD.KEY'], 'kind': 'add', 'basis': 'normalized'},
        {'path': [name, 'env', 'DROP.KEY'], 'kind': 'delete', 'basis': 'normalized'},
        {'path': [name, 'opaque', ''], 'kind': 'change', 'basis': 'native'},
        {'path': [name, 'opaque', 'added.map'], 'kind': 'add', 'basis': 'native'},
        {'path': [name, 'opaque', 'deleted.map'], 'kind': 'delete', 'basis': 'native'}]
    assert data['findings'] == []
    assert all(v not in json.dumps(data) for v in ('source-value', 'target-value', 'old-value', 'new-value'))
    assert tree(tmp_path) == before


@pytest.mark.parametrize('fmt', FORMATS)
def test_json_check_defaults_and_extra_servers_ignored(tmp_path, fmt):
    source = {'s': {'command': 'value'}}
    native = {'command': 'value', 'args': [], 'env': {}}
    if fmt == 'codex':
        native['enabled'] = True
    elif fmt == 'cline':
        native['disabled'] = False
    args = inputs(tmp_path, source, {'s': native, 'extra': {'command': 'value'}}, fmt)
    data = record(args)
    assert data == {'schemaVersion': 2, 'operation': 'check', 'status': 'in-sync',
                    'drift': [], 'findings': [], 'error': None}


@pytest.mark.parametrize('source_format', FORMATS)
@pytest.mark.parametrize('target_format', FORMATS)
def test_json_check_all_format_directions(tmp_path, source_format, target_format):
    source = {'s': {'command': 'value', 'args': ['one', 'two'], 'env': {'REGION': 'eu'}}}
    args = inputs(tmp_path, source, {**source, 'extra': {'command': 'value'}},
                  source_format, target_format)
    assert record(args)['drift'] == []


def test_json_check_null_absence_bool_number_and_key_collisions(tmp_path):
    source = {'s': {'command': 'value', 'opaque': {'null.add': None, 'bool': [False],
                                                 'a.b': 1, 'a': {'b': 2}}, 'transport': None}}
    target = {'s': {'command': 'value', 'opaque': {'null.delete': None, 'bool': [0],
                                                 'a.b': 2, 'a': {'b': 1}}}}
    args = inputs(tmp_path, source, target)
    assert record(args, 1)['drift'] == [
        {'path': ['s', 'opaque', 'a', 'b'], 'kind': 'change', 'basis': 'native'},
        {'path': ['s', 'opaque', 'a.b'], 'kind': 'change', 'basis': 'native'},
        {'path': ['s', 'opaque', 'bool'], 'kind': 'change', 'basis': 'native'},
        {'path': ['s', 'opaque', 'null.add'], 'kind': 'add', 'basis': 'native'},
        {'path': ['s', 'opaque', 'null.delete'], 'kind': 'delete', 'basis': 'native'},
        {'path': ['s', 'transport'], 'kind': 'add', 'basis': 'native'}]
    # Input map order cannot reorder drift records.
    args = inputs(tmp_path, dict(reversed(list(source.items()))),
                  {'s': dict(reversed(list(target['s'].items())))})
    assert record(args, 1)['drift'][0]['path'] == ['s', 'opaque', 'a', 'b']


@pytest.mark.parametrize('source,target,from_fmt,to_fmt,field,kind', [
    ({'command': 'value', 'timeout': 12}, {'command': 'value'}, 'cline', 'codex', 'timeout', 'loss'),
    ({'type': 'sse', 'url': 'https://example.test/sse'}, None, 'claude', 'codex', 'type', 'error'),
    ({'command': 'value', 'disabled': True}, None, 'cline', 'claude', 'disabled', 'error'),
    ({'command': 'value', 'env': {'EMPTY': None}}, None, 'vscode', 'claude', 'env', 'error'),
    ({'command': 'value', 'vendor.dot': {'工具': 'value'}}, {'command': 'value'},
     'claude', 'codex', 'vendor.dot.工具', 'loss'),
])
def test_json_check_unrepresentable_settings_are_drift(tmp_path, source, target, from_fmt, to_fmt, field, kind):
    args = inputs(tmp_path, {'s': source}, {'s': target} if target else {}, from_fmt, to_fmt)
    before = tree(tmp_path)
    data = record(args, 1)
    assert data['drift'] == [] and any(f['field'] == field and f['kind'] == kind for f in data['findings'])
    assert tree(tmp_path) == before


def test_json_check_canonical_remote_paths_and_document_warnings(tmp_path):
    args = inputs(tmp_path,
                  {'remote.工具': {'url': 'https://example.test/mcp', 'headers': {'X.Region': '${SOURCE}'}}},
                  {'remote.工具': {'url': 'https://example.test/mcp', 'http_headers': {'X.Region': '${TARGET}'},
                                  'native-only': 'ignored-value'}}, 'claude', 'codex')
    src = tmp_path / 'source'
    source = json.loads(src.read_text())
    source['document.setting'] = 'ignored-value'
    src.write_text(json.dumps(source))
    data = record(args, 1)
    assert data['drift'] == [{'path': ['remote.工具', 'headers', 'X.Region'], 'kind': 'change', 'basis': 'normalized'}]
    assert {f['kind'] for f in data['findings']} == {'interpolation', 'loss'}
    (tmp_path / 'target').write_text('[mcp_servers."remote.工具"]\nurl="https://example.test/mcp"\n'
                                     'http_headers={"X.Region"="${SOURCE}"}\n')
    assert record(args)['drift'] == []


@pytest.mark.parametrize('boundary', ['missing-source', 'missing-target', 'malformed-source', 'malformed-target',
                                     'invalid-source', 'invalid-target', 'duplicate-keys', 'non-utf8',
                                     'symlink', 'directory', 'oversized', 'too-deep'])
def test_json_check_invalid_inputs_no_partial_records_or_writes(tmp_path, boundary):
    args = inputs(tmp_path, {'s': {'command': 'source-value'}}, {'s': {'command': 'target-value'}})
    src, dst = tmp_path / 'source', tmp_path / 'target'
    if boundary.startswith('missing-'):
        (src if boundary.endswith('source') else dst).unlink()
    elif boundary.startswith('malformed-'):
        (src if boundary.endswith('source') else dst).write_text('{"unclosed":"sensitive-value"')
    elif boundary.startswith('invalid-'):
        (src if boundary.endswith('source') else dst).write_text('{"mcpServers":{"s":{"command":42}}}')
    elif boundary == 'duplicate-keys':
        src.write_text('{"mcpServers":{},"mcpServers":{}}')
    elif boundary == 'non-utf8':
        src.write_bytes(b'\xff')
    elif boundary == 'symlink':
        src.unlink()
        src.symlink_to(dst)
    elif boundary == 'directory':
        src.unlink()
        src.mkdir()
    elif boundary == 'oversized':
        src.write_bytes(b' ' * (1024 * 1024 + 1))
    else:
        src.write_text('[' * 70 + '0' + ']' * 70)
    before = {p.name: (p.is_symlink(), p.read_bytes() if p.is_file() else None) for p in tmp_path.iterdir()}
    data = record(args, 2)
    assert data['drift'] == data['findings'] == []
    assert all(v not in json.dumps(data) for v in ('source-value', 'target-value', 'sensitive-value', str(tmp_path)))
    assert before == {p.name: (p.is_symlink(), p.read_bytes() if p.is_file() else None) for p in tmp_path.iterdir()}


def test_json_check_non_disclosure_of_values_and_token_names(tmp_path):
    token = 'ghp_' + 'a' * 30
    args = inputs(tmp_path, {token: {'command': 'source-value', 'env': {'TOKEN': 'sensitive-source'},
                                    'unknown': {token: 'opaque-source', 'control\n.key': 'old'}}},
                  {token: {'command': 'target-value', 'env': {'TOKEN': 'sensitive-target'},
                           'unknown': {token: 'opaque-target', 'control\n.key': 'new'}}})
    before = tree(tmp_path)
    data = record(args, 1)
    text = json.dumps(data)
    assert all(v not in text for v in (token, 'source-value', 'target-value', 'sensitive-', 'opaque-source', 'opaque-target'))
    assert data['drift'][0]['path'][0] == '[REDACTED]'
    assert any(r['path'][-1] == 'control\n.key' for r in data['drift'])
    assert tree(tmp_path) == before


def test_json_check_masks_unexpected_processing_errors(tmp_path, monkeypatch, capsys):
    args = inputs(tmp_path, {'s': {'command': 'value'}}, {})
    def fail(*args):
        raise ValueError('sensitive-value')
    monkeypatch.setattr('agentwrench.mcpconfig.cli.project', fail)
    assert main([*map(str, args), '--json']) == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert captured.err == '' and data['status'] == 'error'
    assert data['drift'] == data['findings'] == []
    assert 'sensitive-value' not in captured.out


@pytest.mark.parametrize('options', [[], ['--from', 'invalid'], ['--unknown']])
def test_json_check_parser_errors_keep_existing_usage_contract(options):
    result = invoke('check', '--json', *options)
    assert result.returncode == 2 and result.stdout == '' and 'usage:' in result.stderr


def test_document_named_server_loss_cannot_claim_in_sync(tmp_path):
    args = inputs(tmp_path, {'(document)': {'command': 'value', 'vendor': 'opaque-value'}},
                  {'(document)': {'command': 'value'}}, 'claude', 'codex')
    assert record(args, 1)['findings'][0]['server'] == '(document)'
    human = invoke(*args)
    assert human.returncode == 1 and '(unrepresentable)' in human.stdout


def test_json_check_normalized_aliases_and_native_basis_collision(tmp_path):
    args = inputs(tmp_path, {'s': {'command': 'source-value', 'transport': 'native-source'}},
                  {'s': {'url': 'https://example.test/mcp', 'transport': 'native-target'}})
    assert [item for item in record(args, 1)['drift'] if item['path'] == ['s', 'transport']] == [
        {'path': ['s', 'transport'], 'kind': 'change', 'basis': 'native'},
        {'path': ['s', 'transport'], 'kind': 'change', 'basis': 'normalized'}]
    args = inputs(tmp_path, {'s': {'type': 'streamableHttp', 'url': 'https://example.test/mcp',
                                  'headers': {}, 'disabled': False}},
                  {'s': {'url': 'https://example.test/mcp', 'enabled': True}}, 'cline', 'codex')
    assert record(args)['drift'] == []


def test_json_check_record_order_independent_of_source_and_target_maps(tmp_path):
    source = {'z': {'command': 'source-value', 'vendor': {'z': 1, 'a.b': 1}},
              'a': {'command': 'source-value'}}
    target = {'z': {'command': 'target-value', 'vendor': {'z': 2, 'a.b': 2}},
              'a': {'command': 'target-value'}}
    first = record(inputs(tmp_path, source, target), 1)
    def reverse(value):
        return {k: reverse(v) for k, v in reversed(list(value.items()))} if isinstance(value, dict) else value
    assert record(inputs(tmp_path, reverse(source), reverse(target)), 1) == first
