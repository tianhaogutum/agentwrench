"""V2 cannot assert simultaneous loaded globals, even with consistent lengths."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save
from .test_snapshot_v2 import document, v2


def dual_loaded(snapshot):
    winner, shadow = snapshot['entries']
    assert winner['scope'] == shadow['scope'] == 'global'
    assert winner['selected'] and not shadow['selected']
    shadow.update(selected=True, status='loaded', bytes_loaded=3, text_bytes=3)
    snapshot['effective']['rendered_bytes'] = 6  # Correct sum, impossible selection.


@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
@pytest.mark.parametrize('position', ['before', 'after', 'self'])
def test_dual_loaded_global_reproducer(tree, tmp_path, isolated_codex_home, manifest, position):
    root, cwd = tree
    put(isolated_codex_home/'AGENTS.override.md', 'WIN')
    put(isolated_codex_home/'AGENTS.md', 'LOW')
    original = (capture(root, isolated_codex_home, root, cwd, options=['--snapshot-version', 2])
                if manifest else v2(cwd, isolated_codex_home))
    bad = copy.deepcopy(document(original))
    dual_loaded(bad['directories'][-1]['snapshot'] if manifest else bad)
    good = save(tmp_path/'good.json', original)
    impossible = put(tmp_path/'impossible.json', json.dumps(bad))
    mapping = put(tmp_path/'map.json', json.dumps({'pkg/api': 'pkg/api'}))
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    left, right = {'before': (impossible, good), 'after': (good, impossible),
                   'self': (impossible, impossible)}[position]
    options = [[], ['--json'], ['--fail-on-loss'], ['--fail-on-loss', '--json']]
    if manifest:
        options += [['--include-file-changes'], ['--include-file-changes', '--json']]
        options += [['--cwd-map', mapping, *flags] for flags in options.copy()]
        options += [['--cwd-map', mapping, '--include-file-changes', '--fail-on-loss', '--json']]
    for flags in options:
        result = cli('diff', left, right, *flags)
        assert result.returncode == 2, (flags, result.stdout, result.stderr)
        assert not result.stdout
        assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr


@pytest.mark.parametrize('override,regular,selected,statuses,rendered', [
    (None, None, [], [], b''),
    (None, b' G ', ['AGENTS.md'], ['loaded'], b'G'),
    (b'WIN', None, ['AGENTS.override.md'], ['loaded'], b'WIN'),
    (b'WIN', b'LOW', ['AGENTS.override.md'], ['loaded', 'shadowed-by-override'], b'WIN'),
    (b'', b'LOW', ['AGENTS.md'], ['skipped-empty', 'loaded'], b'LOW'),
    (b' \r\n', b' \xff \n', ['AGENTS.md'], ['skipped-empty', 'loaded'], b'\xef\xbf\xbd'),
    (b' \xff \n', b'LOW', ['AGENTS.override.md'], ['loaded', 'shadowed-by-override'], b'\xef\xbf\xbd'),
    (b' \n', b'\t', [], ['skipped-empty', 'skipped-empty'], b''),
])
def test_authentic_global_selection_source_free(tree, isolated_codex_home, monkeypatch,
                                                override, regular, selected, statuses, rendered):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    for name, body in (('AGENTS.override.md', override), ('AGENTS.md', regular)):
        if body is not None:
            put(isolated_codex_home/name, body)
    modern = create_snapshot(cwd, isolated_codex_home, Settings(0), snapshot_version=2)
    legacy = create_snapshot(cwd, isolated_codex_home, Settings(0), snapshot_version=1)
    assert [r['path'] for r in modern['entries'] if r['selected']] == selected
    assert [r['status'] for r in modern['entries']] == statuses
    import hashlib
    assert modern['effective'] == dict(sha256=hashlib.sha256(rendered).hexdigest(), rendered_bytes=len(rendered))
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical comparison read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy(modern)
    assert validate_snapshot(modern) == frozen
    for left, right in ((modern, modern), (legacy, modern), (modern, legacy)):
        result = diff_artifacts(left, right)
        assert result['schema_version'] == 1 and not result['effective_changed'] and not result['changes']
    assert diff_artifacts(modern, modern, fail_on_loss=True)['loss_policy']['passed']
    assert modern == frozen


def test_global_optional_shadow_failure_and_untrusted_v1(tree, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    put(isolated_codex_home/'AGENTS.override.md', 'WIN')
    shadow = put(isolated_codex_home/'AGENTS.md', 'LOW')
    read = Reader.read
    def optional_failure(self, path):
        if path == shadow:
            raise PermissionError('synthetic optional hash failure')
        return read(self, path)
    monkeypatch.setattr(Reader, 'read', optional_failure)
    original = create_snapshot(cwd, isolated_codex_home, Settings(), snapshot_version=2)
    assert original['entries'][1]['sha256'] is None
    assert original['entries'][1]['hash_error'] == 'PermissionError'
    # Use an asserted valid digest for the mutation; the count must be the only defect.
    good = copy.deepcopy(original)
    good['entries'][1].update(sha256='a'*64, hash_error=None)
    bad = copy.deepcopy(good)
    dual_loaded(bad)
    legacy = copy.deepcopy(bad)
    legacy['schema_version'] = 1
    for row in legacy['entries']:
        row.pop('cut')
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy((original, good, bad, legacy))
    assert validate_snapshot(original) == original
    assert validate_snapshot(legacy) == legacy  # V1 compatibility, no new invariant.
    assert diff_artifacts(original, good, fail_on_loss=True)['loss_policy']['passed']
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)
    for left, right in ((good, bad), (bad, good), (bad, bad), (legacy, bad), (bad, legacy)):
        for options in ({}, dict(fail_on_loss=True)):
            with pytest.raises(ScopeError, match='invalid snapshot'):
                diff_artifacts(left, right, **options)
    assert (original, good, bad, legacy) == frozen


@pytest.mark.parametrize('position', ['before', 'after'])
def test_unpaired_dual_global_checked_before_projection(tree, isolated_codex_home, monkeypatch, position):
    from agentwrench.agentsmd.core import ScopeError
    from agentwrench.agentsmd.snapshot import diff_artifacts
    root, cwd = tree
    put(isolated_codex_home/'AGENTS.override.md', 'WIN')
    put(isolated_codex_home/'AGENTS.md', 'LOW')
    bad = document(capture(root, isolated_codex_home, root, cwd, options=['--snapshot-version', 2]))
    other = copy.deepcopy(bad)
    other['directories'].pop()  # No comparison/equality loop would visit the invalid nested row.
    dual_loaded(bad['directories'][-1]['snapshot'])
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_projection(*args):
        pytest.fail('projected original artifacts before validating all nested snapshots')
    monkeypatch.setattr('agentwrench.agentsmd.snapshot.project_manifest', no_projection)
    frozen = copy.deepcopy((bad, other))
    left, right = (bad, other) if position == 'before' else (other, bad)
    for flags in ({}, dict(include_file_changes=True), dict(fail_on_loss=True),
                  dict(cwd_map={'.': '.'}, include_file_changes=True, fail_on_loss=True)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right, **flags)
    assert (bad, other) == frozen
