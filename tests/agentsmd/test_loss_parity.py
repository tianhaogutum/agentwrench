"""Live and historical no-new-loss gates use the same raw-prefix boundary."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_impact_policy import homes_args
from .test_manifest import capture
from .test_snapshot import save


@pytest.mark.parametrize('text,budgets,global_after,lost,files', [
    (' \n tail', (3, 1), 'B', 2, 0),  # retained=0 on both sides; excluded=4 -> 6
    (' \n tail', (1, 3), 'B', 0, 0),
    (' \n tail', (3, 3), 'B', 0, 0),
    (' \n tail', (3, 1), 'A', 0, 0),  # equal effective bytes suppress raw loss
    (' \n tail', (3, 4), 'B', 0, 0),  # skipped-empty -> partial
    (' \n tail', (4, 3), 'B', 4, 1),  # partial -> skipped-empty: max, not sum
    ('\u2003tail', (3, 1), 'B', 2, 0),  # whitespace -> split UTF-8 replacement
    ('\u2003tail', (3, 0), 'B', 3, 0),  # skipped-empty already had exclusion
    ('A\n€Z', (6, 3), 'B', 3, 0),
    ('A\n€Z', (3, 0), 'B', 3, 1),
    ('€', (2, 1), 'A', 0, 0),  # different raw cuts, same replacement character
])
def test_live_saved_raw_prefix_parity(tree, tmp_path, isolated_codex_home,
                                     text, budgets, global_after, lost, files):
    root, cwd = tree
    after_home = tmp_path/'after-home'
    put(root/'AGENTS.md', text)
    put(isolated_codex_home/'AGENTS.md', 'A')
    put(after_home/'AGENTS.md', global_after)
    paths = []
    for name, home, budget in zip(('before', 'after'), (isolated_codex_home, after_home), budgets):
        paths.append(save(tmp_path/(name+'.json'), capture(
            root, home, cwd, root, options=['--snapshot-version', 2, '--max-bytes', budget])))
    args = homes_args(isolated_codex_home, after_home, cwd, root)
    args += ['--before-max-bytes', budgets[0], '--after-max-bytes', budgets[1]]
    live = cli(*args)
    assert live.returncode == int(lost > 0), live.stderr
    data = json.loads(live.stdout)
    assert data['loss_policy'] == dict(name='no-new-loss', passed=not lost,
        regressed_cwd_count=2 if lost else 0, newly_lost_bytes=2*lost, newly_lost_files=2*files)
    for row in data['directories']:
        assert row['loss']['regressions'] == ([dict(scope='project', path=str(root/'AGENTS.md'),
            newly_lost_bytes=lost, newly_lost_file=bool(files))] if lost else [])
    # Compare exact default structure as well as default analysis exit behavior.
    default_args = [arg for arg in args if arg != '--fail-on-loss']
    default = cli(*default_args)
    assert default.returncode == 0
    expected = copy.deepcopy(data)
    del expected['loss_policy']
    for row in expected['directories']:
        del row['loss']
    assert json.loads(default.stdout) == expected
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    shutil.rmtree(after_home)
    historical = cli('diff', *paths, '--fail-on-loss', '--json')
    assert historical.returncode == live.returncode
    saved = json.loads(historical.stdout)
    assert saved['loss_policy'] == data['loss_policy']
    for old, new in zip(data['directories'], saved['losses']):
        loss = {**old['loss'], 'regressions': [dict(r, path='AGENTS.md')
                                             for r in old['loss']['regressions']]}
        assert new['loss'] == loss
    assert str(tmp_path).encode() not in historical.stdout
    assert b'heading' not in live.stdout + historical.stdout


def test_whitespace_loss_cannot_net_against_new_leaf_retention(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', ' \n tail')
    put(cwd/'TEAM.md', 'X')
    put(isolated_codex_home/'AGENTS.md', 'A')
    after_home = tmp_path/'after-home'
    put(after_home/'AGENTS.md', 'B')
    put(after_home/'config.toml', 'project_doc_fallback_filenames=["TEAM.md"]')
    result = cli(*homes_args(isolated_codex_home, after_home, root, cwd),
                 '--before-max-bytes', 3, '--after-max-bytes', 1)
    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    assert data['loss_policy']['newly_lost_bytes'] == 4
    assert data['loss_policy']['regressed_cwd_count'] == 2
    assert data['directories'][1]['before']['project_bytes_used'] == 0
    assert data['directories'][1]['after']['project_bytes_used'] == 1
    for row in data['directories']:
        assert row['loss']['regressions'] == [dict(scope='project', path=str(root/'AGENTS.md'),
            newly_lost_bytes=2, newly_lost_file=False)]
