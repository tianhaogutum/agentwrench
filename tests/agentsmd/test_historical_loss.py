"""Historical v2 retention policy: saved bytes, explicit baseline, no netting."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save, snapshot


FLAG = '--fail-on-loss'


def capture_v2(root, home, budget=3, cwds=None, version=2):
    options = ['--snapshot-version', version, '--max-bytes', budget]
    return (snapshot(root, home, *options) if cwds is None else
            capture(root, home, *cwds, options=options))


def compare(tmp_path, before, after, code=0, *options):
    old, new = save(tmp_path/'before.json', before), save(tmp_path/'after.json', after)
    result = cli('diff', old, new, FLAG, '--json', *options)
    assert result.returncode == code, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('manifest', [False, True])
@pytest.mark.parametrize('budgets,lost,files', [((3, 3), 0, 0), ((3, 6), 0, 0),
                                             ((6, 3), 3, 0), ((6, 0), 6, 1),
                                             ((0, 0), 0, 0), ((0, 6), 0, 0)])
def test_cut_policy_source_free(tree, tmp_path, isolated_codex_home, manifest, budgets, lost, files):
    root, cwd = tree
    put(root/'AGENTS.md', 'A\n€Z')
    cwds = [cwd, root] if manifest else None
    before = capture_v2(root, isolated_codex_home, budgets[0], cwds)
    after = capture_v2(root, isolated_codex_home, budgets[1], cwds)
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    data = compare(tmp_path, before, after, int(lost > 0))
    count = 2 if manifest else 1
    assert data['schema_version'] == (3 if manifest else 2)
    assert data['loss_policy'] == dict(name='no-new-loss', passed=lost == 0,
        regressed_cwd_count=count if lost else 0, newly_lost_bytes=count*lost, newly_lost_files=count*files)
    losses = [r['loss'] for r in data['losses']] if manifest else [data['loss']]
    if manifest:
        assert [r['cwd'] for r in data['losses']] == ['pkg/api', '.']
    for loss in losses:
        assert loss['newly_lost_bytes'] == lost and loss['newly_lost_files'] == files
        assert loss['regressions'] == ([dict(scope='project', path='AGENTS.md', newly_lost_bytes=lost,
                                             newly_lost_file=bool(files))] if lost else [])
    human = cli('diff', tmp_path/'before.json', tmp_path/'after.json', FLAG)
    assert human.returncode == int(lost > 0)
    assert (b'FAIL: no-new-loss' if lost else b'PASS: no-new-loss') in human.stdout
    assert b'heading' not in human.stdout and str(tmp_path).encode() not in human.stdout


def test_effective_edit_without_loss_and_default_compatibility(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'ABCtail')
    put(root/'AGENTS.override.md', 'XYZtail')
    before = capture_v2(cwd, isolated_codex_home)
    put(root/'AGENTS.override.md', 'UVWtail')
    after = capture_v2(cwd, isolated_codex_home)
    data = compare(tmp_path, before, after)
    assert data['effective_changed'] and data['loss']['regressions'] == []
    default = cli('diff', tmp_path/'before.json', tmp_path/'after.json', '--json')
    assert default.returncode == 1
    legacy = json.loads(default.stdout)
    assert legacy == {**{k: v for k, v in data.items() if k not in ('loss', 'loss_policy')}, 'schema_version': 1}


def test_equal_effective_suppression_tail_growth_and_global_trim(tree, tmp_path, isolated_codex_home):
    root, _ = tree
    put(root/'AGENTS.md', 'ABCtail')
    put(isolated_codex_home/'AGENTS.md', ' GLOBAL ')
    before = capture_v2(root, isolated_codex_home)
    put(root/'AGENTS.md', 'ABCmuchlongertail')
    put(isolated_codex_home/'AGENTS.md', '\nGLOBAL\n')
    after = capture_v2(root, isolated_codex_home)
    data = compare(tmp_path, before, after)
    assert not data['effective_changed'] and data['loss']['regressions'] == []
    # A changed global body enables per-file accounting; excluded growth is loss.
    put(isolated_codex_home/'AGENTS.md', ' CHANGE ')
    changed = capture_v2(root, isolated_codex_home)
    data = compare(tmp_path, before, changed, 1)
    assert data['loss']['regressions'] == [dict(scope='project', path='AGENTS.md',
                                               newly_lost_bytes=10, newly_lost_file=False)]


def test_whitespace_prefix_exclusion_uses_raw_cut_boundary(tree, tmp_path, isolated_codex_home):
    root, _ = tree
    put(root/'AGENTS.md', ' \n tail')
    put(isolated_codex_home/'AGENTS.md', 'A')
    before = capture_v2(root, isolated_codex_home, 3)
    put(isolated_codex_home/'AGENTS.md', 'B')
    after = capture_v2(root, isolated_codex_home, 1)
    data = compare(tmp_path, before, after, 1)
    assert data['loss']['regressions'] == [dict(scope='project', path='AGENTS.md',
                                               newly_lost_bytes=2, newly_lost_file=False)]


def test_selected_replacement_loses_baseline_and_shadow_gain_cannot_cancel(tree, tmp_path, isolated_codex_home):
    root, _ = tree
    put(root/'AGENTS.md', 'BASE')
    before = capture_v2(root, isolated_codex_home, 100)
    put(root/'AGENTS.override.md', 'LONGER REPLACEMENT')
    after = capture_v2(root, isolated_codex_home, 100)
    data = compare(tmp_path, before, after, 1)
    assert data['loss']['regressions'] == [dict(scope='project', path='AGENTS.md',
                                               newly_lost_bytes=4, newly_lost_file=True)]


def test_manifest_no_netting_reorder_and_detail_composition(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'AB')
    put(cwd/'AGENTS.md', 'LONGER')
    before = capture_v2(root, isolated_codex_home, 4, [cwd, root])
    put(root/'AGENTS.md', 'A')  # root loses one, leaf gains one: no aggregate netting
    after = capture_v2(root, isolated_codex_home, 4, [root, cwd])
    data = compare(tmp_path, before, after, 1, '--include-file-changes')
    assert data['loss_policy']['newly_lost_bytes'] == 2
    assert data['loss_policy']['regressed_cwd_count'] == 2
    assert [r['cwd'] for r in data['losses']] == ['pkg/api', '.']
    assert len(data['file_changes']) == 2 and data['summary']['changed_cwd_count'] == 2
    for row in data['losses']:
        assert row['loss']['regressions'] == [dict(scope='project', path='AGENTS.md',
                                                  newly_lost_bytes=1, newly_lost_file=False)]
    identical = compare(tmp_path, after, after, 0, '--include-file-changes')
    assert identical['directories'] == identical['file_changes'] == []
    assert len(identical['losses']) == 2 and identical['loss_policy']['passed']


def test_shared_global_project_path_separate_retention(tree, tmp_path):
    root, _ = tree
    put(root/'AGENTS.md', 'ABCD')
    before = capture_v2(root, root, 2)
    put(root/'AGENTS.md', 'AB')
    after = capture_v2(root, root, 2)
    data = compare(tmp_path, before, after, 1)
    assert data['loss']['regressions'] == [dict(scope='global', path='AGENTS.md',
                                               newly_lost_bytes=2, newly_lost_file=False)]


@pytest.mark.parametrize('bad_kind', ['v1', 'cross-version', 'mixed', 'membership', 'coordinate',
                                     'root-found', 'cut', 'version', 'flag-value', 'single-detail'])
def test_invalid_policy_baselines_empty_stdout(tree, tmp_path, isolated_codex_home, bad_kind):
    root, cwd = tree
    put(root/'AGENTS.md', 'ABCtail')
    before = capture_v2(root, isolated_codex_home, cwds=[root, cwd])
    after = before
    options = []
    if bad_kind in ('v1', 'cross-version'):
        after = capture_v2(root, isolated_codex_home, cwds=[root, cwd], version=1)
        if bad_kind == 'v1':
            before = after
    elif bad_kind in ('mixed', 'single-detail'):
        after = capture_v2(root, isolated_codex_home)
        if bad_kind == 'single-detail':
            before = after
            options = ['--include-file-changes']
    elif bad_kind == 'membership':
        after = capture_v2(root, isolated_codex_home, cwds=[cwd])
    old, new = save(tmp_path/'before.json', before), save(tmp_path/'after.json', after)
    if bad_kind in ('coordinate', 'root-found', 'cut', 'version'):
        broken = copy.deepcopy(json.loads(after.stdout))
        snap = broken['directories'][-1]['snapshot']
        if bad_kind == 'coordinate':
            snap['cwd'] = 'other'
        elif bad_kind == 'root-found':
            snap['root_found'] = False
        elif bad_kind == 'cut':
            snap['entries'][0]['cut']['byte_offset'] = 100
        else:
            broken['schema_version'] = 3
        put(new, json.dumps(broken))
    if bad_kind == 'flag-value':
        options = ['yes']
    for a, b in ((old, new), (new, old)):
        result = cli('diff', a, b, FLAG, '--json', *options)
        assert result.returncode == 2 and result.stdout == b''
        assert b'Traceback' not in result.stderr


@pytest.mark.parametrize('case,lost,file_lost', [('removed', 3, True), ('new-drop', 4, True),
                                               ('new-cut', 2, False), ('max-not-sum', 2, False)])
def test_removed_and_new_candidates_and_nonadditive_loss(tree, tmp_path, isolated_codex_home, case, lost, file_lost):
    root, _ = tree
    put(isolated_codex_home/'AGENTS.md', 'A')
    if case in ('removed', 'max-not-sum'):
        put(root/'AGENTS.md', 'ABCDEF')
    before = capture_v2(root, isolated_codex_home, 3)
    put(isolated_codex_home/'AGENTS.md', 'B')
    if case == 'removed':
        (root/'AGENTS.md').unlink()
    else:
        put(root/'AGENTS.md', 'WXYZ' if case == 'new-drop' else 'WXYZQ' if case == 'new-cut' else 'UVWXYZ')
    after = capture_v2(root, isolated_codex_home, 0 if case == 'new-drop' else 1 if case == 'max-not-sum' else 3)
    data = compare(tmp_path, before, after, 1)
    assert data['loss']['regressions'] == [dict(scope='project', path='AGENTS.md',
                                               newly_lost_bytes=lost, newly_lost_file=file_lost)]


def test_gains_in_other_cwd_do_not_net_out_loss(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'BASE')
    put(cwd/'AGENTS.override.md', 'X')
    # cwd's own root isolates the two files but leaves manifest identities stable.
    put(cwd/'.git', 'gitdir: synthetic')
    before = capture_v2(root, isolated_codex_home, 100, [root, cwd])
    put(root/'AGENTS.md', 'B')
    put(cwd/'AGENTS.override.md', 'EXTRA GAIN')
    after = capture_v2(root, isolated_codex_home, 100, [cwd, root])
    data = compare(tmp_path, before, after, 1)
    assert data['loss_policy']['regressed_cwd_count'] == 1
    assert data['loss_policy']['newly_lost_bytes'] == 3
    assert data['losses'][0]['loss']['newly_lost_bytes'] == 3
    assert data['losses'][1]['loss']['regressions'] == []
