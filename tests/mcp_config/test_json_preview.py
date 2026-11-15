"""CI preview contracts: segmented paths, classified edits, masked failures, no writes."""
import json

import pytest

from agentwrench.mcpconfig.formats import FORMATS
from .test_bridge import invoke
from .test_three_way import snapshots


def preview(args, code=0):
    result = invoke(*args, '--dry-run', '--json')
    assert result.returncode == code, result.stderr or result.stdout
    assert result.stderr == ''
    data = json.loads(result.stdout)
    assert set(data) == {'schemaVersion', 'operation', 'status', 'changes', 'conflicts',
                         'servers', 'findings', 'error'}
    assert data['schemaVersion'] == 2 and data['operation'] == 'three-way-merge'
    return data


def tree(root):
    return {p.name: p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize('fmt', FORMATS)
def test_json_preview_classifies_target_changes_with_literal_segments(tmp_path, fmt):
    name = 'worker.工具/one'
    base = {name: {'command': 'old-value', 'args': ['old-value'],
                   'env': {'DROP.KEY': 'drop-value'}, 'opaque': {'': False}},
            'gone': {'command': 'gone-value'}}
    source = {name: {'command': 'new-value', 'args': ['new-value'],
                     'env': {'ADD.KEY': 'add-value'}, 'opaque': {'': 0}},
              'added': {'command': 'added-value'}}
    target = {**base, name: {**base[name], 'local': {'keep': 'local-value'}},
              'local-only': {'command': 'local-value'}}
    _, args = snapshots(tmp_path, fmt, base, source, target)
    before = tree(tmp_path)
    data = preview(args)
    assert data == {
        'schemaVersion': 2, 'operation': 'three-way-merge', 'status': 'preview',
        'changes': [
            {'path': ['added'], 'kind': 'add'}, {'path': ['gone'], 'kind': 'delete'},
            {'path': [name, 'args'], 'kind': 'change'},
            {'path': [name, 'command'], 'kind': 'change'},
            {'path': [name, 'env', 'ADD.KEY'], 'kind': 'add'},
            {'path': [name, 'env', 'DROP.KEY'], 'kind': 'delete'},
            {'path': [name, 'opaque', ''], 'kind': 'change'}],
        'conflicts': [], 'servers': {'added': 1, 'removed': 1, 'total': 3},
        'findings': {'source': [], 'base': []}, 'error': None}
    assert tree(tmp_path) == before


def test_json_preview_noop_equal_edits_local_deletions_and_existing_backup(tmp_path):
    base = {'s': {'command': 'old-value', 'args': ['base-value']}, 'deleted': {'command': 'x'}}
    source = {'s': {'command': 'new-value', 'args': ['base-value']}, 'deleted': base['deleted']}
    target = {'s': {'command': 'new-value', 'args': ['local-value']}}
    _, args = snapshots(tmp_path, 'claude', base, source, target)
    (tmp_path / 'target.bak').write_text('existing-backup-value')
    before = tree(tmp_path)
    data = preview(args)
    assert data['changes'] == [] and data['conflicts'] == []
    assert data['servers'] == {'added': 0, 'removed': 0, 'total': 1}
    assert tree(tmp_path) == before


def test_json_preview_null_missing_and_atomic_maps(tmp_path):
    base = {'s': {'command': 'x', 'drop': None, 'map': {'old': 1}}}
    source = {'s': {'command': 'x', 'add': None, 'map': False, 'empty': {}}}
    _, args = snapshots(tmp_path, 'claude', base, source, base)
    data = preview(args)
    assert data['changes'] == [
        {'path': ['s', 'add'], 'kind': 'add'}, {'path': ['s', 'drop'], 'kind': 'delete'},
        {'path': ['s', 'empty'], 'kind': 'add'}, {'path': ['s', 'map'], 'kind': 'change'}]


@pytest.mark.parametrize('fmt', FORMATS)
def test_json_preview_conflicts_classify_both_sides_and_hide_partial_plan(tmp_path, fmt):
    base = {'delete-incoming': {'command': 'base-value'},
            'delete-target': {'command': 'base-value'},
            's.工具': {'command': 'base-value', 'args': ['base-value']},
            'safe': {'command': 'base-value'}}
    source = {'added': {'command': 'incoming-value'},
              'delete-target': {'command': 'incoming-value'},
              's.工具': {'command': 'incoming-value', 'args': ['incoming-value']},
              'safe': {'command': 'incoming-value'}}
    target = {'added': {'command': 'target-value'},
              'delete-incoming': {'command': 'target-value'},
              's.工具': {'command': 'target-value', 'args': ['target-value']},
              'safe': base['safe']}
    _, args = snapshots(tmp_path, fmt, base, source, target)
    before = tree(tmp_path)
    data = preview(args, 2)
    assert data['status'] == 'conflict' and data['error']
    assert data['changes'] == [] and data['servers'] is None
    assert data['conflicts'] == [
        {'path': ['added'], 'incoming': 'add', 'target': 'add'},
        {'path': ['delete-incoming'], 'incoming': 'delete', 'target': 'change'},
        {'path': ['delete-target'], 'incoming': 'change', 'target': 'delete'},
        {'path': ['s.工具', 'args'], 'incoming': 'change', 'target': 'change'},
        {'path': ['s.工具', 'command'], 'incoming': 'change', 'target': 'change'}]
    assert all(value not in json.dumps(data) for value in
               ('base-value', 'incoming-value', 'target-value'))
    assert tree(tmp_path) == before


def test_json_preview_escapes_controls_and_masks_token_signatures_in_target_keys(tmp_path):
    name = 'worker.工具"\\'
    field = 'nested.工具\n"\\'
    secret_key = 'ghp_synthetic12345678'
    base = {name: {'command': 'x', field: 'base-value'}}
    source = {name: {'command': 'x', field: 'incoming-value'}}
    target = {name: {'command': 'x', field: 'target-value'}, secret_key: {'command': 'local-value'}}
    _, args = snapshots(tmp_path, 'claude', base, source, target)
    data = preview(args, 2)
    assert data['conflicts'] == [{'path': [name, field], 'incoming': 'change', 'target': 'change'}]
    # A signed key present only in BASE/TARGET can appear in a deletion diagnostic.
    _, args = snapshots(tmp_path, 'claude', {secret_key: {'command': 'x'}}, {},
                        {secret_key: {'command': 'x'}})
    data = preview(args)
    assert data['changes'] == [{'path': ['[REDACTED]'], 'kind': 'delete'}]
    assert secret_key not in json.dumps(data)


@pytest.mark.parametrize('boundary', ['no-base', 'no-dry-run', 'no-merge', 'no-output',
                                     'overwrite', 'redact', 'missing-target', 'malformed-base',
                                     'malformed-source', 'malformed-target', 'source-secret'])
def test_json_preview_negative_contracts_leave_everything_untouched(tmp_path, boundary):
    base = {'s': {'command': 'old-value'}}
    files, args = snapshots(tmp_path, 'claude', base, {'s': {'command': 'new-value'}}, base)
    args += ['--dry-run', '--json']
    flag = {'no-base': '--base', 'no-dry-run': '--dry-run', 'no-merge': '--merge',
            'no-output': '-o'}.get(boundary)
    if flag:
        index = args.index(flag)
        del args[index:index + (2 if flag in ('--base', '-o') else 1)]
    elif boundary in ('overwrite', 'redact'):
        args += ['--overwrite' if boundary == 'overwrite' else '--redact-secrets']
    elif boundary == 'missing-target':
        files[2].unlink()
    elif boundary.startswith('malformed-'):
        files[{'base': 0, 'source': 1, 'target': 2}[boundary.split('-')[1]]].write_text('{unclosed')
    else:
        files[1].write_text('{"mcpServers":{"s":{"command":"x","env":{"TOKEN":"synthetic-value"}}}}')
    before = tree(tmp_path)
    result = invoke(*args)
    assert result.returncode == 2 and result.stderr == ''
    data = json.loads(result.stdout)
    assert data['status'] == 'error' and data['error']
    assert data['changes'] == [] and data['conflicts'] == [] and data['servers'] is None
    assert data['findings'] == {'source': [], 'base': []}
    assert 'synthetic-value' not in result.stdout
    assert tree(tmp_path) == before


@pytest.mark.parametrize('unsupported', ['base', 'source'])
def test_json_preview_cross_format_projection_failure_is_not_deletion(tmp_path, unsupported):
    files, args = snapshots(tmp_path, 'claude', {'s': {'command': 'x'}}, {'s': {'command': 'x'}}, {})
    files[2].write_text('[mcp_servers.s]\ncommand="x"\n')
    args[args.index('--to') + 1] = 'codex'
    files[0 if unsupported == 'base' else 1].write_text(
        '{"mcpServers":{"s":{"type":"sse","url":"https://example.test/sse"}}}')
    before = tree(tmp_path)
    data = preview(args, 3)
    assert data['status'] == 'error' and data['error']
    assert data['changes'] == [] and data['conflicts'] == [] and data['servers'] is None
    assert any(f['kind'] == 'error' for f in data['findings'][unsupported])
    assert tree(tmp_path) == before


def test_json_preview_rejects_invalid_combined_result(tmp_path):
    base = {'s': {'url': 'https://example.test/mcp'}}
    _, args = snapshots(tmp_path, 'codex', base, {'s': {'command': 'new-value'}},
                        {'s': {**base['s'], 'bearer_token_env_var': 'LOCAL_TOKEN'}})
    before = tree(tmp_path)
    data = preview(args, 2)
    assert data['status'] == 'error' and data['changes'] == []
    assert 'stdio' in data['error']
    assert tree(tmp_path) == before


def test_json_preview_cross_format_native_paths_and_fidelity_findings(tmp_path):
    base = {'s': {'url': 'https://example.test/mcp', 'headers': {'X.Region': '${REGION}'},
                   'custom': 'native-value'}}
    source = {'s': {**base['s'], 'headers': {'X.Region': '${OTHER}'}}}
    _, args = snapshots(tmp_path, 'claude', base, source, {})
    (tmp_path / 'target').write_text('[mcp_servers.s]\nurl="https://example.test/mcp"\n'
                                     'http_headers={"X.Region"="${REGION}"}\n')
    args[args.index('--to') + 1] = 'codex'
    data = preview(args)
    assert data['changes'] == [{'path': ['s', 'http_headers', 'X.Region'], 'kind': 'change'}]
    for findings in data['findings'].values():
        assert {f['kind'] for f in findings} == {'loss', 'interpolation'}
        assert all(set(f) == {'scope', 'server', 'path', 'field', 'kind', 'message'} for f in findings)
    assert all(value not in json.dumps(data) for value in ('native-value', '${REGION}', '${OTHER}'))


def test_json_unknown_option_retains_argument_parser_contract():
    result = invoke('convert', '--json', '--unknown')
    assert result.returncode == 2 and result.stdout == ''
    assert 'usage:' in result.stderr
