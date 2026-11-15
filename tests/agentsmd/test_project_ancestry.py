"""Selected v2 project directories must belong to lexical discovery ancestry."""
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


@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
@pytest.mark.parametrize('position', ['before', 'after', 'self'])
def test_outside_ancestry_reproducer(tree, tmp_path, isolated_codex_home, manifest, position):
    root, _ = tree
    put(root/'AGENTS.override.md', 'WIN')
    original = (capture(root, isolated_codex_home, root, options=['--snapshot-version', 2])
                if manifest else v2(root, isolated_codex_home))
    bad = copy.deepcopy(document(original))
    nested = bad['directories'][0]['snapshot'] if manifest else bad
    winner, = nested['entries']
    assert nested['cwd'] == '.' and winner['selected']
    winner['path'] = 'unsearched/AGENTS.override.md'  # Change nothing else.
    good = save(tmp_path/'good.json', original)
    impossible = put(tmp_path/'impossible.json', json.dumps(bad))
    mapping = put(tmp_path/'map.json', json.dumps({'.': '.'}))
    frozen = {p: p.read_bytes() for p in (good, impossible, mapping)}
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    left, right = {'before': (impossible, good), 'after': (good, impossible),
                   'self': (impossible, impossible)}[position]
    options = [[], ['--json'], ['--fail-on-loss'], ['--fail-on-loss', '--json']]
    if manifest:
        options += [['--include-file-changes', *flags] for flags in options.copy()]
        options += [['--cwd-map', mapping, *flags] for flags in options.copy()]
    for flags in options:
        result = cli('diff', left, right, *flags)
        assert result.returncode == 2, (flags, result.stdout, result.stderr)
        assert not result.stdout
        assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr
    assert all(p.read_bytes() == data for p, data in frozen.items())


@pytest.mark.parametrize('path', ['pkg/api/child/AGENTS.md', 'pkg/sibling/AGENTS.md',
                                   'pkg/ap/AGENTS.md', 'pkg/api-suffix/AGENTS.md',
                                   'other/AGENTS.md'])
@pytest.mark.parametrize('body,budget', [(b'WIN', 100), (b'', 100), (b'WIN', 0), (b'WIN', 1)])
def test_non_ancestor_parents_all_selected_statuses(tree, isolated_codex_home, monkeypatch,
                                                   path, body, budget):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, validate_snapshot
    root, cwd = tree
    put(cwd/'AGENTS.md', body)
    good = create_snapshot(cwd, isolated_codex_home, Settings(budget), snapshot_version=2)
    bad = copy.deepcopy(good)
    bad['entries'][0]['path'] = path
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy((good, bad))
    assert validate_snapshot(good) == good
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)
    assert (good, bad) == frozen


@pytest.mark.parametrize('position', ['before', 'after'])
def test_unpaired_outside_ancestry_before_projection(tree, tmp_path, isolated_codex_home,
                                                    monkeypatch, position):
    from agentwrench.agentsmd.core import Reader, ScopeError
    from agentwrench.agentsmd.snapshot import diff_artifacts
    root, cwd = tree
    put(cwd/'AGENTS.md', 'C')
    bad = document(capture(root, isolated_codex_home, root, cwd, options=['--snapshot-version', 2]))
    other = copy.deepcopy(bad)
    other['directories'].pop()
    bad['directories'][-1]['snapshot']['entries'][0]['path'] = 'unsearched/AGENTS.md'
    frozen = copy.deepcopy((bad, other))
    left, right = (bad, other) if position == 'before' else (other, bad)
    before, after = (put(tmp_path/f'{name}.json', json.dumps(doc))
                     for name, doc in [('before', left), ('after', right)])
    mapping = put(tmp_path/'map.json', '{".": "."}')
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_projection_or_reads(*args):
        pytest.fail('projected or read source before validating unpaired snapshot')
    monkeypatch.setattr('agentwrench.agentsmd.snapshot.project_manifest', no_projection_or_reads)
    monkeypatch.setattr(Reader, 'read', no_projection_or_reads)
    for flags in ({}, dict(include_file_changes=True), dict(fail_on_loss=True),
                  dict(cwd_map={'.': '.'}, include_file_changes=True, fail_on_loss=True)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right, **flags)
    for flags in ([], ['--json'], ['--include-file-changes', '--json'], ['--fail-on-loss'],
                  ['--cwd-map', mapping, '--include-file-changes', '--fail-on-loss', '--json']):
        result = cli('diff', before, after, *flags)
        assert result.returncode == 2 and not result.stdout, result.stderr
        assert b'invalid snapshot' in result.stderr
    assert (bad, other) == frozen


@pytest.mark.parametrize('root_found', [True, False])
def test_authentic_chain_and_no_root_source_free(tree, isolated_codex_home, monkeypatch, root_found):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    put(root/'AGENTS.override.md', '')
    put(root/'AGENTS.md', 'shadow')
    put(root/'pkg'/'AGENTS.md', '\xff')
    put(cwd/'RULES.md', 'C')
    settings = Settings(100, ['RULES.md'], ['.git'] if root_found else [])
    modern = create_snapshot(cwd, isolated_codex_home, settings, snapshot_version=2)
    legacy = create_snapshot(cwd, isolated_codex_home, settings, snapshot_version=1)
    selected = [r['path'] for r in modern['entries'] if r['selected']]
    assert selected == (['AGENTS.override.md', 'pkg/AGENTS.md', 'pkg/api/RULES.md']
                        if root_found else ['RULES.md'])
    assert modern['cwd'] == ('pkg/api' if root_found else '.')
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy((modern, legacy))
    assert validate_snapshot(modern) == modern
    for before, after in ((modern, modern), (legacy, modern), (modern, legacy)):
        result = diff_artifacts(before, after)
        assert result['schema_version'] == 1 and not result['effective_changed'] and not result['changes']
    assert diff_artifacts(modern, modern, fail_on_loss=True)['loss_policy']['passed']
    assert (modern, legacy) == frozen


def test_lexical_symlink_aliases_and_legacy_boundary(tree, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_manifest, diff_artifacts, validate_snapshot
    root, cwd = tree
    put(cwd/'AGENTS.md', 'C')
    (root/'alias').symlink_to(root/'pkg', target_is_directory=True)
    doc = create_manifest([cwd, root/'alias/api'], root, isolated_codex_home, Settings(), snapshot_version=2)
    assert [r['cwd'] for r in doc['directories']] == ['pkg/api', 'alias/api']
    assert [r['snapshot']['entries'][0]['path'] for r in doc['directories']] == [
        'pkg/api/AGENTS.md', 'alias/api/AGENTS.md']
    bad = copy.deepcopy(doc['directories'][1]['snapshot'])
    bad['entries'][0]['path'] = 'pkg/api/AGENTS.md'  # Same physical file, wrong lexical chain.
    legacy = copy.deepcopy(bad)
    legacy['schema_version'] = 1
    for row in legacy['entries']:
        row.pop('cut')
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_filesystem(*args):
        pytest.fail('historical validation consulted filesystem')
    monkeypatch.setattr(Reader, 'read', no_filesystem)
    monkeypatch.setattr(Path, 'resolve', no_filesystem)
    monkeypatch.setattr(Path, 'stat', no_filesystem)
    frozen = copy.deepcopy((doc, bad, legacy))
    assert validate_snapshot(legacy) == legacy
    assert not diff_artifacts(doc, doc)['effective_changed']
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)
    assert (doc, bad, legacy) == frozen


@pytest.mark.parametrize('body,budget', [(b'WIN', 100), (b'', 100), (b'WIN', 0), (b'WIN', 1)])
def test_no_root_searches_only_cwd(tree, isolated_codex_home, body, budget):
    from agentwrench.agentsmd.core import ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, validate_snapshot
    root, cwd = tree
    put(root/'AGENTS.md', body)
    rooted = create_snapshot(cwd, isolated_codex_home, Settings(budget), snapshot_version=2)
    # A root-level winner is in the chain only when a discovery root exists.
    bad = copy.deepcopy(rooted)
    bad['root_found'] = False
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)
    put(cwd/'AGENTS.md', body)
    no_root = create_snapshot(cwd, isolated_codex_home, Settings(budget, [], []), snapshot_version=2)
    assert no_root['cwd'] == '.' and [r['path'] for r in no_root['entries']] == ['AGENTS.md']
    bad = copy.deepcopy(no_root)
    bad['entries'][0]['path'] = 'child/AGENTS.md'
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    assert validate_snapshot(no_root) == no_root
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)
