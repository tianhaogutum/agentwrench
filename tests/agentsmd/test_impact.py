"""Synthetic integration oracles for the multi-directory configuration comparison."""
import json
from pathlib import Path

import pytest

from .conftest import put
from .test_cli import cli


def impact_args(home, *cwds):
    return ['impact', '--before-home', home, '--after-home', home,
            *(arg for cwd in cwds for arg in ('--cwd', cwd))]


def test_budget_recovery_across_working_directories(tree, isolated_codex_home):
    root, cwd = tree
    pkg = root/'pkg'
    put(root/'AGENTS.md', 'ROOT')  # 4 bytes
    put(pkg/'AGENTS.md', '# PKG\n')  # 6 bytes, including a private heading
    put(cwd/'AGENTS.md', 'API')  # 3 bytes
    args = impact_args(isolated_codex_home, root, pkg, cwd)
    result = cli(*args, '--before-max-bytes', 6, '--after-max-bytes', 20, '--json')
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    assert report['summary'] == {'cwd_count': 3, 'changed_cwd_count': 2, 'error_count': 0}
    top, package, api = report['directories']
    assert top['changes'] == [] and not top['changed']
    assert [d['cwd'] for d in report['directories']] == list(map(str, (root, pkg, cwd)))
    for directory, expected_after in [(package, 10), (api, 13)]:
        assert directory['before']['project_bytes_used'] == 6
        assert directory['after']['project_bytes_used'] == expected_after
        change = directory['changes'][0]
        assert change['path'] == str(pkg/'AGENTS.md')
        assert change['kind'] == 'changed'
        assert change['before']['status'] == 'partial'
        assert change['before']['bytes_loaded'] == 2
        assert change['before']['cut'] == {'byte_offset': 2, 'line': 1, 'splits_utf8_character': False}
        assert change['after']['status'] == 'loaded'
        assert change['after']['bytes_loaded'] == 6 and change['after']['cut'] is None
    leaf = api['changes'][1]
    assert leaf['path'] == str(cwd/'AGENTS.md')
    assert leaf['before']['status'] == 'dropped-over-budget'
    assert leaf['before']['cut']['byte_offset'] == 0
    assert leaf['after']['status'] == 'loaded' and leaf['after']['bytes_loaded'] == 3
    # Even headings are content: the impact schema must omit all body strings.
    for secret in (b'ROOT', b'# PKG', b'API', b'heading', b'"text"'):
        assert secret not in result.stdout
    human = cli(*args, '--before-max-bytes', 6, '--after-max-bytes', 20)
    assert human.returncode == 0 and b'cut byte 2' in human.stdout
    assert b'no cut' in human.stdout and b'dropped-over-budget' in human.stdout
    assert b'# PKG' not in human.stdout


def test_configuration_changes_root_and_selected_fallback(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    after_home = tmp_path/'after-home'
    put(root/'AGENTS.md', 'EXCLUDED_ROOT')
    put(cwd/'RULES.md', 'BEFORE_RULES')
    put(cwd/'TEAM.md', 'AFTER_TEAM')
    put(isolated_codex_home/'config.toml', 'project_doc_fallback_filenames = ["RULES.md"]')
    put(after_home/'config.toml', '''project_root_markers = []
project_doc_fallback_filenames = ["TEAM.md"]
''')
    result = cli('impact', '--cwd', cwd, '--before-home', isolated_codex_home,
                 '--after-home', after_home, '--json')
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['after']['settings']['root_markers'] == []
    directory = report['directories'][0]
    assert directory['before']['root'] == str(root) and directory['after']['root'] is None
    assert [(Path(c['path']).name, c['kind']) for c in directory['changes']] == [
        ('AGENTS.md', 'removed'), ('RULES.md', 'removed'), ('TEAM.md', 'added')]
    assert directory['changes'][0]['after'] is None
    assert directory['changes'][-1]['before'] is None
    assert b'EXCLUDED_ROOT' not in result.stdout and b'BEFORE_RULES' not in result.stdout


def test_identical_configuration_is_stable_and_deduplicates_cwds(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'root')
    args = impact_args(isolated_codex_home, cwd, cwd/'..'/'api', root)
    first = cli(*args, '--json')
    second = cli(*args, '--json')
    assert first.returncode == 0 and first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report['summary'] == {'cwd_count': 2, 'changed_cwd_count': 0, 'error_count': 0}
    assert all(not d['changes'] and not d['changed'] for d in report['directories'])


def test_instruction_error_retains_other_directories_and_exits_two(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'safe')
    (cwd/'AGENTS.md').symlink_to('AGENTS.md')
    result = cli(*impact_args(isolated_codex_home, cwd, root), '--json')
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report['summary']['error_count'] == 2  # each configuration
    bad, good = report['directories']
    assert bad['before']['diagnostics'] == [{'path': str(cwd/'AGENTS.md'), 'error': 'OSError'}]
    assert bad['after']['diagnostics'] == bad['before']['diagnostics']
    assert good['after']['project_bytes_used'] == 4 and not good['after']['diagnostics']


@pytest.mark.parametrize('invalid, message', [
    ('config', b'malformed config.toml'), ('cwd', b'not a directory'),
    ('budget', b'project_doc_max_bytes must be an integer'),
    ('too-many-cwds', b'impact requires 1 to 64 --cwd arguments')])
def test_invalid_input_exits_two_without_partial_output(tree, isolated_codex_home, invalid, message):
    root, cwd = tree
    args = impact_args(isolated_codex_home, root, cwd)
    if invalid == 'config':
        put(isolated_codex_home/'config.toml', 'secret = "PRIVATE_CONFIG" invalid')
    elif invalid == 'cwd':
        args += ['--cwd', root/'missing']
    elif invalid == 'budget':
        args += ['--after-max-bytes', -1]
    else:
        args += [arg for _ in range(65) for arg in ('--cwd', root)]
    result = cli(*args, '--json')
    assert result.returncode == 2 and not result.stdout
    assert message in result.stderr
    assert b'PRIVATE_CONFIG' not in result.stderr


def test_zero_budget_and_unicode_cut(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'A€Z')  # 5 source bytes
    put(isolated_codex_home/'AGENTS.md', '# PRIVATE_GLOBAL')
    result = cli(*impact_args(isolated_codex_home, cwd), '--before-max-bytes', 0,
                 '--after-max-bytes', 2, '--json')
    assert result.returncode == 0, result.stderr
    directory = json.loads(result.stdout)['directories'][0]
    assert len(directory['changes']) == 1  # global stays outside the budget
    change = directory['changes'][0]
    assert change['before']['cut']['byte_offset'] == 0
    assert change['before']['bytes_loaded'] == 0
    assert change['after']['cut']['byte_offset'] == 2
    assert change['after']['cut']['splits_utf8_character'] is True
    assert change['after']['bytes_loaded'] == 2 and change['after']['text_bytes'] == 4
    assert b'PRIVATE_GLOBAL' not in result.stdout


def test_same_path_in_global_and_project_has_only_project_cut(tree):
    root, cwd = tree
    put(root/'AGENTS.md', 'abcde')
    result = cli(*impact_args(root, cwd), '--before-max-bytes', 2,
                 '--after-max-bytes', 10, '--json')
    assert result.returncode == 0, result.stderr
    directory = json.loads(result.stdout)['directories'][0]
    global_row, project_row = directory['before']['entries']
    assert global_row['scope'] == 'global' and global_row['cut'] is None
    assert project_row['scope'] == 'project' and project_row['cut']['byte_offset'] == 2
    assert len(directory['changes']) == 1
    assert directory['changes'][0]['scope'] == 'project'


def test_fallback_reordering_changes_selection_but_override_stays_loaded(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    after_home = tmp_path/'after-home'
    put(root/'AGENTS.override.md', 'winner')
    for folder in (root, cwd):
        put(folder/'RULES.md', 'rules')
        put(folder/'TEAM.md', 'team')
    put(isolated_codex_home/'config.toml', 'project_doc_fallback_filenames = ["RULES.md", "TEAM.md"]')
    put(after_home/'config.toml', 'project_doc_fallback_filenames = ["TEAM.md", "RULES.md"]')
    result = cli('impact', '--cwd', cwd, '--before-home', isolated_codex_home,
                 '--after-home', after_home, '--json')
    assert result.returncode == 0, result.stderr
    changes = json.loads(result.stdout)['directories'][0]['changes']
    assert all(Path(c['path']).name != 'AGENTS.override.md' for c in changes)
    leaf = [c for c in changes if Path(c['path']).parent == cwd]
    assert [(Path(c['path']).name, c['before']['status'], c['after']['status']) for c in leaf] == [
        ('RULES.md', 'loaded', 'shadowed-by-precedence'),
        ('TEAM.md', 'shadowed-by-precedence', 'loaded')]
