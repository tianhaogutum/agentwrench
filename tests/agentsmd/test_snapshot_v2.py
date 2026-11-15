"""V2 raw byte-cut persistence and historical v1/v2 compatibility contracts."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import compare, save, snapshot


def v2(cwd, home, *options):
    return snapshot(cwd, home, '--snapshot-version', 2, *options)


def document(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('data,budget,status,loaded,cut', [
    (b'A\nBtail', 3, 'partial', 3, dict(byte_offset=3, line=2, splits_utf8_character=False)),
    ('A\n€Z'.encode(), 3, 'partial', 3, dict(byte_offset=3, line=2, splits_utf8_character=True)),
    (b' \n X', 3, 'skipped-empty', 0, dict(byte_offset=3, line=2, splits_utf8_character=False)),
    (b'nonempty', 0, 'dropped-over-budget', 0, dict(byte_offset=0, line=1, splits_utf8_character=False)),
    (b'', 0, 'dropped-over-budget', 0, None),
    (b'full', 4, 'loaded', 4, None),
    (b'\xffX', 1, 'partial', 1, dict(byte_offset=1, line=1, splits_utf8_character=False)),
])
def test_exact_raw_cut_projection(tree, isolated_codex_home, data, budget, status, loaded, cut):
    root, cwd = tree
    put(root/'AGENTS.override.md', data)
    put(root/'AGENTS.md', '# PRIVATE_SHADOW_HEADING')
    result = v2(cwd, isolated_codex_home, '--max-bytes', budget)
    doc = document(result)
    assert doc['schema_version'] == 2
    selected, hidden = doc['entries']
    assert (selected['status'], selected['bytes_loaded'], selected['cut']) == (status, loaded, cut)
    assert hidden['cut'] is None
    assert b'PRIVATE' not in result.stdout and b'heading' not in result.stdout
    # The original artifact shape and default capture remain unchanged.
    legacy = document(snapshot(cwd, isolated_codex_home, '--max-bytes', budget))
    projected = copy.deepcopy(doc)
    projected['schema_version'] = 1
    for row in projected['entries']:
        del row['cut']
    assert legacy == projected


def test_same_path_scopes_cumulative_cuts_and_no_rereads(tree, monkeypatch):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot
    root, cwd = tree
    path = put(root/'AGENTS.md', 'A\n€Z')
    put(cwd/'AGENTS.md', 'fully dropped')
    reads = []
    original = Reader.read
    def tracked(self, target):
        reads.append(target)
        return original(self, target)
    monkeypatch.setattr(Reader, 'read', tracked)
    doc = create_snapshot(cwd, root, Settings(3), snapshot_version=2)
    assert reads == [path, path, cwd/'AGENTS.md']
    assert [e['cut'] for e in doc['entries']] == [None,
        dict(byte_offset=3, line=2, splits_utf8_character=True),
        dict(byte_offset=0, line=1, splits_utf8_character=False)]


def test_whitespace_cut_does_not_charge_following_file(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', ' \n tail')
    put(cwd/'AGENTS.md', 'next')
    doc = document(v2(cwd, isolated_codex_home, '--max-bytes', 3))
    assert [row['cut']['byte_offset'] for row in doc['entries']] == [3, 3]
    assert [row['bytes_loaded'] for row in doc['entries']] == [0, 3]


def test_historical_cut_only_diff_and_cross_version_projection(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    chosen = put(root/'AGENTS.md', ' \n tail')
    before = v2(cwd, isolated_codex_home, '--max-bytes', 3)
    legacy = snapshot(cwd, isolated_codex_home, '--max-bytes', 3)
    for left, right in ((legacy, before), (before, legacy)):
        result = compare(tmp_path, left, right, '--json')
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['changes'] == []
    after = v2(cwd, isolated_codex_home, '--max-bytes', 1)
    result = compare(tmp_path, before, after, '--json')
    assert result.returncode == 0, result.stderr
    diff = json.loads(result.stdout)
    assert diff['schema_version'] == 1 and not diff['effective_changed']
    old, new = diff['changes'][0]['before'], diff['changes'][0]['after']
    assert old['sha256'] == new['sha256']
    assert old['bytes_loaded'] == new['bytes_loaded'] == 0
    assert old['cut'] == dict(byte_offset=3, line=2, splits_utf8_character=False)
    assert new['cut'] == dict(byte_offset=1, line=1, splits_utf8_character=False)
    put(chosen, 'EFFECTIVE')
    changed = v2(cwd, isolated_codex_home, '--max-bytes', 3)
    save(tmp_path/'legacy.json', legacy)
    save(tmp_path/'changed.json', changed)
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    assert cli('diff', tmp_path/'before.json', tmp_path/'after.json').returncode == 0
    for left, right in (('legacy.json', 'changed.json'), ('changed.json', 'legacy.json')):
        result = cli('diff', tmp_path/left, tmp_path/right, '--json')
        assert result.returncode == 1, result.stderr
        assert all('cut' not in state for row in json.loads(result.stdout)['changes']
                   for state in (row['before'], row['after']) if state is not None)


def test_v2_manifest_portability_membership_and_historical_gate(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    chosen = put(root/'AGENTS.md', ' \n tail')
    def manifest(version=2, folders=None, budget=3):
        return capture(root, isolated_codex_home, *(folders or [root, cwd]),
                       options=['--snapshot-version', version, '--max-bytes', budget])
    before = manifest()
    doc = document(before)
    assert doc['schema_version'] == 2
    assert [row['snapshot']['schema_version'] for row in doc['directories']] == [2, 2]
    clone, home = tmp_path/'clone', tmp_path/'other-home'
    shutil.copytree(root, clone)
    shutil.copytree(isolated_codex_home, home)
    assert capture(clone, home, clone, clone/'pkg/api', clone/'pkg/../pkg/api',
                   options=['--snapshot-version', 2, '--max-bytes', 3]).stdout == before.stdout
    for after in (manifest(1), manifest(budget=1)):
        result = compare(tmp_path, before, after, '--json')
        assert result.returncode == 0 and json.loads(result.stdout)['directories'] == []
    removed = manifest(folders=[root])
    result = compare(tmp_path, before, removed, '--json')
    assert result.returncode == 1
    assert [(r['cwd'], r['kind']) for r in json.loads(result.stdout)['directories']] == [('pkg/api', 'removed')]
    legacy = manifest(1)
    put(chosen, 'changed')
    changed = manifest()
    compare(tmp_path, legacy, changed, '--json')
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    result = cli('diff', tmp_path/'before.json', tmp_path/'after.json', '--json')
    assert result.returncode == 1
    assert [r['cwd'] for r in json.loads(result.stdout)['directories']] == ['.', 'pkg/api']


@pytest.mark.parametrize('manifest', [False, True])
@pytest.mark.parametrize('mutation', ['missing', 'extra', 'type', 'offset-bool', 'negative', 'eof',
    'wrong-budget', 'line-zero', 'line-bool', 'line-too-high', 'split-type', 'null', 'global',
    'shadow', 'uncut', 'zero-split', 'zero-line', 'version', 'mixed-version'])
def test_malformed_v2_cuts_fail_without_partial_stdout(tree, tmp_path, isolated_codex_home, manifest, mutation):
    root, cwd = tree
    put(root/'AGENTS.override.md', 'A\n€Z')
    put(root/'AGENTS.md', '# PRIVATE_SHADOW')
    put(isolated_codex_home/'AGENTS.md', 'global')
    put(cwd/'AGENTS.md', 'dropped')
    options = ['--snapshot-version', 2, '--max-bytes', 3]
    before = capture(root, isolated_codex_home, cwd, options=options) if manifest else v2(cwd, isolated_codex_home, '--max-bytes', 3)
    doc = copy.deepcopy(document(before))
    nested = doc['directories'][0]['snapshot'] if manifest else doc
    row = nested['entries'][1]
    if mutation == 'missing':
        del row['cut']
    elif mutation == 'extra':
        row['cut']['heading'] = 'PRIVATE_HEADING'
    elif mutation == 'type':
        row['cut'] = []
    elif mutation in ('offset-bool', 'negative', 'eof', 'wrong-budget'):
        row['cut']['byte_offset'] = {'offset-bool': True, 'negative': -1, 'eof': 6, 'wrong-budget': 2}[mutation]
    elif mutation in ('line-zero', 'line-bool', 'line-too-high'):
        row['cut']['line'] = {'line-zero': 0, 'line-bool': True, 'line-too-high': 5}[mutation]
    elif mutation == 'split-type':
        row['cut']['splits_utf8_character'] = 1
    elif mutation == 'null':
        row['cut'] = None
    elif mutation in ('global', 'shadow'):
        nested['entries'][0 if mutation == 'global' else 2]['cut'] = row['cut']
    elif mutation == 'uncut':
        row['size'] = 3
    elif mutation in ('zero-split', 'zero-line'):
        nested['entries'][-1]['cut']['splits_utf8_character' if mutation == 'zero-split' else 'line'] = True if mutation == 'zero-split' else 2
    elif mutation == 'version':
        nested['schema_version'] = 3
    else:
        # A legacy artifact cannot carry cut fields, nor can a v2 manifest nest v1.
        nested['schema_version'] = 1
        if manifest:
            for entry in nested['entries']:
                del entry['cut']
    old = save(tmp_path/'before.json', before)
    bad = put(tmp_path/'bad.json', json.dumps(doc))
    for left, right in ((old, bad), (bad, old)):
        result = cli('diff', left, right, '--json')
        assert result.returncode == 2 and not result.stdout
        assert b'Traceback' not in result.stderr and b'PRIVATE' not in result.stderr


@pytest.mark.parametrize('version', [0, 3, 'two'])
def test_unsupported_capture_version(tree, isolated_codex_home, version):
    _, cwd = tree
    result = snapshot(cwd, isolated_codex_home, '--snapshot-version', version)
    assert result.returncode == 2 and not result.stdout
