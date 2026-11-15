from pathlib import Path
import os
import shutil

import pytest

from agentwrench.patch import ApplyError, PatchError, apply, commit, parse, plan
from agentwrench.patch.engine import seek
import agentwrench.patch.transaction as transaction

FIXTURES = Path(__file__).parent / 'fixtures'


def patch(body):
    return parse('*** Begin Patch\n' + body + '\n*** End Patch\n')


@pytest.mark.parametrize(('source', 'expected_tier'), [
    ('hello', 'exact'), ('hello \t', 'rstrip'), ('  hello ', 'trim'),
    (' “hello”— ', 'unicode')])
def test_matching_tiers(source, expected_tier, tmp_path):
    old = '"hello"-' if expected_tier == 'unicode' else 'hello'
    (tmp_path / 'x').write_text(source + '\n')
    p = plan(patch(f'*** Update File: x\n@@\n-{old}\n+new'), tmp_path)
    assert p.ok and p.diagnostics[0]['tier'] == expected_tier
    commit(p)
    assert (tmp_path / 'x').read_text() == 'new\n'


def test_exact_tier_beats_earlier_trimmed_match():
    assert seek([' x ', 'x'], ['x']).index == 1
    assert seek(['x'], ['x', 'y']) is None
    assert seek(['x'], ['x'], start=2) is None


def test_bom_crlf_fixture(tmp_path):
    shutil.copytree(FIXTURES / 'bom_tree', tmp_path, dirs_exist_ok=True)
    p = plan(parse((FIXTURES / 'bom.patch').read_text()), tmp_path)
    assert p.ok
    assert p.diagnostics[0]['diagnosis']['bom']
    assert p.diagnostics[0]['diagnosis']['crlf']
    commit(p)
    assert (tmp_path / 'app.js').read_bytes() == b'\xef\xbb\xbfconsole.log("after");\r\n'


def test_fuzzy_context_bytes_and_mixed_endings_preserved(tmp_path):
    original = b'\xef\xbb\xbf keep \t\r\nold\nlast\r'
    (tmp_path / 'x').write_bytes(original)
    apply(patch('*** Update File: x\n@@\n keep\n-old\n+new\n last'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'\xef\xbb\xbf keep \t\r\nnew\r\nlast\r'


def test_upstream_update_adds_final_newline(tmp_path):
    (tmp_path / 'x').write_bytes(b'a\nb')
    apply(patch('*** Update File: x\n@@\n-a\n+A\n b'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'A\nb\n'


def test_eof_prefers_tail_then_falls_back(tmp_path):
    assert seek(['a', 'b', 'a', 'b'], ['a', 'b'], eof=True).index == 2
    (tmp_path / 'x').write_text('alpha\nbeta\ngamma\ndelta\n')
    p = plan(patch('*** Update File: x\n@@\n-alpha\n+ALPHA\n beta\n*** End of File'), tmp_path)
    assert p.diagnostics[0]['eof_fallback']
    commit(p)
    assert (tmp_path / 'x').read_text() == 'ALPHA\nbeta\ngamma\ndelta\n'


def test_eof_does_not_search_before_cursor():
    assert seek(['a', 'b'], ['a'], start=1, eof=True) is None


def test_final_empty_context_sentinel(tmp_path):
    (tmp_path / 'x').write_text('old\n')
    apply(patch('*** Update File: x\n@@\n-old\n+new\n \n*** End of File'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'new\n'


def test_pure_append_and_empty_file(tmp_path):
    (tmp_path / 'x').write_text('a\n')
    apply(patch('*** Update File: x\n@@\n+b'), tmp_path)
    assert (tmp_path / 'x').read_text() == 'a\nb\n'
    (tmp_path / 'empty').touch()
    apply(patch('*** Update File: empty\n@@\n+x'), tmp_path)
    assert (tmp_path / 'empty').read_text() == 'x\n'


def test_anchor_and_multiple_hunks(tmp_path):
    (tmp_path / 'x').write_text('head\na\nb\nsecond\na\nb\n')
    p = patch('*** Update File: x\n@@ head\n-a\n+A\n b\n@@ second\n-a\n+Z\n b')
    apply(p, tmp_path)
    assert (tmp_path / 'x').read_text() == 'head\nA\nb\nsecond\nZ\nb\n'


def test_diagnostics_all_failing_hunks(tmp_path):
    (tmp_path / 'x').write_text('function\n    alpha = 1\nvalue = 8\n')
    p = plan(patch('*** Update File: x\n@@\n-\talpha = 2\n+new\n@@ missing\n-z\n+Z'), tmp_path)
    assert not p.ok and len(p.diagnostics) == 2
    assert p.diagnostics[0]['candidate']['start_line'] == 2
    assert p.diagnostics[0]['diagnosis']['context_drift']
    assert p.diagnostics[1]['reason'] == '@@ anchor not found'


def test_tab_indentation_diagnosis(tmp_path):
    (tmp_path / 'x').write_text('        alpha\n')
    p = plan(patch('*** Update File: x\n@@\n-\talpha\n+new'), tmp_path)
    assert p.diagnostics[0]['diagnosis']['indentation_tabs_vs_spaces']


def test_ambiguous_first_after_cursor(tmp_path):
    (tmp_path / 'x').write_text('a\na\n')
    p = plan(patch('*** Update File: x\n@@\n-a\n+A'), tmp_path)
    assert p.diagnostics[0]['diagnosis']['ambiguous_match']
    assert p.diagnostics[0]['matches'] == [1, 2]
    commit(p)
    assert (tmp_path / 'x').read_text() == 'A\na\n'


def test_atomic_second_of_three_fails_and_dry_run(tmp_path):
    (tmp_path / 'one').write_text('old\n')
    (tmp_path / 'two').write_text('actual\n')
    p = patch('*** Update File: one\n@@\n-old\n+new\n*** Update File: two\n@@\n-wrong\n+new\n*** Add File: sub/three\n+created')
    before = {f.name: f.read_bytes() for f in tmp_path.iterdir()}
    with pytest.raises(ApplyError):
        apply(p, tmp_path)
    assert {f.name: f.read_bytes() for f in tmp_path.iterdir()} == before
    good = patch('*** Add File: sub/three\n+created')
    apply(good, tmp_path, dry_run=True)
    assert not (tmp_path / 'sub').exists()


@pytest.mark.parametrize('body', ['*** Add File: x\n+new', '*** Delete File: absent',
                                '*** Update File: absent\n@@\n+x',
                                '*** Update File: x\n*** Move to: exists\n@@\n-old\n+new'])
def test_conflicting_files(body, tmp_path):
    (tmp_path / 'x').write_text('old\n')
    (tmp_path / 'exists').touch()
    with pytest.raises(ApplyError):
        apply(patch(body), tmp_path)
    assert (tmp_path / 'x').read_text() == 'old\n'


def test_move_add_delete_and_mode(tmp_path):
    (tmp_path / 'old').write_text('old\n')
    (tmp_path / 'old').chmod(0o755)
    (tmp_path / 'delete').write_text('bye\n')
    apply(patch('*** Update File: old\n*** Move to: nested/new\n@@\n-old\n+new\n*** Delete File: delete\n*** Add File: empty'), tmp_path)
    assert not (tmp_path / 'old').exists() and not (tmp_path / 'delete').exists()
    assert (tmp_path / 'nested/new').read_text() == 'new\n'
    assert (tmp_path / 'nested/new').stat().st_mode & 0o777 == 0o755
    assert (tmp_path / 'empty').read_bytes() == b''


def test_symlink_file_and_parent_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('old\n')
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    (root / 'file').symlink_to(outside / 'secret')
    for path in ('link/secret', 'file'):
        with pytest.raises(PatchError, match='symlink'):
            apply(patch(f'*** Delete File: {path}'), root)
    assert (outside / 'secret').read_text() == 'old\n'


def test_duplicate_path_rejected(tmp_path):
    with pytest.raises(PatchError, match='only once'):
        apply(patch('*** Add File: x\n+a\n*** Delete File: x'), tmp_path)


def test_stale_plan_rejected(tmp_path):
    (tmp_path / 'x').write_text('old\n')
    p = plan(patch('*** Update File: x\n@@\n-old\n+new'), tmp_path)
    (tmp_path / 'x').write_text('changed\n')
    with pytest.raises(ApplyError, match='changed since planning'):
        commit(p)
    assert (tmp_path / 'x').read_text() == 'changed\n'
    assert sorted(f.name for f in tmp_path.iterdir()) == ['x']


@pytest.mark.parametrize('dry_run', [False, True])
def test_stale_plan_rejected_before_staging(tmp_path, monkeypatch, dry_run):
    (tmp_path / 'x').write_text('old\n')
    prepared = plan(patch('*** Update File: x\n@@\n-old\n+new'), tmp_path)
    (tmp_path / 'x').chmod(0o600)
    def unexpected_staging(*args, **kwargs):
        pytest.fail('stale plans must be rejected without staging')
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', unexpected_staging)
    with pytest.raises(ApplyError, match='changed since planning'):
        commit(prepared, dry_run=dry_run)
    assert (tmp_path / 'x').read_bytes() == b'old\n'
    assert (tmp_path / 'x').stat().st_mode & 0o7777 == 0o600


def test_commit_io_failure_rolls_back_files_modes_directories(tmp_path, monkeypatch):
    (tmp_path / 'one').write_text('old\n')
    (tmp_path / 'one').chmod(0o750)
    (tmp_path / 'two').write_text('second\n')
    p = patch('*** Update File: one\n@@\n-old\n+new\n*** Add File: nested/new\n+created\n*** Delete File: two')
    original_unlink = Path.unlink
    def fail_delete(self, *args, **kwargs):
        if self == tmp_path / 'two':
            raise OSError('injected commit failure')
        return original_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_delete)
    with pytest.raises(ApplyError, match='rolled back'):
        apply(p, tmp_path)
    assert (tmp_path / 'one').read_text() == 'old\n'
    assert (tmp_path / 'one').stat().st_mode & 0o777 == 0o750
    assert (tmp_path / 'two').read_text() == 'second\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['one', 'two']


@pytest.mark.parametrize('content', [b'\xff', b'a\x00b'])
def test_non_text_fails_without_writes(tmp_path, content):
    (tmp_path / 'x').write_bytes(content)
    with pytest.raises(PatchError):
        apply(patch('*** Delete File: x'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == content


def test_rollback_failure_retains_recovery_backups(tmp_path, monkeypatch):
    import json
    (tmp_path / 'one').write_text('old\n')
    (tmp_path / 'two').write_text('second\n')
    p = patch('*** Update File: one\n@@\n-old\n+new\n*** Delete File: two')
    replace = os.replace
    unlink = Path.unlink
    def fail_restore(src, dst):
        if Path(src).name.startswith('backup-'):
            raise OSError('injected rollback failure')
        return replace(src, dst)
    def fail_delete(self, *args, **kwargs):
        if self == tmp_path / 'two':
            raise OSError('injected commit failure')
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(transaction.os, 'replace', fail_restore)
    monkeypatch.setattr(Path, 'unlink', fail_delete)
    with pytest.raises(ApplyError, match='ROLLBACK INCOMPLETE.*recovery data:'):
        apply(p, tmp_path)
    recovery = next(tmp_path.glob('.v4a-*'))
    manifest = json.loads((recovery / 'recovery.json').read_text())
    assert (recovery / manifest['one']['backup']).read_bytes() == b'old\n'


def test_cleanup_failure_after_success_is_warning(tmp_path, monkeypatch):
    def fail_cleanup(path):
        raise OSError('injected cleanup failure')
    monkeypatch.setattr(transaction.shutil, 'rmtree', fail_cleanup)
    prepared = apply(patch('*** Add File: x\n+new'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'new\n'
    assert prepared.ok
    assert prepared.diagnostics[-1]['status'] == 'warning'


def test_resource_boundaries(tmp_path, monkeypatch):
    import agentwrench.patch.engine as engine
    with pytest.raises(PatchError, match='matching work'):
        seek(['a'] * 10000, ['b'] * 4000)
    (tmp_path / 'x').write_bytes(b'x' * 10)
    monkeypatch.setattr(engine, 'MAX_FILE_BYTES', 5)
    with pytest.raises(PatchError, match='exceeds'):
        apply(patch('*** Delete File: x'), tmp_path)


def test_empty_sentinel_deletion_and_addition(tmp_path):
    (tmp_path / 'x').write_text('old\n')
    apply(patch('*** Update File: x\n@@\n-old\n-\n+new\n+\n*** End of File'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'new\n'


def test_final_empty_old_context_keeps_internal_new_blank(tmp_path):
    (tmp_path / 'x').write_text('old\n')
    apply(patch('*** Update File: x\n@@\n old\n \n+new\n*** End of File'), tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'old\n\nnew\n'
