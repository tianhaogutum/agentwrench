"""Portable membership and effective-only historical multi-cwd CI contracts."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_snapshot import save, snapshot


def capture(base, home, *cwds, options=()):
    return cli('snapshot', '--manifest', '--base-dir', base, '--codex-home', home,
               *(arg for cwd in cwds for arg in ('--cwd', cwd)), *options)


def compare(tmp_path, before, after, *options):
    return cli('diff', save(tmp_path/'before.json', before),
               save(tmp_path/'after.json', after), *options)


def test_effective_only_historical_gate(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'ROOT')
    chosen = put(root/'pkg/AGENTS.override.md', 'ABtail')
    hidden = put(root/'pkg/AGENTS.md', '# PRIVATE_SHADOW')
    args = (root, isolated_codex_home, root, root/'pkg', cwd)
    before = capture(*args, options=['--max-bytes', 6])
    save(tmp_path/'initial.json', before)
    put(chosen, 'ABgone')
    put(hidden, '# PRIVATE_EDIT')
    after = capture(*args, options=['--max-bytes', 6])
    result = compare(tmp_path, before, after, '--json')
    assert result.returncode == 0, result.stderr
    diff = json.loads(result.stdout)
    assert diff['format'] == 'agentsmd-scope.manifest-diff' and diff['schema_version'] == 1
    assert diff['directories'] == [] and not diff['effective_changed']
    assert diff['summary'] == dict(before_cwd_count=3, after_cwd_count=3, changed_cwd_count=0,
                                   added_cwd_count=0, removed_cwd_count=0)
    put(chosen, 'XYgone')
    changed = capture(*args, options=['--max-bytes', 6])
    result = compare(tmp_path, after, changed, '--json')
    assert result.returncode == 1, result.stderr
    diff = json.loads(result.stdout)
    assert diff['effective_changed'] and diff['summary']['changed_cwd_count'] == 2
    assert [(d['cwd'], d['kind']) for d in diff['directories']] == [('pkg', 'changed'), ('pkg/api', 'changed')]
    for row in diff['directories']:
        assert set(row) == {'cwd', 'kind', 'before_effective', 'after_effective'}
        assert row['before_effective'] != row['after_effective']
    human = cli('diff', tmp_path/'before.json', tmp_path/'after.json')
    assert human.returncode == 1 and b'changed "pkg"' in human.stdout
    for secret in (b'PRIVATE', b'ROOT', b'ABtail', b'XYgone', str(tmp_path).encode()):
        assert secret not in result.stdout and secret not in human.stdout
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    assert cli('diff', tmp_path/'before.json', tmp_path/'after.json').returncode == 1


def test_portable_deduplicated_identities_and_reordered_membership(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'root')
    put(isolated_codex_home/'AGENTS.md', ' global ')
    before = capture(root, isolated_codex_home, cwd, cwd/'../api', root)
    save(tmp_path/'before.json', before)
    doc = json.loads(before.stdout)
    assert set(doc) == {'format', 'schema_version', 'agent', 'semantics', 'directories'}
    assert doc['format'] == 'agentsmd-scope.manifest' and doc['schema_version'] == 1
    assert [d['cwd'] for d in doc['directories']] == ['pkg/api', '.']
    assert doc['directories'][0]['snapshot'] == json.loads(snapshot(cwd, isolated_codex_home).stdout)
    assert str(tmp_path).encode() not in before.stdout
    clone, home = tmp_path/'clone', tmp_path/'other-home'
    shutil.copytree(root, clone)
    shutil.copytree(isolated_codex_home, home)
    relocated = capture(clone, home, clone/'pkg/api', clone)
    assert relocated.stdout == before.stdout
    reordered = capture(root, isolated_codex_home, root, cwd)
    result = compare(tmp_path, before, reordered, '--json')
    assert result.returncode == 0 and json.loads(result.stdout)['directories'] == []


@pytest.mark.parametrize('own_roots', [False, True])
def test_membership_added_removed_including_empty_bodies(tmp_path, isolated_codex_home, own_roots):
    base = tmp_path/'workspace'
    left, right, new = (base/name for name in ('one', 'two', 'three'))
    for directory in (left, right, new):
        directory.mkdir(parents=True)
        if own_roots:
            put(directory/'.git', 'gitdir: synthetic')
    # Independent roots or no roots: snapshots say '.', but identities stay unique.
    before = capture(base, isolated_codex_home, left, right)
    after = capture(base, isolated_codex_home, new, right)
    doc = json.loads(save(tmp_path/'manifest.json', before).read_bytes())
    assert [d['snapshot']['cwd'] for d in doc['directories']] == ['.', '.']
    result = compare(tmp_path, before, after, '--json')
    assert result.returncode == 1, result.stderr
    diff = json.loads(result.stdout)
    assert diff['summary'] == dict(before_cwd_count=2, after_cwd_count=2, changed_cwd_count=2,
                                   added_cwd_count=1, removed_cwd_count=1)
    removed, added = diff['directories']
    assert (removed['cwd'], removed['kind'], removed['after_effective']) == ('one', 'removed', None)
    assert (added['cwd'], added['kind'], added['before_effective']) == ('three', 'added', None)
    assert removed['before_effective'] == added['after_effective']


def test_flags_and_root_changes_do_not_change_manifest_identity(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(cwd/'RULES.md', 'same')
    before = capture(root, isolated_codex_home, cwd, options=['--fallback', 'RULES.md'])
    after = capture(root, isolated_codex_home, cwd,
                    options=['--fallback', 'RULES.md', '--no-root-markers', '--max-bytes', 100])
    left = json.loads(save(tmp_path/'capture.json', before).read_bytes())['directories'][0]
    right = json.loads(after.stdout)['directories'][0]
    assert left['cwd'] == right['cwd'] == 'pkg/api'
    assert left['snapshot']['cwd'] == 'pkg/api' and right['snapshot']['cwd'] == '.'
    result = compare(tmp_path, before, after, '--json')
    assert result.returncode == 0 and json.loads(result.stdout)['directories'] == []


@pytest.mark.parametrize('invalid', ['no-cwd', 'no-base', 'no-home', 'base-alone', 'repeated-single',
                                    'missing-cwd', 'outside', 'missing-base', 'budget', 'config',
                                    'selected-error', 'too-many', 'claude', 'markers'])
def test_capture_errors_have_no_partial_output(tree, tmp_path, isolated_codex_home, invalid):
    root, cwd = tree
    args = ['snapshot', '--manifest', '--base-dir', root, '--codex-home', isolated_codex_home,
            '--cwd', root, '--cwd', cwd]
    if invalid in ('no-base', 'no-home'):
        index = args.index('--base-dir' if invalid == 'no-base' else '--codex-home')
        del args[index:index + 2]
    elif invalid == 'no-cwd':
        args = args[:-4]
    elif invalid == 'base-alone':
        args.remove('--manifest')
    elif invalid == 'repeated-single':
        args.remove('--manifest')
        index = args.index('--base-dir')
        del args[index:index + 2]
    elif invalid in ('missing-cwd', 'outside'):
        args += ['--cwd', root/'missing' if invalid == 'missing-cwd' else tmp_path]
    elif invalid == 'missing-base':
        args[args.index('--base-dir') + 1] = tmp_path/'absent'
    elif invalid == 'budget':
        args += ['--max-bytes', -1]
    elif invalid == 'config':
        put(isolated_codex_home/'config.toml', 'secret = "PRIVATE_CONFIG" invalid')
    elif invalid == 'selected-error':
        (cwd/'AGENTS.md').symlink_to('AGENTS.md')
    elif invalid == 'too-many':
        args += [arg for _ in range(63) for arg in ('--cwd', root)]
    elif invalid == 'claude':
        args += ['--agent', 'claude']
    else:
        args += ['--no-root-markers', '--root-marker', '.git']
    result = cli(*args)
    assert result.returncode == 2 and not result.stdout
    assert b'Traceback' not in result.stderr and b'PRIVATE_CONFIG' not in result.stderr


def test_exact_64_argument_boundary_deduplicates(tree, isolated_codex_home):
    root, _ = tree
    result = capture(root, isolated_codex_home, *([root]*64))
    assert result.returncode == 0, result.stderr
    assert [d['cwd'] for d in json.loads(result.stdout)['directories']] == ['.']


@pytest.mark.parametrize('mutation', ['version', 'format', 'semantics', 'duplicate', 'empty', 'too-many',
                                     'absolute', 'parent', 'nonnormal', 'cwd-type', 'row-type',
                                     'nested-version', 'nested-digest', 'extra', 'mixed'])
def test_manifest_validation_fails_closed(tree, tmp_path, isolated_codex_home, mutation):
    root, cwd = tree
    result = capture(root, isolated_codex_home, root, cwd)
    before = save(tmp_path/'before.json', result)
    doc = copy.deepcopy(json.loads(result.stdout))
    if mutation in ('version', 'format', 'semantics'):
        doc[{'version': 'schema_version'}.get(mutation, mutation)] = 'unsupported'
    elif mutation == 'duplicate':
        doc['directories'].append(doc['directories'][0])
    elif mutation == 'empty':
        doc['directories'] = []
    elif mutation == 'too-many':
        doc['directories'] = [dict(cwd=f'cwd-{i}', snapshot=doc['directories'][0]['snapshot']) for i in range(65)]
    elif mutation in ('absolute', 'parent', 'nonnormal', 'cwd-type'):
        doc['directories'][0]['cwd'] = {'absolute': '/tmp/host', 'parent': '../out',
                                       'nonnormal': './pkg', 'cwd-type': []}[mutation]
    elif mutation == 'row-type':
        doc['directories'][0] = []
    elif mutation == 'nested-version':
        doc['directories'][1]['snapshot']['schema_version'] = 99
    elif mutation == 'nested-digest':
        doc['directories'][1]['snapshot']['effective']['sha256'] = 'bad'
    elif mutation == 'extra':
        doc['unrecognized'] = True
    else:
        doc = doc['directories'][0]['snapshot']
    after = put(tmp_path/'after.json', json.dumps(doc))
    for left, right in ((before, after), (after, before)):
        diff = cli('diff', left, right, '--json')
        assert diff.returncode == 2 and not diff.stdout
        assert b'Traceback' not in diff.stderr


def test_nested_duplicate_json_keys_are_rejected(tree, tmp_path, isolated_codex_home):
    root, _ = tree
    result = capture(root, isolated_codex_home, root)
    before = save(tmp_path/'before.json', result)
    after = put(tmp_path/'after.json', result.stdout.replace(b'"cwd": "."', b'"cwd": ".", "cwd": "."', 1))
    result = cli('diff', before, after)
    assert result.returncode == 2 and not result.stdout


def test_64_unique_cwds_and_lexical_symlink_identity(tmp_path, isolated_codex_home):
    base = tmp_path/'base'
    folders = [base/f'pkg-{i}' for i in range(63)]
    for folder in folders:
        folder.mkdir(parents=True)
    alias = base/'alias'
    alias.symlink_to(folders[0], target_is_directory=True)
    folders.append(alias)
    result = capture(base, isolated_codex_home, *folders)
    assert result.returncode == 0, result.stderr
    assert [row['cwd'] for row in json.loads(result.stdout)['directories']] == [folder.name for folder in folders]


def test_manifest_size_boundary_fails_without_partial_output(tree, isolated_codex_home, monkeypatch, capsys):
    import agentwrench.agentsmd.snapshot as snapshots
    from agentwrench.agentsmd.cli import main
    from agentwrench.agentsmd.core import Settings
    root, cwd = tree
    doc = snapshots.create_manifest([root, cwd], root, isolated_codex_home, Settings())
    exact = len(json.dumps(doc, ensure_ascii=True, indent=2).encode()) + 1
    args = ['snapshot', '--manifest', '--base-dir', str(root), '--codex-home', str(isolated_codex_home),
            '--cwd', str(root), '--cwd', str(cwd)]
    monkeypatch.setattr(snapshots, 'MAX_FILE_BYTES', exact)
    assert main(args) == 0
    assert len(capsys.readouterr().out.encode()) == exact
    monkeypatch.setattr(snapshots, 'MAX_FILE_BYTES', exact - 1)
    assert main(args) == 2
    result = capsys.readouterr()
    assert not result.out and 'artifact limit' in result.err


def test_all_cwds_validated_before_inspection_and_config_read_once(tree, tmp_path, isolated_codex_home, monkeypatch, capsys):
    import agentwrench.agentsmd.snapshot as snapshots
    from agentwrench.agentsmd.cli import main
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    root, cwd = tree
    inspected = []
    original = snapshots.create_snapshot
    def counted(folder, *args, **kwargs):
        inspected.append(folder)
        return original(folder, *args, **kwargs)
    monkeypatch.setattr(snapshots, 'create_snapshot', counted)
    for folders in ([root, tmp_path], [root, root/'missing']):
        with pytest.raises(ScopeError):
            snapshots.create_manifest(folders, root, isolated_codex_home, Settings())
        assert inspected == []
    config = put(isolated_codex_home/'config.toml', 'project_doc_max_bytes = 42')
    reads = []
    original_read = Reader.read
    def tracked_read(self, path):
        reads.append(path)
        return original_read(self, path)
    monkeypatch.setattr(Reader, 'read', tracked_read)
    assert main(['snapshot', '--manifest', '--base-dir', str(root), '--codex-home', str(isolated_codex_home),
                 '--cwd', str(root), '--cwd', str(cwd)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert all(row['snapshot']['settings']['max_bytes'] == 42 for row in doc['directories'])
    assert reads.count(config) == 1 and inspected == [root, cwd]
