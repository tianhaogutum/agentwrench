"""Baseline-relative loss gate: improvements never offset another file's loss."""
import json

import pytest

from .conftest import put
from .test_cli import cli
from .test_impact import impact_args


@pytest.mark.parametrize('before, after, code, counts', [
    (6, 6, 0, [(0, 0), (0, 0), (0, 0)]),  # unchanged existing loss
    (6, 20, 0, [(0, 0), (0, 0), (0, 0)]),  # improved partial and dropped files
    (20, 6, 1, [(0, 0), (4, 0), (7, 1)]),
    (8, 6, 1, [(0, 0), (2, 0), (2, 0)]),  # worsened cut, already dropped leaf
    (6, 0, 1, [(4, 1), (6, 2), (6, 2)]),
])
def test_loss_policy_across_cwds(tree, isolated_codex_home, before, after, code, counts):
    root, cwd = tree
    pkg = root/'pkg'
    put(root/'AGENTS.md', 'ROOT')
    put(pkg/'AGENTS.md', '# PKG\n')
    put(cwd/'AGENTS.md', 'API')
    args = impact_args(isolated_codex_home, root, pkg, cwd, cwd/'..'/'api')
    args += ['--before-max-bytes', before, '--after-max-bytes', after]
    # Analysis mode remains non-gating and its schema is unchanged.
    analysis = cli(*args, '--json')
    assert analysis.returncode == 0
    assert 'loss_policy' not in json.loads(analysis.stdout)
    result = cli(*args, '--fail-on-loss', '--json')
    assert result.returncode == code, result.stderr
    report = json.loads(result.stdout)
    assert report['summary']['cwd_count'] == 3
    assert report['loss_policy'] == dict(
        name='no-new-loss', passed=code == 0,
        regressed_cwd_count=sum(b > 0 for b, _ in counts),
        newly_lost_bytes=sum(b for b, _ in counts), newly_lost_files=sum(f for _, f in counts))
    assert [(d['loss']['newly_lost_bytes'], d['loss']['newly_lost_files'])
            for d in report['directories']] == counts
    human = cli(*args, '--fail-on-loss')
    assert human.returncode == code
    assert (b'PASS: no-new-loss' if code == 0 else b'FAIL: no-new-loss') in human.stdout
    for secret in (b'ROOT', b'# PKG', b'API', b'heading', b'"text"'):
        assert secret not in result.stdout and secret not in human.stdout


def homes_args(before, after, *cwds):
    return ['impact', '--before-home', before, '--after-home', after,
            *(arg for cwd in cwds for arg in ('--cwd', cwd)), '--fail-on-loss', '--json']


@pytest.mark.parametrize('kind', ['selection', 'global-trimming', 'unicode-decoding', 'new-zero-budget'])
def test_identical_effective_bytes_override_metadata_loss(tree, tmp_path, isolated_codex_home, kind):
    root, cwd = tree
    after = tmp_path/'after'
    if kind == 'selection':
        put(root/'AGENTS.md', 'same')
        put(cwd/'TEAM.md', 'same')
        put(after/'config.toml', 'project_root_markers=[]\nproject_doc_fallback_filenames=["TEAM.md"]')
    elif kind == 'global-trimming':
        put(isolated_codex_home/'AGENTS.override.md', '  same  ')
        put(after/'AGENTS.md', 'same')
    elif kind == 'unicode-decoding':
        # Both cuts decode to one replacement character although fewer source
        # bytes are accepted on the proposed side.
        put(root/'AGENTS.md', '€')
        put(isolated_codex_home/'config.toml', 'project_doc_max_bytes=2')
        put(after/'config.toml', 'project_doc_max_bytes=1')
    else:
        put(root/'TEAM.md', 'unseen')
        put(after/'config.toml', 'project_doc_max_bytes=0\nproject_doc_fallback_filenames=["TEAM.md"]')
    result = cli(*homes_args(isolated_codex_home, after, cwd))
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc['directories'][0]['changed']
    assert doc['directories'][0]['loss'] == dict(
        effective_changed=False, newly_lost_bytes=0, newly_lost_files=0, regressions=[])
    assert doc['loss_policy']['passed'] is True


def test_removed_files_fail_despite_larger_replacement_and_other_passing_cwd(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    after = tmp_path/'after'
    put(root/'AGENTS.md', 'old')
    put(cwd/'TEAM.md', 'LARGER_REPLACEMENT')
    put(after/'config.toml', 'project_root_markers=[]\nproject_doc_fallback_filenames=["TEAM.md"]')
    result = cli(*homes_args(isolated_codex_home, after, cwd, root))
    assert result.returncode == 1, result.stderr
    doc = json.loads(result.stdout)
    bad, good = doc['directories']
    assert bad['after']['project_bytes_used'] > bad['before']['project_bytes_used']
    assert bad['loss']['regressions'] == [dict(
        scope='project', path=str(root/'AGENTS.md'), newly_lost_bytes=3, newly_lost_file=True)]
    assert good['loss']['newly_lost_bytes'] == 0
    assert doc['loss_policy']['regressed_cwd_count'] == 1


@pytest.mark.parametrize('after_budget, expected_files, expected_bytes', [(2, 0, 4), (0, 1, 6)])
def test_newly_selected_truncated_file_is_new_loss(tree, tmp_path, isolated_codex_home,
                                                  after_budget, expected_files, expected_bytes):
    root, cwd = tree
    after = tmp_path/'after'
    put(root/'TEAM.md', 'SECRET')
    # Keep the effective bodies different even at zero project budget. Otherwise
    # the identical-byte exemption correctly takes precedence over new cuts.
    put(isolated_codex_home/'AGENTS.md', 'GLOBAL_A')
    put(after/'AGENTS.md', 'GLOBAL_B')
    put(after/'config.toml', 'project_doc_fallback_filenames=["TEAM.md"]')
    result = cli(*homes_args(isolated_codex_home, after, cwd), '--after-max-bytes', after_budget)
    assert result.returncode == 1, result.stderr
    loss = json.loads(result.stdout)['loss_policy']
    assert loss['newly_lost_bytes'] == expected_bytes and loss['newly_lost_files'] == expected_files
    assert b'SECRET' not in result.stdout


@pytest.mark.parametrize('global_after, expected_code, expected_bytes', [('LONGER', 0, 0), ('XY', 1, 2)])
def test_global_identity_is_home_relative_and_counts_trimmed_bytes(tree, tmp_path, isolated_codex_home,
                                                                  global_after, expected_code, expected_bytes):
    root, cwd = tree
    after = tmp_path/'after'
    put(isolated_codex_home/'AGENTS.md', '   ABCD   ')
    put(after/'AGENTS.md', global_after)
    result = cli(*homes_args(isolated_codex_home, after, root, cwd))
    assert result.returncode == expected_code, result.stderr
    doc = json.loads(result.stdout)
    assert doc['loss_policy']['newly_lost_bytes'] == 2 * expected_bytes
    for directory in doc['directories']:
        assert directory['loss']['effective_changed'] is True
        if expected_bytes:
            assert directory['loss']['regressions'] == [dict(
                scope='global', path='AGENTS.md', newly_lost_bytes=2, newly_lost_file=False)]


def test_file_errors_override_loss_and_keep_other_directory_results(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'safe')
    (cwd/'AGENTS.md').symlink_to('AGENTS.md')
    result = cli(*impact_args(isolated_codex_home, cwd, root), '--before-max-bytes', 4,
                 '--after-max-bytes', 2, '--fail-on-loss', '--json')
    assert result.returncode == 2
    doc = json.loads(result.stdout)
    assert doc['directories'][0]['loss'] is None
    assert doc['directories'][1]['loss']['newly_lost_bytes'] == 2
    assert doc['loss_policy']['passed'] is None
    assert doc['loss_policy']['regressed_cwd_count'] == 1


def test_cwd_improvement_cannot_cancel_another_cwd_regression(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    after = tmp_path/'after'
    put(root/'AGENTS.md', 'ROOT')
    put(cwd/'TEAM.md', 'LEAF_GAIN')
    put(after/'config.toml', 'project_root_markers=[]\nproject_doc_fallback_filenames=["TEAM.md"]')
    # At api, one old file is lost despite a larger replacement. A separate
    # project gains instructions without losing any baseline file.
    extra = tmp_path/'extra'
    put(extra/'.git', '')
    put(extra/'pkg/TEAM.md', 'IMPROVEMENT')
    result = cli(*homes_args(isolated_codex_home, after, extra/'pkg', cwd, root))
    assert result.returncode == 1, result.stderr
    doc = json.loads(result.stdout)
    assert doc['directories'][0]['after']['project_bytes_used'] > doc['directories'][0]['before']['project_bytes_used']
    assert doc['directories'][0]['loss']['newly_lost_bytes'] == 0
    assert doc['loss_policy']['regressed_cwd_count'] == 1
    assert doc['loss_policy']['newly_lost_bytes'] == 4


def test_global_and_project_same_path_are_separate_loss_identities(tree):
    root, cwd = tree
    put(root/'AGENTS.md', 'A€Z')
    result = cli(*impact_args(root, cwd), '--before-max-bytes', 5,
                 '--after-max-bytes', 2, '--fail-on-loss', '--json')
    assert result.returncode == 1, result.stderr
    loss = json.loads(result.stdout)['directories'][0]['loss']
    assert loss['regressions'] == [dict(scope='project', path=str(root/'AGENTS.md'),
                                      newly_lost_bytes=3, newly_lost_file=False)]


@pytest.mark.parametrize('invalid, message', [
    ('config', b'malformed config.toml'), ('cwd', b'not a directory'),
    ('budget', b'project_doc_max_bytes must be an integer'),
    ('too-many-cwds', b'impact requires 1 to 64 --cwd arguments'),
    ('flag-value', b'unrecognized arguments')])
def test_invalid_policy_input_exits_two_without_partial_report(tree, isolated_codex_home, invalid, message):
    root, cwd = tree
    args = impact_args(isolated_codex_home, root, cwd) + ['--fail-on-loss', '--json']
    if invalid == 'config':
        put(isolated_codex_home/'config.toml', 'secret="PRIVATE_CONFIG" broken')
    elif invalid == 'cwd':
        args += ['--cwd', root/'missing']
    elif invalid == 'budget':
        args += ['--after-max-bytes', -1]
    elif invalid == 'too-many-cwds':
        args += [arg for _ in range(65) for arg in ('--cwd', root)]
    else:
        args += ['true']
    result = cli(*args)
    assert result.returncode == 2 and not result.stdout
    assert message in result.stderr and b'PRIVATE_CONFIG' not in result.stderr
