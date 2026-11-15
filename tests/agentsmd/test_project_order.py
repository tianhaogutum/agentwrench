"""Selected v2 project directories follow the pinned lexical root-to-cwd walk."""
import copy
import json
import shutil
from pathlib import Path

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save
from .test_snapshot_v2 import document, v2


def reverse_projects(snapshot):
    indexes = [i for i, row in enumerate(snapshot['entries'])
               if row['scope'] == 'project' and row['selected']]
    assert len(indexes) >= 2
    winners = [snapshot['entries'][i] for i in indexes]
    for index, row in zip(indexes, reversed(winners)):
        snapshot['entries'][index] = row
    for order, row in enumerate(snapshot['entries']):
        row['order'] = order


def options(mapping=None):
    flags = [[], ['--json'], ['--fail-on-loss'], ['--fail-on-loss', '--json']]
    if mapping is not None:
        flags += [['--include-file-changes', *f] for f in flags.copy()]
        flags += [['--cwd-map', mapping, *f] for f in flags.copy()]
    return flags


@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
@pytest.mark.parametrize('position', ['before', 'after', 'self'])
def test_reversed_project_order_reproducer(tree, tmp_path, isolated_codex_home, manifest, position):
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    put(cwd/'AGENTS.override.md', 'WIN')
    original = (capture(root, isolated_codex_home, cwd, options=['--snapshot-version', 2])
                if manifest else v2(cwd, isolated_codex_home))
    bad = copy.deepcopy(document(original))
    nested = bad['directories'][0]['snapshot'] if manifest else bad
    assert len(nested['entries']) == 2 and all(r['selected'] for r in nested['entries'])
    reverse_projects(nested)  # Only row positions/order indexes change; even body digest is authentic.
    good = save(tmp_path/'good.json', original)
    impossible = put(tmp_path/'impossible.json', json.dumps(bad))
    mapping = put(tmp_path/'map.json', '{"pkg/api": "pkg/api"}')
    frozen = {p: p.read_bytes() for p in (good, impossible, mapping)}
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    left, right = {'before': (impossible, good), 'after': (good, impossible),
                   'self': (impossible, impossible)}[position]
    for flags in options(mapping if manifest else None):
        result = cli('diff', left, right, *flags)
        assert result.returncode == 2, (flags, result.stdout, result.stderr)
        assert not result.stdout
        assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr
    assert all(p.read_bytes() == data for p, data in frozen.items())


@pytest.mark.parametrize('body,budget', [(b'WIN', 100), (b'', 100), (b'WIN', 0), (b' \n tail', 1)])
def test_all_winners_and_legacy_boundary_source_free(tree, isolated_codex_home, monkeypatch,
                                                    body, budget):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    put(root/'AGENTS.override.md', body)
    put(cwd/'AGENTS.override.md', body)
    modern = create_snapshot(cwd, isolated_codex_home, Settings(budget), snapshot_version=2)
    bad = copy.deepcopy(modern)
    reverse_projects(bad)
    # Equal bodies retain authentic fingerprint and all fields except order.
    assert bad['effective'] == modern['effective']
    assert [{k: v for k, v in r.items() if k != 'order'} for r in bad['entries']] == [
        {k: v for k, v in r.items() if k != 'order'} for r in reversed(modern['entries'])]
    legacy = copy.deepcopy(bad)
    legacy['schema_version'] = 1
    for row in legacy['entries']:
        row.pop('cut')
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_filesystem(*args):
        pytest.fail('historical validation consulted source filesystem')
    monkeypatch.setattr(Reader, 'read', no_filesystem)
    monkeypatch.setattr(Path, 'resolve', no_filesystem)
    monkeypatch.setattr(Path, 'stat', no_filesystem)
    frozen = copy.deepcopy((modern, bad, legacy))
    assert validate_snapshot(modern) == modern
    assert validate_snapshot(legacy) == legacy  # V1 validation remains unchanged.
    for left, right in ((modern, modern), (legacy, modern), (modern, legacy)):
        assert not diff_artifacts(left, right)['effective_changed']
    assert diff_artifacts(modern, modern, fail_on_loss=True)['loss_policy']['passed']
    for left, right in ((bad, modern), (modern, bad), (bad, bad), (bad, legacy), (legacy, bad)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right)
    assert (modern, bad, legacy) == frozen


@pytest.mark.parametrize('order', [(0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)])
def test_every_three_directory_inversion(tree, isolated_codex_home, order):
    from agentwrench.agentsmd.core import ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, validate_snapshot
    root, cwd = tree
    for directory in (root, root/'pkg', cwd):
        put(directory/'AGENTS.md', 'WIN')
    good = create_snapshot(cwd, isolated_codex_home, Settings(), snapshot_version=2)
    bad = copy.deepcopy(good)
    bad['entries'] = [bad['entries'][i] for i in order]
    for i, row in enumerate(bad['entries']):
        row['order'] = i
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    assert validate_snapshot(good) == good
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)


@pytest.mark.parametrize('present', [(), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)])
@pytest.mark.parametrize('shadow', [False, True])
def test_authentic_sparse_chains_with_optional_shadow_audits(tree, isolated_codex_home,
                                                            monkeypatch, present, shadow):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    directories = [root, root/'pkg', cwd]
    # All three precedence branches, an empty winner, lossy decoding and joins.
    candidates = ['AGENTS.override.md', 'AGENTS.md', 'RULES.md']
    bodies = [b'', b' \xff\r\n', b'C']
    put(isolated_codex_home/'AGENTS.override.md', ' G ')
    put(isolated_codex_home/'AGENTS.md', 'global shadow')
    for i in present:
        put(directories[i]/candidates[i], bodies[i])
        if i == 0:
            put(directories[i]/'AGENTS.md', 'project shadow')
    good = create_snapshot(cwd, isolated_codex_home, Settings(100, ['RULES.md']), snapshot_version=2)
    if not shadow:
        good['entries'] = [r for r in good['entries'] if not r['status'].startswith('shadowed-')]
        for i, row in enumerate(good['entries']):
            row['order'] = i
    else:
        # Optional audit failures must stay irrelevant to selected order.
        for row in good['entries']:
            if row['status'].startswith('shadowed-'):
                row.update(sha256=None, hash_error='synthetic optional audit failure', size=None)
    selected = [Path(r['path']).parent for r in good['entries']
                if r['scope'] == 'project' and r['selected']]
    assert selected == [directories[i].relative_to(root) for i in present]
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy(good)
    assert validate_snapshot(good) == good
    assert diff_artifacts(good, good, fail_on_loss=True)['loss_policy']['passed']
    assert good == frozen


@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('unpaired', [False, True])
def test_every_nested_row_before_projection(tree, tmp_path, isolated_codex_home, monkeypatch,
                                            index, position, unpaired):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_manifest, diff_artifacts
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    put(root/'pkg/AGENTS.override.md', 'WIN')
    (cwd/'deep').mkdir()
    good = create_manifest([root/'pkg', cwd, cwd/'deep'], root,
                           isolated_codex_home, Settings(), snapshot_version=2)
    bad = copy.deepcopy(good)
    reverse_projects(bad['directories'][index]['snapshot'])
    other = copy.deepcopy(good)
    if unpaired:
        other['directories'].pop(index)
    left, right = (bad, other) if position == 'before' else (other, bad)
    before = put(tmp_path/'before.json', json.dumps(left))
    after = put(tmp_path/'after.json', json.dumps(right))
    moves = {r['cwd']: r['cwd'] for r in other['directories']}
    mapping = put(tmp_path/'map.json', json.dumps(moves))
    frozen = copy.deepcopy((left, right))
    frozen_files = {p: p.read_bytes() for p in (before, after, mapping)}
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_projection_or_reads(*args):
        pytest.fail('projected or read source before validating nested snapshot')
    monkeypatch.setattr('agentwrench.agentsmd.snapshot.project_manifest', no_projection_or_reads)
    monkeypatch.setattr(Reader, 'read', no_projection_or_reads)
    for flags in ({}, dict(include_file_changes=True), dict(fail_on_loss=True),
                  dict(cwd_map=moves, include_file_changes=True, fail_on_loss=True)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right, **flags)
    for flags in options(mapping):
        result = cli('diff', before, after, *flags)
        assert result.returncode == 2 and not result.stdout, (flags, result.stderr)
        assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr
    assert (left, right) == frozen
    assert all(p.read_bytes() == data for p, data in frozen_files.items())


def test_lexical_alias_chain_and_no_root(tree, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_manifest, create_snapshot, diff_artifacts
    root, cwd = tree
    for directory in (root, root/'pkg', cwd):
        put(directory/'AGENTS.md', 'WIN')
    (root/'alias').symlink_to(root/'pkg', target_is_directory=True)
    doc = create_manifest([cwd, root/'alias/api'], root, isolated_codex_home,
                          Settings(), snapshot_version=2)
    assert [[r['path'] for r in d['snapshot']['entries']] for d in doc['directories']] == [
        ['AGENTS.md', 'pkg/AGENTS.md', 'pkg/api/AGENTS.md'],
        ['AGENTS.md', 'alias/AGENTS.md', 'alias/api/AGENTS.md']]
    no_root = create_snapshot(root/'alias/api', isolated_codex_home,
                              Settings(100, [], []), snapshot_version=2)
    assert not no_root['root_found'] and no_root['cwd'] == '.'
    assert [r['path'] for r in no_root['entries']] == ['AGENTS.md']
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_filesystem(*args):
        pytest.fail('historical validation consulted filesystem')
    monkeypatch.setattr(Reader, 'read', no_filesystem)
    monkeypatch.setattr(Path, 'resolve', no_filesystem)
    monkeypatch.setattr(Path, 'stat', no_filesystem)
    frozen = copy.deepcopy((doc, no_root))
    assert diff_artifacts(doc, doc, fail_on_loss=True)['loss_policy']['passed']
    assert diff_artifacts(no_root, no_root, fail_on_loss=True)['loss_policy']['passed']
    assert (doc, no_root) == frozen
