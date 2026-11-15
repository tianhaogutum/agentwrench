"""Commit snapshot bounds, independent of planner and saved hash-read budgets."""
import hashlib
import builtins
from pathlib import Path

import pytest

from agentwrench.patch import ApplyError, commit, parse, plan
import agentwrench.patch.transaction as transaction
from agentwrench.patch.engine import MAX_FILE_BYTES

REASON = 'original bytes, permissions or absence no longer match'


def request(body):
    return parse('*** Begin Patch\n' + body + '*** End Patch\n')


def tree(root):
    def digest(path):
        # Evidence reads must not enter the source-read spy during injection.
        with builtins.open(path, 'rb') as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    return {str(p.relative_to(root)): (p.lstat().st_mode,
            digest(p) if p.is_file() else None)
            for p in root.rglob('*') if not any(s.startswith('.v4a-') for s in p.parts)}


def spy_reads(monkeypatch, root, before_open=None, after_read=None):
    original = Path.open
    reads = []

    class Reader:
        def __init__(self, handle, path):
            self.handle, self.path = handle, path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size=-1):
            data = self.handle.read(size)
            reads.append((str(self.path.relative_to(root)), size, len(data)))
            if after_read:
                after_read(self.path)
            return data

    def opened(path, mode='r', *args, **kwargs):
        if (path.is_relative_to(root) and mode == 'rb'
                and not any(s.startswith('.v4a-') for s in path.relative_to(root).parts)):
            if before_open:
                before_open(path)
            return Reader(original(path, mode, *args, **kwargs), path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', opened)
    return reads


def forbid(*args, **kwargs):
    pytest.fail('initial drift/dry-run must never stage')


def assert_stale(prepared, path, *, dry=False, final=False):
    prefix = 'commit failed; changes rolled back: ' if final else ''
    with pytest.raises(ApplyError) as caught:
        commit(prepared, dry_run=dry)
    assert str(caught.value) == prefix + f'tree changed since planning: {path}: {REASON}'
    assert caught.value.diagnostics == []


@pytest.mark.parametrize('dry', [False, True])
def test_empty_snapshot_grown_to_real_8_mib_never_opened(tmp_path, monkeypatch, dry):
    (tmp_path / 'empty').touch()
    prepared = plan(request('*** Delete File: empty\n'), tmp_path)
    (tmp_path / 'empty').write_bytes(b'x' * (MAX_FILE_BYTES - 1) + b'\n')
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    assert_stale(prepared, 'empty', dry=dry)
    assert reads == []
    monkeypatch.undo()
    assert tree(tmp_path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['empty']


@pytest.mark.parametrize('phase', ['initial', 'dry-run', 'final'])
@pytest.mark.parametrize('drift', ['grow', 'shrink', 'bytes', 'mode', 'missing', 'appeared'])
def test_stale_contracts_and_order(tmp_path, monkeypatch, phase, drift):
    (tmp_path / 'first').write_bytes(b'first\n')
    (tmp_path / 'source').write_bytes(b'old\n')
    (tmp_path / 'source').chmod(0o751)
    prepared = plan(request('*** Delete File: first\n*** Delete File: source\n'
                            '*** Add File: nested/new\n+created\n'), tmp_path)
    changed = None

    def mutate():
        nonlocal changed
        source = tmp_path / 'source'
        if drift in ('grow', 'shrink', 'bytes'):
            source.write_bytes({'grow': b'longer\n', 'shrink': b'', 'bytes': b'NEW\n'}[drift])
        elif drift == 'mode':
            source.chmod(0o600)
        elif drift == 'missing':
            source.unlink()
        else:
            (tmp_path / 'nested').mkdir()
            (tmp_path / 'nested/new').write_bytes(b'writer\n')
        changed = tree(tmp_path)

    if phase == 'final':
        chmod = Path.chmod
        def staged(path, *args, **kwargs):
            result = chmod(path, *args, **kwargs)
            if path.name == 'new-2':
                assert (path.parent / 'recovery.json').exists()
                mutate()  # after all new bytes and backups have been staged
            return result
        monkeypatch.setattr(Path, 'chmod', staged)
    else:
        mutate()
        monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    reads = spy_reads(monkeypatch, tmp_path)
    assert_stale(prepared, 'nested/new' if drift == 'appeared' else 'source',
                 dry=phase == 'dry-run', final=phase == 'final')
    per_pass = [('first', 7, 6)]
    if drift in ('bytes', 'appeared'):
        per_pass.append(('source', 5, 4))
    assert reads == ([('first', 7, 6), ('source', 5, 4)] if phase == 'final' else []) + per_pass
    monkeypatch.undo()
    assert tree(tmp_path) == changed
    assert not list(tmp_path.glob('.v4a-*'))


@pytest.mark.parametrize('original', [b'', b'old\n', b'\xef\xbb\xbf\xc3\xa9\r\n'])
@pytest.mark.parametrize('phase', ['initial', 'dry-run', 'final'])
@pytest.mark.parametrize('stream', ['grow-at-open', 'underreported-size', 'shrink-at-open'])
def test_stat_matching_stream_drift_is_bounded(tmp_path, monkeypatch, original, phase, stream):
    if stream == 'shrink-at-open' and not original:
        pytest.skip('empty stream cannot shrink')
    path = tmp_path / 'source'
    path.write_bytes(original)
    prepared = plan(request('*** Delete File: source\n'), tmp_path)
    actual = original[:-1] if stream == 'shrink-at-open' else original + b'x' * 100
    armed = phase != 'final'
    if phase == 'final':
        chmod = Path.chmod
        def staged(p, *args, **kwargs):
            nonlocal armed
            result = chmod(p, *args, **kwargs)
            if p.name == 'backup-0':
                armed = True
                if stream == 'underreported-size':
                    path.write_bytes(actual)
            return result
        monkeypatch.setattr(Path, 'chmod', staged)
    else:
        monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    if stream == 'underreported-size':
        # Real larger file, but every stat reports the expected size.
        stat = Path.stat
        def underreport(p, *args, **kwargs):
            info = stat(p, *args, **kwargs)
            if p == path and armed:
                values = list(info)
                values[6] = len(original)
                return type(info)(values)
            return info
        monkeypatch.setattr(Path, 'stat', underreport)
        if armed:
            path.write_bytes(actual)
    def grow(p):
        if armed and stream != 'underreported-size':
            p.write_bytes(actual)
    reads = spy_reads(monkeypatch, tmp_path, before_open=grow)
    assert_stale(prepared, 'source', dry=phase == 'dry-run', final=phase == 'final')
    expected = [('source', len(original) + 1, min(len(actual), len(original) + 1))]
    assert reads == ([('source', len(original) + 1, len(original))] if phase == 'final' else []) + expected
    monkeypatch.undo()
    assert path.read_bytes() == actual
    assert not list(tmp_path.glob('.v4a-*'))


@pytest.mark.parametrize('dry', [False, True])
def test_unchanged_exact_bytes_empty_absence_modes_and_pass_count(tmp_path, monkeypatch, dry):
    data = b'\xef\xbb\xbf\xc3\xa9\r\n'
    (tmp_path / 'source').write_bytes(data)
    (tmp_path / 'source').chmod(0o751)
    (tmp_path / 'empty').touch()
    prepared = plan(request('*** Update File: source\n*** Move to: nested/moved\n@@\n é\n'
                            '*** Delete File: empty\n*** Add File: created\n+note\n'), tmp_path)
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    if dry:
        monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    assert commit(prepared, dry_run=dry) is prepared
    assert reads == [('source', len(data) + 1, len(data)), ('empty', 1, 0)] * (1 if dry else 2)
    monkeypatch.undo()
    if dry:
        assert tree(tmp_path) == before
    else:
        assert sorted(tree(tmp_path)) == ['created', 'nested', 'nested/moved']
        assert (tmp_path / 'nested/moved').read_bytes() == data
        assert (tmp_path / 'nested/moved').stat().st_mode & 0o7777 == 0o751
        assert (tmp_path / 'created').read_bytes() == b'note\n'
        assert (tmp_path / 'created').stat().st_mode & 0o7777 == 0o644
    assert not list(tmp_path.glob('.v4a-*'))


def test_mode_change_during_read_still_rejects(tmp_path, monkeypatch):
    path = tmp_path / 'source'
    path.write_bytes(b'old\n')
    prepared = plan(request('*** Delete File: source\n'), tmp_path)
    reads = spy_reads(monkeypatch, tmp_path, after_read=lambda p: p.chmod(0o600))
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    assert_stale(prepared, 'source')
    assert reads == [('source', 5, 4)]


def test_validation_path_safety_and_io_error_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'source'
    path.write_bytes(b'old\n')
    prepared = plan(request('*** Delete File: source\n'), tmp_path)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    prepared.diagnostics.append(dict(status='failed'))
    path.unlink()
    path.symlink_to(tmp_path / 'missing')
    with pytest.raises(ApplyError, match='^patch validation failed; no files changed$'):
        commit(prepared)
    prepared.diagnostics.clear()
    with pytest.raises(ApplyError, match='^tree changed since planning: source: symlink paths are forbidden: source$'):
        commit(prepared)
    path.unlink()
    path.write_bytes(b'old\n')
    opened = Path.open
    def fail(p, mode='r', *args, **kwargs):
        if p == path and mode == 'rb':
            raise OSError('injected read failure')
        return opened(p, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(ApplyError, match='^tree changed since planning: source: injected read failure$'):
        commit(prepared)
