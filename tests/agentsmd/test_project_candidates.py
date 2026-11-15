"""Selected v2 project basenames must belong to saved discovery candidates."""
import copy
import json
import shutil
from pathlib import Path

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_project_order import options
from .test_snapshot import save
from .test_snapshot_v2 import document, v2


def undiscoverable_copy(original):
    bad = copy.deepcopy(original)
    nested = bad['directories'][0]['snapshot'] if 'directories' in bad else bad
    row, = [r for r in nested['entries'] if r['scope'] == 'project' and r['selected']]
    assert nested['settings']['fallback'] == []
    row['path'] = (Path(row['path']).parent/'NOT_CONFIGURED.md').as_posix()
    return bad


@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
@pytest.mark.parametrize('position', ['before', 'after', 'self'])
def test_undiscoverable_project_reproducer(tree, tmp_path, isolated_codex_home, manifest, position):
    root, _ = tree
    put(root/'AGENTS.override.md', 'WIN')
    original = (capture(root, isolated_codex_home, root, options=['--snapshot-version', 2])
                if manifest else v2(root, isolated_codex_home))
    bad = undiscoverable_copy(document(original))  # Only selected path changes.
    good = save(tmp_path/'good.json', original)
    impossible = put(tmp_path/'impossible.json', json.dumps(bad))
    mapping = put(tmp_path/'map.json', '{".": "."}')
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


@pytest.mark.parametrize('body,budget', [(b'WIN', 100), (b'', 100), (b' \n', 100),
    (b'WIN', 0), (b'', 0), (b' \n tail', 1), (b'\xffWIN', 1)])
def test_all_winner_states_and_v1_boundary_source_free(tree, isolated_codex_home, monkeypatch,
                                                       body, budget):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, _ = tree
    put(root/'AGENTS.override.md', body)
    good = create_snapshot(root, isolated_codex_home, Settings(budget), snapshot_version=2)
    bad = undiscoverable_copy(good)
    assert bad['effective'] == good['effective']
    assert {k: v for k, v in bad['entries'][0].items() if k != 'path'} == {
        k: v for k, v in good['entries'][0].items() if k != 'path'}
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
    frozen = copy.deepcopy((good, bad, legacy))
    assert validate_snapshot(good) == good
    assert validate_snapshot(legacy) == legacy  # V1 filename validation unchanged.
    for left, right in ((good, good), (legacy, good), (good, legacy)):
        assert not diff_artifacts(left, right)['effective_changed']
    assert diff_artifacts(good, good, fail_on_loss=True)['loss_policy']['passed']
    for left, right in ((bad, good), (good, bad), (bad, bad), (legacy, bad), (bad, legacy)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right)
    assert (good, bad, legacy) == frozen


@pytest.mark.parametrize('name', ['AGENTS.override.md', 'AGENTS.md', 'RULES.md',
    'team:rules.md', r'team\rules.md', 'équipe.md'])
@pytest.mark.parametrize('audits', ['present', 'omitted', 'hash-error'])
def test_authentic_candidates_normalized_config_and_optional_audits(tree, isolated_codex_home,
                                                                   monkeypatch, name, audits):
    from agentwrench.agentsmd.core import Reader, load_settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    # JSON strings are also valid TOML basic strings here, including escaped backslash.
    configured = [' ', 'AGENTS.md', 'AGENTS.override.md', 'MISSING.md',
                  ' \u2002'+name+'\u2002 ', name, './ignored.md', '..', '\0']
    put(isolated_codex_home/'config.toml',
        'project_doc_fallback_filenames = '+json.dumps(configured, ensure_ascii=False))
    settings, warnings = load_settings(isolated_codex_home)
    expected = ['MISSING.md'] + ([] if name in ('AGENTS.md', 'AGENTS.override.md') else [name])
    assert settings.fallback == expected and warnings
    for directory in (root, cwd):
        put(directory/name, 'WIN')
        put(directory/'later.md', 'shadow')
    settings.fallback.append('later.md')
    put(isolated_codex_home/'AGENTS.override.md', ' global ')
    good = create_snapshot(cwd, isolated_codex_home, settings, snapshot_version=2)
    if audits == 'omitted':
        good['entries'] = [r for r in good['entries'] if not r['status'].startswith('shadowed-')]
        for i, row in enumerate(good['entries']):
            row['order'] = i
    elif audits == 'hash-error':
        for row in good['entries']:
            if row['status'].startswith('shadowed-'):
                row.update(sha256=None, hash_error='synthetic optional hash failure', size=None)
    assert [Path(r['path']).name for r in good['entries']
            if r['scope'] == 'project' and r['selected']] == [name, name]
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
def test_every_nested_candidate_before_projection(tree, tmp_path, isolated_codex_home, monkeypatch,
                                                  index, position, unpaired):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_manifest, diff_artifacts
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    good = create_manifest([root, root/'pkg', cwd], root, isolated_codex_home,
                           Settings(), snapshot_version=2)
    bad = copy.deepcopy(good)
    bad['directories'][index]['snapshot'] = undiscoverable_copy(
        bad['directories'][index]['snapshot'])
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
        pytest.fail('projected or read source before validating nested candidate')
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


def test_custom_candidate_lexical_symlinks_and_no_root(tree, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_manifest, create_snapshot, diff_artifacts
    root, cwd = tree
    name = r'team\rules:local.md'
    target = put(root/'target.txt', 'WIN')
    for directory in (root, root/'pkg', cwd):
        (directory/name).symlink_to(target)
    (root/'alias').symlink_to(root/'pkg', target_is_directory=True)
    doc = create_manifest([cwd, root/'alias/api'], root, isolated_codex_home,
                          Settings(100, [name]), snapshot_version=2)
    assert [[r['path'] for r in d['snapshot']['entries']] for d in doc['directories']] == [
        [name, 'pkg/'+name, 'pkg/api/'+name], [name, 'alias/'+name, 'alias/api/'+name]]
    no_root = create_snapshot(root/'alias/api', isolated_codex_home,
                              Settings(100, [name], []), snapshot_version=2)
    assert not no_root['root_found'] and no_root['cwd'] == '.'
    assert [r['path'] for r in no_root['entries']] == [name]
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
