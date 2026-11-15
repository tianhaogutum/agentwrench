"""Historical snapshots use raw-file and exact-render hashes, never mtimes."""
import copy
import hashlib
import json
import os

import pytest

from .conftest import put
from .test_cli import cli


def snapshot(cwd, home, *options):
    return cli('snapshot', '--cwd', cwd, '--codex-home', home, *options)


def save(path, result):
    assert result.returncode == 0, result.stderr
    path.write_bytes(result.stdout)
    return path


def compare(tmp_path, before, after, *options):
    return cli('diff', save(tmp_path/'before.json', before),
               save(tmp_path/'after.json', after), *options)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_deterministic_portable_hashes_and_shadowed_status(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(isolated_codex_home/'AGENTS.override.md', '  GLOBAL  ')
    put(isolated_codex_home/'AGENTS.md', 'PRIVATE_GLOBAL_SHADOW')
    put(root/'AGENTS.override.md', 'ABCDEFtail')
    put(root/'AGENTS.md', '# PRIVATE_SHADOW')
    first = snapshot(cwd, isolated_codex_home, '--max-bytes', 6)
    save(tmp_path/'snapshot.json', first)
    # Stable across mtime changes and entirely different checkout/home locations.
    os.utime(root/'AGENTS.override.md', (1, 1))
    assert snapshot(cwd, isolated_codex_home, '--max-bytes', 6).stdout == first.stdout
    import shutil
    clone, home = tmp_path/'clone', tmp_path/'home-clone'
    shutil.copytree(root, clone)
    shutil.copytree(isolated_codex_home, home)
    assert snapshot(clone/'pkg/api', home, '--max-bytes', 6).stdout == first.stdout
    doc = json.loads(first.stdout)
    assert doc['format'] == 'agentsmd-scope.snapshot' and doc['schema_version'] == 1
    assert doc['cwd'] == 'pkg/api' and doc['root_found'] is True
    assert doc['effective'] == {'sha256': digest(b'GLOBAL\n\n--- project-doc ---\n\nABCDEF'),
                                'rendered_bytes': 35}
    assert [(e['scope'], e['path'], e['selected']) for e in doc['entries']] == [
        ('global', 'AGENTS.override.md', True), ('global', 'AGENTS.md', False),
        ('project', 'AGENTS.override.md', True), ('project', 'AGENTS.md', False)]
    assert [e['sha256'] for e in doc['entries']] == list(map(digest, [
        b'  GLOBAL  ', b'PRIVATE_GLOBAL_SHADOW', b'ABCDEFtail', b'# PRIVATE_SHADOW']))
    assert doc['entries'][-1]['status'] == 'shadowed-by-override'
    assert doc['entries'][2]['status'] == 'partial'
    for secret in [b'PRIVATE', b'ABCDEF', b'GLOBAL', str(tmp_path).encode(), b'heading']:
        assert secret not in first.stdout


def test_ci_only_fails_for_effective_changes(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.override.md', 'ABCDEFtail')
    hidden = put(root/'AGENTS.md', 'hidden-a')
    meta = hidden.stat()
    before = snapshot(cwd, isolated_codex_home, '--max-bytes', 6)
    put(hidden, 'hidden-b')  # identical size and mtime cannot hide a content edit
    os.utime(hidden, ns=(meta.st_atime_ns, meta.st_mtime_ns))
    after = snapshot(cwd, isolated_codex_home, '--max-bytes', 6)
    result = compare(tmp_path, before, after, '--json')
    assert result.returncode == 0, result.stderr
    diff = json.loads(result.stdout)
    assert diff['effective_changed'] is False
    assert len(diff['changes']) == 1
    assert diff['changes'][0]['path'] == 'AGENTS.md'
    assert diff['changes'][0]['before']['sha256'] != diff['changes'][0]['after']['sha256']
    put(root/'AGENTS.override.md', 'ABCDEFgone')
    tail = snapshot(cwd, isolated_codex_home, '--max-bytes', 6)
    assert compare(tmp_path, after, tail).returncode == 0
    put(root/'AGENTS.override.md', 'UVWXYZgone')
    effective = snapshot(cwd, isolated_codex_home, '--max-bytes', 6)
    changed = compare(tmp_path, tail, effective, '--json')
    assert changed.returncode == 1 and json.loads(changed.stdout)['effective_changed']
    assert cli('diff', tmp_path/'before.json', tmp_path/'after.json').stdout.startswith(b'CHANGED:')
    # Diff is historical and needs no source files, homes or watcher.
    import shutil
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    assert cli('diff', tmp_path/'before.json', tmp_path/'after.json').returncode == 1


def test_selection_add_remove_and_equal_body_change(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    normal = put(root/'AGENTS.md', 'same')
    before = snapshot(cwd, isolated_codex_home)
    override = put(root/'AGENTS.override.md', 'same')
    after = snapshot(cwd, isolated_codex_home)
    result = compare(tmp_path, before, after, '--json')
    assert result.returncode == 0
    changes = json.loads(result.stdout)['changes']
    assert {c['kind'] for c in changes} == {'added', 'changed'}
    assert next(c for c in changes if c['path'] == 'AGENTS.md')['after']['selected'] is False
    normal.unlink()
    removed = snapshot(cwd, isolated_codex_home)
    assert compare(tmp_path, after, removed).returncode == 0
    override.unlink()
    assert compare(tmp_path, removed, snapshot(cwd, isolated_codex_home)).returncode == 1


def test_empty_global_trim_unicode_and_budget_changes(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(isolated_codex_home/'AGENTS.override.md', ' \n')
    global_file = put(isolated_codex_home/'AGENTS.md', ' user ')
    put(root/'AGENTS.md', b'A\xe2\x82\xacZ')
    before = snapshot(cwd, isolated_codex_home, '--max-bytes', 2)
    doc = json.loads(before.stdout)
    assert [e['selected'] for e in doc['entries']] == [False, True, True]
    assert doc['effective']['sha256'] == digest('user\n\n--- project-doc ---\n\nA\ufffd'.encode())
    put(global_file, '\n user\t')
    after = snapshot(cwd, isolated_codex_home, '--max-bytes', 2)
    assert compare(tmp_path, before, after).returncode == 0
    assert compare(tmp_path, after, snapshot(cwd, isolated_codex_home, '--max-bytes', 5)).returncode == 1
    assert compare(tmp_path, after, snapshot(cwd, isolated_codex_home, '--max-bytes', 0)).returncode == 1
    # Settings changes that leave the rendered body alone remain metadata-only.
    assert compare(tmp_path, snapshot(cwd, isolated_codex_home, '--max-bytes', 5),
                   snapshot(cwd, isolated_codex_home, '--max-bytes', 10)).returncode == 0


def test_selected_errors_fail_closed_but_optional_shadow_hash_errors_do_not(tree, isolated_codex_home):
    root, cwd = tree
    blocked = put(root/'AGENTS.md', 'hidden')
    blocked.chmod(0)
    try:
        put(root/'AGENTS.override.md', 'winner')
        result = snapshot(cwd, isolated_codex_home)
        assert result.returncode == 0, result.stderr
        row = json.loads(result.stdout)['entries'][-1]
        assert row['selected'] is False and row['sha256'] is None and row['hash_error'] == 'PermissionError'
        (root/'AGENTS.override.md').unlink()
        result = snapshot(cwd, isolated_codex_home)
        assert result.returncode == 2 and not result.stdout
    finally:
        blocked.chmod(0o600)
    blocked.unlink()
    blocked.symlink_to('AGENTS.md')
    assert snapshot(cwd, isolated_codex_home).returncode == 2
    put(root/'AGENTS.override.md', 'winner')
    assert snapshot(cwd, isolated_codex_home).returncode == 0


@pytest.mark.parametrize('mutation', ['version', 'format', 'semantics', 'digest', 'entries',
                                     'selected', 'path', 'duplicate', 'effective-size', 'settings'])
def test_malformed_snapshots_fail_instead_of_passing_gate(tree, tmp_path, isolated_codex_home, mutation):
    root, cwd = tree
    put(root/'AGENTS.md', 'rules')
    result = snapshot(cwd, isolated_codex_home)
    before = save(tmp_path/'before.json', result)
    doc = copy.deepcopy(json.loads(result.stdout))
    if mutation in ('version', 'format', 'semantics'):
        doc[{'version': 'schema_version'}.get(mutation, mutation)] = 'unsupported'
    elif mutation == 'digest':
        doc['effective']['sha256'] = 'invalid'
    elif mutation == 'entries':
        del doc['entries']
    elif mutation == 'selected':
        doc['entries'][0]['selected'] = 'false'
    elif mutation == 'path':
        doc['entries'][0]['path'] = '../outside.md'
    elif mutation == 'duplicate':
        doc['entries'].append(doc['entries'][0])
    elif mutation == 'effective-size':
        doc['effective']['rendered_bytes'] = -1
    else:
        doc['settings']['max_bytes'] = True
    after = put(tmp_path/'after.json', json.dumps(doc))
    diff = cli('diff', before, after, '--json')
    assert diff.returncode == 2 and not diff.stdout
    assert b'invalid snapshot' in diff.stderr


def test_invalid_json_missing_file_config_and_agent_fail_closed(tree, tmp_path, isolated_codex_home):
    _, cwd = tree
    invalid = put(tmp_path/'bad.json', '{"SECRET": invalid')
    result = cli('diff', invalid, invalid)
    assert result.returncode == 2 and not result.stdout and b'SECRET' not in result.stderr
    assert cli('diff', tmp_path/'absent', invalid).returncode == 2
    assert snapshot(tmp_path/'absent', isolated_codex_home).returncode == 2
    put(isolated_codex_home/'config.toml', 'project_doc_max_bytes = -1')
    assert snapshot(cwd, isolated_codex_home).returncode == 2
    result = cli('snapshot', '--cwd', cwd, '--agent', 'claude')
    assert result.returncode == 2 and not result.stdout


def test_shadow_hash_limit_never_yields_false_empty_hash(tree, isolated_codex_home, monkeypatch):
    import agentwrench.agentsmd.core as core
    import agentwrench.agentsmd.snapshot as snapshots
    root, cwd = tree
    put(root/'AGENTS.override.md', '123456')
    put(root/'AGENTS.md', 'abcdef')
    put(root/'RULES.md', 'x')
    monkeypatch.setattr(core, 'MAX_TOTAL_BYTES', 10)
    monkeypatch.setattr(snapshots, 'MAX_TOTAL_BYTES', 10)
    doc = snapshots.create_snapshot(cwd, isolated_codex_home, core.Settings(fallback=['RULES.md']))
    assert doc['entries'][0]['sha256'] == digest(b'123456')
    for row in doc['entries'][1:]:
        assert row['sha256'] is None and row['hash_error']
    assert doc['effective']['sha256'] == digest(b'123456')


def test_selected_hashes_use_loading_read_and_same_path_scopes(tree, monkeypatch):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot
    root, cwd = tree
    path = put(root/'AGENTS.md', 'abcde')
    reads = []
    original = Reader.read
    def counted(self, target):
        reads.append(target)
        return original(self, target)
    monkeypatch.setattr(Reader, 'read', counted)
    doc = create_snapshot(cwd, root, Settings(2))
    assert reads == [path, path]  # exactly one load per scope, no hash rereads
    assert [e['sha256'] for e in doc['entries']] == [digest(b'abcde')] * 2
    assert [e['bytes_loaded'] for e in doc['entries']] == [5, 2]


def test_fallback_no_root_and_settings_are_snapshotted(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'excluded')
    put(cwd/'RULES.md', 'fallback')
    put(cwd/'TEAM.md', 'shadow')
    result = snapshot(cwd, isolated_codex_home, '--no-root-markers', '--fallback', 'RULES.md', 'TEAM.md')
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc['cwd'] == '.' and doc['root_found'] is False
    assert doc['settings'] == {'max_bytes': 32768, 'fallback': ['RULES.md', 'TEAM.md'], 'root_markers': []}
    assert [(e['path'], e['status']) for e in doc['entries']] == [
        ('RULES.md', 'loaded'), ('TEAM.md', 'shadowed-by-precedence')]


@pytest.mark.parametrize('source', ['[]', '{}', '{"format":1,"format":2}', 'NaN', '[' * 1100])
def test_invalid_json_shapes_and_duplicate_keys_are_errors(tmp_path, source):
    artifact = put(tmp_path/'bad.json', source)
    result = cli('diff', artifact, artifact, '--json')
    assert result.returncode == 2 and not result.stdout
    assert b'invalid snapshot' in result.stderr
