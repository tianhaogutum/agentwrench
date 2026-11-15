"""Saved-plan checks aggregate drift without applying or staging any changes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agentwrench.patch import parse, preview


PATCH = '''*** Begin Patch
*** Update File: one
@@
-old
+new
*** Delete File: two
*** Update File: old-name
*** Move to: nested/renamed
@@
 keep
*** Add File: nested/new
+created
*** End Patch
'''


def fixture(root):
    for name, data, mode in [('one', b'old\n', 0o640), ('two', b'bye\n', 0o644),
                             ('old-name', b'keep\n', 0o750)]:
        (root / name).write_bytes(data)
        (root / name).chmod(mode)
    return preview(parse(PATCH), root)


def tree(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode,
            p.readlink().as_posix() if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


@pytest.fixture(params=[('check-plan', '--json'), ('apply-plan', '--check-only')],
                ids=['check-plan', 'apply-plan-check-only'])
def check_args(request):
    return request.param


@pytest.fixture
def checker(check_args):
    def run(root, text, expected=0, *, plan_file=None):
        return check(root, text, expected, command=check_args, plan_file=plan_file)
    return run


def check(root, text, expected=0, *, command, plan_file=None):
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'patch', command[0], str(plan_file) if plan_file else '-',
                             '--root', str(root), command[1]],
                            input=None if plan_file else text.encode() if isinstance(text, str) else text, capture_output=True)
    assert result.returncode == expected, (result.stdout, result.stderr)
    assert result.stderr == b''
    doc = json.loads(result.stdout)
    assert set(doc) == {'format', 'version', 'ok', 'diff_verified', 'diagnostics'}
    assert doc['format'] == 'v4a-plan-check' and doc['version'] == 1
    assert doc['ok'] is (expected == 0)
    return doc


def test_all_stale_paths_and_both_byte_mode_mismatches(tmp_path, checker):
    doc = json.loads(fixture(tmp_path))
    doc['preconditions'] = dict(reversed(list(doc['preconditions'].items())))
    saved = json.dumps(doc)
    expected = doc['preconditions']
    (tmp_path / 'one').write_bytes(b'old\nextra\n')  # Old hunk still matches.
    (tmp_path / 'one').chmod(0o600)
    (tmp_path / 'two').unlink()
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/renamed').write_bytes(b'occupied\n')
    (tmp_path / 'nested/new').touch()  # Empty is present, not absent.
    before = tree(tmp_path)
    doc = checker(tmp_path, saved, expected=1)
    assert not doc['diff_verified']
    assert [d['path'] for d in doc['diagnostics']] == ['one', 'two', 'nested/renamed', 'nested/new']
    by_path = {d['path']: d for d in doc['diagnostics']}
    assert by_path['one']['codes'] == ['BYTES_MISMATCH', 'MODE_MISMATCH']
    for name in ('two', 'nested/renamed', 'nested/new'):
        assert by_path[name]['codes'] == ['ABSENCE_MISMATCH']
    for name, d in by_path.items():
        assert set(d) == {'path', 'codes', 'expected', 'actual', 'reason'}
        assert d['expected'] == expected[name]
    assert by_path['one']['actual'] == {
        'sha256': hashlib.sha256(b'old\nextra\n').hexdigest(), 'mode': '0600'}
    assert by_path['two']['actual'] == {'sha256': None, 'mode': None}
    assert by_path['nested/new']['actual']['sha256'] == hashlib.sha256(b'').hexdigest()
    assert tree(tmp_path) == before


def test_valid_restored_plan_root_alias_unrelated_changes_and_file_input(tmp_path, checker):
    root = tmp_path / 'root'
    root.mkdir()
    saved = fixture(root)
    (root / 'unrelated').write_bytes(b'not bound\n')
    before = tree(root)
    assert checker(root, saved) == {'format': 'v4a-plan-check', 'version': 1, 'ok': True,
                                 'diff_verified': True, 'diagnostics': []}
    (root / 'one').write_bytes(b'drift\n')
    assert checker(root, saved, expected=1)['diagnostics'][0]['codes'] == ['BYTES_MISMATCH']
    (root / 'one').write_bytes(b'old\n')
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    plan_file = tmp_path / 'saved.json'
    plan_file.write_text(saved)
    assert checker(alias, None, plan_file=plan_file)['diff_verified']
    assert tree(root) == before


@pytest.mark.parametrize('kind', ['symlink', 'broken-symlink', 'parent-symlink', 'directory', 'fifo', 'parent-file'])
def test_unsafe_path_does_not_hide_later_drift(tmp_path, kind, checker):
    root = tmp_path / 'root'
    root.mkdir()
    saved = fixture(root)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside\n')
    if kind in ('symlink', 'broken-symlink', 'directory', 'fifo'):
        (root / 'one').unlink()
        if kind in ('symlink', 'broken-symlink'):
            (root / 'one').symlink_to(outside if kind == 'symlink' else tmp_path / 'absent')
        elif kind == 'fifo':
            import os
            os.mkfifo(root / 'one')
        else:
            (root / 'one').mkdir()
        unsafe = ['one']
    else:
        if kind == 'parent-symlink':
            (root / 'nested').symlink_to(tmp_path, target_is_directory=True)
        else:
            (root / 'nested').write_bytes(b'parent is a file\n')
        unsafe = ['nested/renamed', 'nested/new']
    (root / 'two').write_bytes(b'\x00\xff')  # Compare raw bytes; no UTF-8 decoding.
    (root / 'old-name').chmod(0o700)
    before = tree(root)
    diagnostics = {d['path']: d for d in checker(root, saved, expected=1)['diagnostics']}
    for name in unsafe:
        assert diagnostics[name]['codes'] == ['UNSAFE_PATH']
        assert diagnostics[name]['actual'] is None
    assert diagnostics['two']['codes'] == ['BYTES_MISMATCH']
    assert diagnostics['old-name']['codes'] == ['MODE_MISMATCH']
    assert tree(root) == before and outside.read_bytes() == b'outside\n'


@pytest.mark.parametrize('damage', ['json', 'utf8', 'version', 'duplicate-key', 'missing-path',
                                    'extra-path', 'unsafe-path', 'patch', 'diff'])
def test_invalid_plan_is_json_even_before_tree_scan(tmp_path, damage, checker):
    doc = json.loads(fixture(tmp_path))
    if damage == 'json':
        saved = b'{'
    elif damage == 'utf8':
        saved = b'\xff'
    elif damage == 'duplicate-key':
        saved = json.dumps(doc).replace('"version": 1', '"version": 1, "version": 1')
    else:
        if damage == 'version':
            doc['version'] = True
        elif damage == 'missing-path':
            del doc['preconditions']['two']
        elif damage in ('extra-path', 'unsafe-path'):
            doc['preconditions']['extra' if damage == 'extra-path' else '../escape'] = {'sha256': None, 'mode': None}
        elif damage == 'patch':
            doc['patch'] = 'invalid'
        else:
            doc['diff'] += 'unreviewed\n'
        saved = json.dumps(doc)
    if damage != 'diff':
        (tmp_path / 'two').unlink()  # Invalid schema must not masquerade as drift.
    before = tree(tmp_path)
    doc = checker(tmp_path, saved, expected=2)
    assert not doc['diff_verified']
    assert doc['diagnostics'][0]['codes'] == ['INVALID_PLAN']
    assert tree(tmp_path) == before


def test_wrong_and_missing_roots_and_input_io(tmp_path, checker):
    root = tmp_path / 'root'
    root.mkdir()
    saved = fixture(root)
    d, = checker(tmp_path, saved, expected=1)['diagnostics']
    assert d['path'] is None and d['codes'] == ['ROOT_MISMATCH']
    for path in root.iterdir():
        path.unlink()
    root.rmdir()
    d, = checker(root, saved, expected=1)['diagnostics']
    assert d['codes'] == ['ROOT_UNAVAILABLE']
    result = checker(root, None, expected=1, plan_file=tmp_path / 'missing.json')
    assert result['diagnostics'][0]['codes'] == ['IO_ERROR']


def test_never_calls_commit_or_staging(tmp_path, monkeypatch, capsys, check_args):
    import agentwrench.patch.cli as cli
    import agentwrench.patch.transaction as transaction
    saved = tmp_path / 'saved.json'
    saved.write_text(fixture(tmp_path))
    def forbidden(*args, **kwargs):
        pytest.fail('saved-plan checks must never commit or stage files')
    monkeypatch.setattr(cli, 'commit', forbidden)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbidden)
    before = tree(tmp_path)
    args = [check_args[0], str(saved), '--root', str(tmp_path), check_args[1]]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['diff_verified']
    assert tree(tmp_path) == before
    (tmp_path / 'two').unlink()
    before = tree(tmp_path)
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['codes'] == ['ABSENCE_MISMATCH']
    assert tree(tmp_path) == before
    saved.write_text('{}')
    before = tree(tmp_path)
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert captured.err == '' and json.loads(captured.out)['diagnostics'][0]['codes'] == ['INVALID_PLAN']
    assert tree(tmp_path) == before


def test_read_error_continues_to_later_paths(tmp_path, monkeypatch):
    from agentwrench.patch import check_plan
    saved = fixture(tmp_path)
    (tmp_path / 'two').unlink()
    before = tree(tmp_path)
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == tmp_path / 'one':
            raise PermissionError('injected read denial')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    first, second = check_plan(saved, tmp_path)['diagnostics']
    assert first['path'] == 'one' and first['codes'] == ['IO_ERROR'] and first['actual'] is None
    assert second['path'] == 'two' and second['codes'] == ['ABSENCE_MISMATCH']
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('budget', ['file', 'total'])
def test_resource_limit_continues_to_later_paths(tmp_path, monkeypatch, budget):
    import agentwrench.patch.planfile as planfile
    saved = fixture(tmp_path)
    (tmp_path / 'two').write_bytes(b'\x00')
    (tmp_path / 'old-name').unlink()
    before = tree(tmp_path)
    if budget == 'file':
        monkeypatch.setattr(planfile, 'MAX_FILE_BYTES', 3)
    else:
        monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 3)
    doc = planfile.check_plan(saved, tmp_path)
    first, second, third = doc['diagnostics']
    assert first['path'] == 'one' and first['codes'] == ['RESOURCE_LIMIT'] and first['actual'] is None
    assert second['path'] == 'two' and second['codes'] == ['BYTES_MISMATCH']
    assert third['path'] == 'old-name' and third['codes'] == ['ABSENCE_MISMATCH']
    assert not doc['ok'] and not doc['diff_verified'] and tree(tmp_path) == before


def test_stale_plan_does_not_claim_to_verify_a_tampered_diff(tmp_path, checker):
    doc = json.loads(fixture(tmp_path))
    doc['diff'] += 'tampered\n'
    (tmp_path / 'two').unlink()
    result = checker(tmp_path, json.dumps(doc), expected=1)
    assert not result['diff_verified']
    assert result['diagnostics'][0]['codes'] == ['ABSENCE_MISMATCH']


def test_plan_size_limit_and_cli_text_mode(tmp_path, monkeypatch, capsys):
    import agentwrench.patch.cli as cli
    saved = tmp_path / 'saved.json'
    saved.write_text(fixture(tmp_path))
    assert cli.main(['check-plan', str(saved), '--root', str(tmp_path)]) == 0
    assert capsys.readouterr().out == 'Plan is valid.\n'
    (tmp_path / 'two').unlink()
    assert cli.main(['check-plan', str(saved), '--root', str(tmp_path)]) == 1
    assert 'two: ABSENCE_MISMATCH' in capsys.readouterr().out
    # Plan input uses its 64 MiB budget, independently of the 8 MiB patch budget.
    monkeypatch.setattr(cli, 'MAX_FILE_BYTES', 1)
    assert cli.main(['check-plan', str(saved), '--root', str(tmp_path), '--json']) == 1
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['codes'] == ['ABSENCE_MISMATCH']
    monkeypatch.setattr(cli, 'MAX_PLAN_BYTES', 10)
    assert cli.main(['check-plan', str(saved), '--root', str(tmp_path), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['codes'] == ['INVALID_PLAN']


@pytest.mark.parametrize('path', ['one', 'two', 'old-name', 'nested/renamed', 'nested/new'])
def test_inconsistent_operation_preconditions_are_invalid_before_drift(tmp_path, path, checker):
    doc = json.loads(fixture(tmp_path))
    if path in ('nested/renamed', 'nested/new'):
        doc['preconditions'][path] = {'sha256': hashlib.sha256(b'').hexdigest(), 'mode': '0644'}
        (tmp_path / 'nested').mkdir()
        (tmp_path / path).touch()  # Even a matching empty file is invalid for a destination.
    else:
        doc['preconditions'][path] = {'sha256': None, 'mode': None}
        (tmp_path / path).unlink()  # Even matching absence is invalid for a source.
    (tmp_path / 'one').write_bytes(b'unrelated drift before later invalid entries\n')
    before = tree(tmp_path)
    result = checker(tmp_path, json.dumps(doc), expected=2)
    d, = result['diagnostics']
    assert d['codes'] == ['INVALID_PLAN'] and path in d['reason']
    assert d['path'] is None and d['expected'] is None and d['actual'] is None
    assert not result['diff_verified'] and tree(tmp_path) == before


def test_check_only_and_dry_run_are_mutually_exclusive(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'patch', 'apply-plan', '-',
                             '--root', str(tmp_path), '--check-only', '--dry-run'],
                            input=b'{}', capture_output=True)
    assert result.returncode == 2 and result.stdout == b''
    assert b'not allowed with argument' in result.stderr
    assert list(tmp_path.iterdir()) == []
