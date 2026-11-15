"""Opt-in historical manifest details preserve the effective-only gate."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture, compare
from .test_snapshot import save, snapshot


FLAG = '--include-file-changes'


def doc(result, code=0):
    assert result.returncode == code, result.stderr
    return json.loads(result.stdout)


def manifest(root, home, *cwds, version=2, budget=3):
    return capture(root, home, *cwds, options=['--snapshot-version', version, '--max-bytes', budget])


def test_cut_only_details_after_source_deletion(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', ' \n tail')
    before = manifest(root, isolated_codex_home, cwd, root)
    after = manifest(root, isolated_codex_home, cwd, root, budget=1)
    old = save(tmp_path/'before.json', before)
    new = save(tmp_path/'after.json', after)
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    default = doc(cli('diff', old, new, '--json'))
    detail_result = cli('diff', old, new, FLAG, '--json')
    detail = doc(detail_result)
    assert detail['format'] == 'agentsmd-scope.manifest-diff'
    assert detail['schema_version'] == 2
    common = {k: v for k, v in detail.items() if k not in ('schema_version', 'file_changes')}
    assert common == {k: v for k, v in default.items() if k != 'schema_version'}
    assert detail['directories'] == [] and not detail['effective_changed']
    assert [r['cwd'] for r in detail['file_changes']] == ['pkg/api', '.']
    for row in detail['file_changes']:
        assert set(row) == {'cwd', 'kind', 'before_effective', 'after_effective', 'context_changes', 'changes'}
        assert row['kind'] == 'changed'
        assert row['before_effective'] == row['after_effective']
        assert row['context_changes'] == {'settings': {'before': {'max_bytes': 3, 'fallback': [], 'root_markers': ['.git']},
                                                     'after': {'max_bytes': 1, 'fallback': [], 'root_markers': ['.git']}}}
        change, = row['changes']
        assert (change['scope'], change['path'], change['kind']) == ('project', 'AGENTS.md', 'changed')
        left, right = change['before'], change['after']
        assert left['sha256'] == right['sha256']
        assert left['bytes_loaded'] == right['bytes_loaded'] == 0
        assert left['cut'] == dict(byte_offset=3, line=2, splits_utf8_character=False)
        assert right['cut'] == dict(byte_offset=1, line=1, splits_utf8_character=False)
    human = cli('diff', old, new, FLAG)
    assert human.returncode == 0
    assert b'UNCHANGED: 0 working directories changed' in human.stdout
    assert b'changed project "AGENTS.md"' in human.stdout
    assert b'context changed: settings' in human.stdout
    assert str(tmp_path).encode() not in detail_result.stdout + human.stdout
    assert b'tail' not in detail_result.stdout + human.stdout


@pytest.mark.parametrize('versions', [(1, 1), (2, 2), (1, 2), (2, 1)])
def test_shadow_tail_effective_edits_and_version_projection(tree, tmp_path, isolated_codex_home, versions):
    root, cwd = tree
    chosen = put(root/'AGENTS.override.md', 'ABCtail')
    hidden = put(root/'AGENTS.md', '# PRIVATE_SHADOW')
    before = manifest(root, isolated_codex_home, cwd, version=versions[0])
    put(chosen, 'ABCgone')
    put(hidden, '# PRIVATE_EDIT')
    after = manifest(root, isolated_codex_home, cwd, version=versions[1])
    result = compare(tmp_path, before, after, FLAG, '--json')
    data = doc(result)
    assert data['directories'] == [] and data['summary']['changed_cwd_count'] == 0
    row, = data['file_changes']
    assert [r['path'] for r in row['changes']] == ['AGENTS.override.md', 'AGENTS.md']
    for change in row['changes']:
        for state in (change['before'], change['after']):
            assert ('cut' in state) == (versions == (2, 2))
    assert row['changes'][1]['after']['selected'] is False
    assert b'PRIVATE' not in result.stdout and b'heading' not in result.stdout
    put(chosen, 'XYZgone')
    effective = manifest(root, isolated_codex_home, cwd, version=versions[1])
    data = doc(compare(tmp_path, after, effective, FLAG, '--json'), code=1)
    assert data['effective_changed'] and data['directories'][0]['cwd'] == 'pkg/api'
    assert data['file_changes'][0]['before_effective'] != data['file_changes'][0]['after_effective']


@pytest.mark.parametrize('versions', [(1, 1), (2, 2), (1, 2), (2, 1)])
def test_schema_only_and_reordered_membership_have_no_detail(tree, tmp_path, isolated_codex_home, versions):
    root, cwd = tree
    put(root/'AGENTS.md', 'A\n€tail')
    before = manifest(root, isolated_codex_home, cwd, root, version=versions[0])
    after = manifest(root, isolated_codex_home, root, cwd, version=versions[1])
    data = doc(compare(tmp_path, before, after, FLAG, '--json'))
    assert data['directories'] == data['file_changes'] == []


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('empty', [False, True])
def test_membership_states_nulls_order_and_available_cuts(tree, tmp_path, isolated_codex_home, version, empty):
    root, cwd = tree
    if not empty:
        put(cwd/'AGENTS.md', 'A\n€tail')
    before = manifest(root, isolated_codex_home, cwd, version=version)
    after = manifest(root, isolated_codex_home, root, version=3-version)
    data = doc(compare(tmp_path, before, after, FLAG, '--json'), code=1)
    removed, added = data['file_changes']
    assert [(r['cwd'], r['kind']) for r in data['file_changes']] == [('pkg/api', 'removed'), ('.', 'added')]
    assert removed['after_effective'] is None and added['before_effective'] is None
    assert set(removed['context_changes']) == {'cwd', 'root_found', 'settings'}
    assert all(v['after'] is None for v in removed['context_changes'].values())
    assert all(v['before'] is None for v in added['context_changes'].values())
    assert added['changes'] == []
    if empty:
        assert removed['changes'] == []
    else:
        change, = removed['changes']
        assert change['kind'] == 'removed' and change['after'] is None
        assert ('cut' in change['before']) == (version == 2)
        if version == 2:
            assert change['before']['cut']['splits_utf8_character'] is True
    reverse = doc(compare(tmp_path, after, before, FLAG, '--json'), code=1)
    first, second = reverse['file_changes']
    assert [(r['cwd'], r['kind']) for r in reverse['file_changes']] == [('.', 'removed'), ('pkg/api', 'added')]
    assert first['changes'] == []
    assert second['context_changes'] == {
        key: {'before': value['after'], 'after': value['before']}
        for key, value in removed['context_changes'].items()}
    assert second['changes'] == [dict(path=r['path'], scope=r['scope'], kind='added',
                                     before=None, after=r['before']) for r in removed['changes']]


def test_context_only_change_includes_nested_snapshot_cwd(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    before = capture(root, isolated_codex_home, cwd)
    after = capture(root, isolated_codex_home, cwd, options=['--no-root-markers'])
    data = doc(compare(tmp_path, before, after, FLAG, '--json'))
    row, = data['file_changes']
    assert row['cwd'] == 'pkg/api'
    assert row['changes'] == [] and data['directories'] == []
    assert row['context_changes']['cwd'] == {'before': 'pkg/api', 'after': '.'}
    assert row['context_changes']['root_found'] == {'before': True, 'after': False}


@pytest.mark.parametrize('bad_kind', ['single', 'mixed', 'cut', 'nested-version', 'flag-value'])
def test_invalid_detail_inputs_fail_before_stdout(tree, tmp_path, isolated_codex_home, bad_kind):
    root, cwd = tree
    put(root/'AGENTS.md', 'A\n€Z')
    before = manifest(root, isolated_codex_home, cwd)
    if bad_kind in ('single', 'mixed'):
        single = snapshot(cwd, isolated_codex_home)
        left, right = (single, single) if bad_kind == 'single' else (before, single)
    else:
        left, right = before, before
    old = save(tmp_path/'before.json', left)
    new = save(tmp_path/'after.json', right)
    if bad_kind in ('cut', 'nested-version'):
        broken = copy.deepcopy(doc(before))
        snap = broken['directories'][0]['snapshot']
        if bad_kind == 'cut':
            snap['entries'][0]['cut']['line'] = 0
        else:
            snap['schema_version'] = 1
        put(new, json.dumps(broken))
    options = [FLAG, '--json'] + (['yes'] if bad_kind == 'flag-value' else [])
    for a, b in ((old, new), (new, old)):
        result = cli('diff', a, b, *options)
        assert result.returncode == 2 and not result.stdout
        assert b'Traceback' not in result.stderr
