"""Synthetic native snapshots: no client settings, secrets, network or execution."""
import json
from pathlib import Path

import pytest

from agentwrench.mcpconfig.formats import FORMATS, loads, new_document
from .test_bridge import invoke


def snapshots(tmp_path, fmt, base, source, target):
    files = [tmp_path / name for name in ('base', 'source', 'target')]
    for path, servers in zip(files, (base, source, target)):
        path.write_text(new_document(fmt, servers))
    args = ['convert', '--from', fmt, '--to', fmt, files[1], '--base', files[0],
            '--merge', '-o', files[2]]
    return files, args


@pytest.mark.parametrize('fmt', FORMATS)
def test_three_way_preserves_independent_target_edits_and_previews(tmp_path, fmt, monkeypatch):
    monkeypatch.setenv('REGION', 'must-not-expand')
    base = {'s': {'command': 'old', 'args': ['base'], 'env': {'REGION': '${REGION}'}}}
    source = {'s': {**base['s'], 'command': 'new'}}
    target = {'s': {**base['s'], 'args': ['local'], 'env': {'REGION': '${REGION}', 'LOCAL': 'eu'},
                    'extension': {'opaque': [True, 'native']}}, 'local': {'command': 'local'}}
    files, args = snapshots(tmp_path, fmt, base, source, target)
    original = files[2].read_bytes()
    preview = invoke(*args, '--dry-run')
    assert preview.returncode == 0, preview.stderr
    assert 'three-way' in preview.stdout
    assert 'CHANGE s.command' in preview.stdout
    assert 'new' not in preview.stdout
    assert files[2].read_bytes() == original
    assert not Path(str(files[2]) + '.bak').exists()
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    expected = {**target, 's': {**target['s'], 'command': 'new'}}
    assert loads(files[2].read_text(), fmt).servers == expected
    assert Path(str(files[2]) + '.bak').read_bytes() == original
    # Reapplying the same source/base is a no-op, even with an existing backup.
    assert invoke(*args).returncode == 0
    assert 'must-not-expand' not in files[2].read_text() + result.stdout + result.stderr


@pytest.mark.parametrize('fmt', FORMATS)
def test_three_way_reports_all_conflicts_without_values_or_writes(tmp_path, fmt):
    base = {'s': {'command': 'base-value', 'args': ['base-value']}}
    source = {'s': {'command': 'incoming-value', 'args': ['incoming-value']}}
    target = {'s': {'command': 'target-value', 'args': ['target-value']}}
    files, args = snapshots(tmp_path, fmt, base, source, target)
    before = {p: p.read_bytes() for p in files}
    for extra in ([], ['--dry-run']):
        result = invoke(*args, *extra)
        assert result.returncode == 2
        assert 'CONFLICT s.command' in result.stderr
        assert 'CONFLICT s.args' in result.stderr
        assert all(value not in result.stdout + result.stderr for value in
                   ('base-value', 'incoming-value', 'target-value'))
        assert {p: p.read_bytes() for p in files} == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ['base', 'source', 'target']


@pytest.mark.parametrize('fmt', FORMATS)
def test_three_way_deletes_only_unchanged_base_fields_and_servers(tmp_path, fmt):
    base = {'s': {'command': 'x', 'args': ['old'], 'env': {'DROP': 'old', 'KEEP': 'old'}},
            'gone': {'command': 'gone'}, 'local-deletion': {'command': 'x'}}
    source = {'s': {'command': 'x', 'env': {'KEEP': 'old'}},
              'local-deletion': {'command': 'x'}, 'added': {'command': 'new'}}
    target = {'s': {**base['s'], 'env': {**base['s']['env'], 'EXTRA': 'local'}, 'custom': 7},
              'gone': base['gone'], 'target-only': {'command': 'stay'}}
    files, args = snapshots(tmp_path, fmt, base, source, target)
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert loads(files[2].read_text(), fmt).servers == {
        's': {'command': 'x', 'env': {'KEEP': 'old', 'EXTRA': 'local'}, 'custom': 7},
        'added': {'command': 'new'}, 'target-only': {'command': 'stay'}}


@pytest.mark.parametrize('fmt', FORMATS)
def test_three_way_can_remove_all_servers(tmp_path, fmt):
    files, args = snapshots(tmp_path, fmt, {'a': {'command': 'a'}, 'b': {'command': 'b'}},
                            {}, {'a': {'command': 'a'}, 'b': {'command': 'b'}})
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert loads(files[2].read_text(), fmt).servers == {}


@pytest.mark.parametrize('base,source,target,path', [
    ({'command': 'x'}, None, {'command': 'local'}, 's'),
    ({'command': 'x'}, {'command': 'new'}, None, 's'),
    (None, {'command': 'x'}, {'command': 'local'}, 's'),
    ({'command': 'x', 'custom': True}, {'command': 'x', 'custom': 1},
     {'command': 'x', 'custom': False}, 's.custom'),
])
def test_three_way_delete_modify_and_concurrent_add_conflicts(tmp_path, base, source, target, path):
    docs = [{'s': raw} if raw is not None else {} for raw in (base, source, target)]
    files, args = snapshots(tmp_path, 'claude', *docs)
    before = files[2].read_bytes()
    result = invoke(*args)
    assert result.returncode == 2 and f'CONFLICT {path}' in result.stderr
    assert files[2].read_bytes() == before and not Path(str(files[2]) + '.bak').exists()


def test_three_way_projects_both_source_snapshots_to_target_format(tmp_path):
    base = tmp_path / 'base.json'
    source = tmp_path / 'source.json'
    target = tmp_path / 'target.toml'
    base.write_text(json.dumps({'mcpServers': {'s': {'command': 'old'}}}))
    source.write_text(json.dumps({'mcpServers': {'s': {'command': 'new'}}}))
    target.write_text('# keep root\nmodel="local"\n[mcp_servers.s]\ncommand="old" # keep field\n'
                      'enabled=false\ncustom={opaque="stay"}\n\n[mcp_servers.local]\ncommand="stay"\n')
    result = invoke('convert', '--from', 'claude', '--to', 'codex', source,
                    '--base', base, '--merge', '-o', target)
    assert result.returncode == 0, result.stderr
    output = target.read_text()
    assert loads(output, 'codex').servers == {
        's': {'command': 'new', 'enabled': False, 'custom': {'opaque': 'stay'}},
        'local': {'command': 'stay'}}
    assert output.startswith('# keep root\nmodel="local"\n')
    assert '# keep field' in output


@pytest.mark.parametrize('extra', [['--overwrite'], ['--redact-secrets']])
def test_three_way_rejects_incompatible_flags(tmp_path, extra):
    files, args = snapshots(tmp_path, 'claude', {}, {}, {})
    before = files[2].read_bytes()
    result = invoke(*args, *extra)
    assert result.returncode == 2
    assert files[2].read_bytes() == before


def test_three_way_requires_merge_and_existing_output(tmp_path):
    files, args = snapshots(tmp_path, 'claude', {}, {}, {})
    assert invoke(*[arg for arg in args if arg != '--merge']).returncode == 2
    files[2].unlink()
    assert invoke(*args).returncode == 2
    assert not files[2].exists()


def test_three_way_unsupported_projection_never_becomes_deletion(tmp_path):
    base, source, target = [tmp_path / name for name in ('base', 'source', 'target')]
    base.write_text('{"mcpServers":{"s":{"command":"x"}}}')
    source.write_text('{"mcpServers":{"s":{"type":"sse","url":"https://example.test/sse"},'
                      '"good":{"command":"good"}}}')
    target.write_text('[mcp_servers.s]\ncommand="x"\n')
    before = target.read_bytes()
    result = invoke('convert', '--from', 'claude', '--to', 'codex', source,
                    '--base', base, '--merge', '-o', target)
    assert result.returncode == 3
    assert target.read_bytes() == before and not Path(str(target) + '.bak').exists()


@pytest.mark.parametrize('fmt', FORMATS)
def test_three_way_replace_entire_collection_and_change_transport(tmp_path, fmt):
    base = {'old': {'command': 'old'}}
    files, args = snapshots(tmp_path, fmt, base, {'new': {'command': 'new'}}, base)
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert loads(files[2].read_text(), fmt).servers == {'new': {'command': 'new'}}
    # No overlay residue from the former command transport may remain.
    other = tmp_path / 'transport'
    other.mkdir()
    incoming = {'s': {'url': 'https://example.test/mcp'}}
    if fmt != 'codex':
        incoming['s']['type'] = {'cline': 'streamableHttp', 'continue': 'streamable-http'}.get(fmt, 'http')
    base = {'s': {'command': 'old', 'args': ['old'], 'env': {'OLD': 'old'}}}
    files, args = snapshots(other, fmt, base, incoming, base)
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert loads(files[2].read_text(), fmt).servers == incoming


@pytest.mark.parametrize('source,target', [
    ({'args': ['incoming']}, {'args': ['local']}),
    ({'custom': None}, {'custom': 'local'}),
])
def test_three_way_lists_and_null_are_not_missing_or_recursively_combined(tmp_path, source, target):
    base = {'s': {'command': 'x', 'args': ['base'], 'custom': None}}
    files, args = snapshots(tmp_path, 'claude', base,
                            {'s': {**base['s'], **source}}, {'s': {**base['s'], **target}})
    result = invoke(*args)
    if 'args' in source:
        assert result.returncode == 2 and 'CONFLICT s.args' in result.stderr
    else:
        # Incoming null is unchanged, so the independent local edit survives.
        assert result.returncode == 0, result.stderr
        assert loads(files[2].read_text(), 'claude').servers['s']['custom'] == 'local'


@pytest.mark.parametrize('removed', [['a'], ['b'], ['c'], ['a', 'b'], ['b', 'c'], ['a', 'c'], ['a', 'b', 'c']])
def test_three_way_jsonc_deletions_preserve_untouched_spans_and_root(tmp_path, removed):
    base = {name: {'command': name} for name in ('a', 'b', 'c')}
    source = {n: v for n, v in base.items() if n not in removed}
    files, args = snapshots(tmp_path, 'vscode', base, source, base)
    files[2].write_text('{/*root*/ "servers": {"a":{"command":"a",}, /*a*/\n'
                        '"b":{"command":"b"}, /*b*/ "c":{"command":"c"}, /*c*/},\n'
                        '"inputs": [ /*keep exactly*/ ], "unknown":  7}\n')
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    output = files[2].read_text()
    assert loads(output, 'vscode').servers == source
    assert output.endswith('"inputs": [ /*keep exactly*/ ], "unknown":  7}\n')
    assert all('/*' + name + '*/' in output for name in ('a', 'b', 'c'))
    for name in source:
        assert '"' + name + '":{"command":"' + name + '"' in output


def test_three_way_yaml_removal_preserves_root_and_literal_comments(tmp_path):
    base = {'a': {'command': 'x'}, 'b': {'command': 'y'}}
    files, args = snapshots(tmp_path, 'continue', base, {'b': base['b']}, base)
    files[2].write_text('# root\nname: local\nmcpServers:\n  - name: a\n    command: x\n'
                        '  # border\n  - name: b\n    command: y\n    custom: |\n'
                        '      # literal\n      # end\n# tail\nrules: [] # keep\n')
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    output = files[2].read_text()
    assert '  # border\n  - name: b\n    command: y\n    custom: |\n      # literal\n      # end\n' in output
    assert output.startswith('# root\nname: local\n') and output.endswith('# tail\nrules: [] # keep\n')


def test_three_way_base_projection_error_is_also_atomic(tmp_path):
    base, source, target = [tmp_path / name for name in ('base', 'source', 'target')]
    base.write_text('{"mcpServers":{"bad":{"type":"sse","url":"https://example.test/sse"}}}')
    source.write_text('{"mcpServers":{"s":{"command":"new"}}}')
    target.write_text('[mcp_servers.local]\ncommand="local"\n')
    before = target.read_bytes()
    result = invoke('convert', '--from', 'claude', '--to', 'codex', source,
                    '--base', base, '--merge', '-o', target)
    assert result.returncode == 3 and 'Base snapshot fidelity' in result.stderr
    assert target.read_bytes() == before and not Path(str(target) + '.bak').exists()


def test_three_way_validates_combined_result_before_writing(tmp_path):
    # Each snapshot is valid, but the target-only native auth field is incompatible
    # with the incoming switch from remote to stdio.
    base = {'s': {'url': 'https://example.test/mcp'}}
    files, args = snapshots(tmp_path, 'codex', base, {'s': {'command': 'new'}},
                            {'s': {**base['s'], 'bearer_token_env_var': 'LOCAL_TOKEN'}})
    before = files[2].read_bytes()
    result = invoke(*args)
    assert result.returncode == 2 and 'stdio' in result.stderr
    assert files[2].read_bytes() == before and not Path(str(files[2]) + '.bak').exists()


def test_three_way_reuses_backup_guard_and_refuses_source_credentials(tmp_path):
    base = {'s': {'command': 'old'}}
    files, args = snapshots(tmp_path, 'claude', base, {'s': {'command': 'new'}}, base)
    backup = Path(str(files[2]) + '.bak')
    backup.write_text('keep backup')
    before = files[2].read_bytes()
    result = invoke(*args)
    assert result.returncode == 2 and 'backup already exists' in result.stderr
    assert files[2].read_bytes() == before and backup.read_text() == 'keep backup'
    files[1].write_text('{"mcpServers":{"s":{"command":"new","env":{"TOKEN":"synthetic-literal"}}}}')
    result = invoke(*args)
    assert result.returncode == 2 and 'literal credential' in result.stderr
    assert 'synthetic-literal' not in result.stderr + result.stdout
    assert files[2].read_bytes() == before


def test_three_way_removes_indentless_yaml_collection(tmp_path):
    files, args = snapshots(tmp_path, 'continue', {'a': {'command': 'x'}}, {}, {'a': {'command': 'x'}})
    files[2].write_text('mcpServers:\n- name: a\n  command: x\nrules: [] # keep\n')
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert loads(files[2].read_text(), 'continue').servers == {}
    assert files[2].read_text().endswith('rules: [] # keep\n')


@pytest.mark.parametrize('fmt', FORMATS)
def test_three_way_boolean_numeric_edit_is_not_a_noop(tmp_path, fmt):
    base = {'s': {'command': 'x', 'custom': True}}
    files, args = snapshots(tmp_path, fmt, base, {'s': {'command': 'x', 'custom': 1}}, base)
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert type(loads(files[2].read_text(), fmt).servers['s']['custom']) is int


def test_three_way_null_can_be_deleted_without_losing_target_only_null(tmp_path):
    files, args = snapshots(tmp_path, 'claude', {'s': {'command': 'x', 'drop': None}},
                            {'s': {'command': 'x'}}, {'s': {'command': 'x', 'drop': None, 'keep': None}})
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert loads(files[2].read_text(), 'claude').servers == {'s': {'command': 'x', 'keep': None}}
