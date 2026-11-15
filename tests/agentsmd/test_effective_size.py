"""V2 rendered lengths must be possible before any historical verdict."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save
from .test_snapshot_v2 import document, v2


@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
def test_four_byte_rendered_total_reproducer(tree, tmp_path, isolated_codex_home, manifest):
    root, cwd = tree
    put(root/'AGENTS.md', 'BASE')
    before = (capture(root, isolated_codex_home, root, cwd, options=['--snapshot-version', 2])
              if manifest else v2(cwd, isolated_codex_home))
    original = document(before)
    bad = copy.deepcopy(original)
    nested = bad['directories'][-1]['snapshot'] if manifest else bad
    assert nested['effective']['rendered_bytes'] == nested['entries'][0]['text_bytes'] == 4
    nested['effective']['rendered_bytes'] = 0  # All entry fields and hashes unchanged.
    old = save(tmp_path/'before.json', before)
    impossible = put(tmp_path/'impossible.json', json.dumps(bad))
    mapping = put(tmp_path/'map.json', json.dumps({'pkg/api': 'pkg/api'}))
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    flags = [[], ['--json'], ['--fail-on-loss'], ['--fail-on-loss', '--json']]
    if manifest:
        flags += [['--include-file-changes', '--json'],
                  ['--cwd-map', mapping, '--include-file-changes', '--fail-on-loss', '--json']]
    for left, right in ((old, impossible), (impossible, old), (impossible, impossible)):
        for options in flags:
            result = cli('diff', left, right, *options)
            assert result.returncode == 2, (options, result.stdout, result.stderr)
            assert not result.stdout
            assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr


@pytest.mark.parametrize('global_body,project_bodies,budget,rendered', [
    (None, [None, None, None], 100, b''),
    (None, [b'', b' \n', b'\t'], 100, b''),
    (b' \n', [b'', None, None], 0, b''),
    (b' G\r\n', [None, None, None], 0, b'G'),
    (None, [b'BASE', None, None], 100, b'BASE'),
    (None, [b'A\r\n', b' \n', b'B'], 100, b'A\r\n\n\nB'),
    (None, [b'A', b'B', b'C'], 100, b'A\n\nB\n\nC'),
    (b' G ', [b'A', b'B', b'C'], 100, b'G\n\n--- project-doc ---\n\nA\n\nB\n\nC'),
    (b' \xff \n', [b'\xff', None, b'Z'], 100,
     '\ufffd\n\n--- project-doc ---\n\n\ufffd\n\nZ'.encode()),
    (b' G ', ['A\n€Z'.encode(), None, b'NEXT'], 3,
     'G\n\n--- project-doc ---\n\nA\n\ufffd'.encode()),
    (None, [b' \n tail', None, b'NEXT'], 3, b'NEX'),
    (b' G ', [b' \n tail', None, b'NEXT'], 0, b'G'),
    (None, ['\u2003tail'.encode(), None, b'X'], 3, b'X'),
])
def test_exact_rendered_accounting_source_free(tree, isolated_codex_home, monkeypatch,
                                               global_body, project_bodies, budget, rendered):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_snapshots, validate_snapshot
    root, cwd = tree
    if global_body is not None:
        put(isolated_codex_home/'AGENTS.md', global_body)
    for folder, body in zip((root, root/'pkg', cwd), project_bodies):
        if body is not None:
            put(folder/'AGENTS.md', body)
    modern = create_snapshot(cwd, isolated_codex_home, Settings(budget), snapshot_version=2)
    legacy = create_snapshot(cwd, isolated_codex_home, Settings(budget), snapshot_version=1)
    assert modern['effective']['rendered_bytes'] == len(rendered)
    import hashlib
    assert modern['effective']['sha256'] == hashlib.sha256(rendered).hexdigest()
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy(modern)
    assert validate_snapshot(modern) == frozen
    for left, right in ((modern, modern), (modern, legacy), (legacy, modern)):
        result = diff_snapshots(left, right)
        assert result['schema_version'] == 1 and not result['effective_changed']
        assert result['changes'] == []
    for size in {0, max(0, len(rendered)-1), len(rendered)+1, 2**63} - {len(rendered)}:
        invalid = copy.deepcopy(modern)
        invalid['effective']['rendered_bytes'] = size
        with pytest.raises(ScopeError, match='invalid snapshot'):
            validate_snapshot(invalid)
    # Consistency does not authenticate the saved fingerprints or v1 totals.
    assertion = copy.deepcopy(modern)
    assertion['effective']['sha256'] = 'f'*64
    assert validate_snapshot(assertion) == assertion
    legacy['effective']['rendered_bytes'] += 1
    assert validate_snapshot(legacy) == legacy
    assert modern == frozen


@pytest.mark.parametrize('position', ['before', 'after'])
def test_invalid_unpaired_manifest_checked_before_projection(tree, isolated_codex_home,
                                                            monkeypatch, position):
    from agentwrench.agentsmd.core import ScopeError
    from agentwrench.agentsmd.snapshot import diff_artifacts, validate_manifest
    root, cwd = tree
    put(root/'AGENTS.md', 'BASE')
    original = document(capture(root, isolated_codex_home, root, cwd,
                               options=['--snapshot-version', 2]))
    invalid = copy.deepcopy(original)
    invalid['directories'][-1]['snapshot']['effective']['rendered_bytes'] = 0
    other = copy.deepcopy(original)
    other['directories'].pop()  # Invalid row would be absent from matched cwds.
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_projection(*args):
        pytest.fail('projected before validating all original nested snapshots')
    monkeypatch.setattr('agentwrench.agentsmd.snapshot.project_manifest', no_projection)
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_manifest(invalid)
    left, right = (invalid, other) if position == 'before' else (other, invalid)
    for flags in ({}, dict(include_file_changes=True), dict(fail_on_loss=True),
                  dict(cwd_map={'.': '.'}, include_file_changes=True, fail_on_loss=True)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right, **flags)


@pytest.mark.parametrize('position', ['before', 'after'])
def test_moved_manifest_total_checked_before_comparison(tree, tmp_path, isolated_codex_home, position):
    from .test_cwd_map import compare, moved
    before, after = moved(tree, isolated_codex_home)
    (before if position == 'before' else after)['directories'][0]['snapshot']['effective']['rendered_bytes'] = 0
    shutil.rmtree(tree[0])
    shutil.rmtree(isolated_codex_home)
    for flags in ([], ['--include-file-changes', '--fail-on-loss', '--json']):
        result = compare(tmp_path, before, after, {'pkg': 'services/api'}, *flags)
        assert result.returncode == 2 and not result.stdout
        assert b'invalid snapshot' in result.stderr
