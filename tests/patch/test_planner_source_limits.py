"""Planner snapshot allowances are independent of content/result/output limits."""
import io
from pathlib import Path

import pytest

from agentwrench.patch import PatchError, apply, load_plan, parse, plan, preview
import agentwrench.patch.engine as engine
import agentwrench.patch.transaction as transaction
from agentwrench.patch.engine import MAX_FILE_BYTES, MAX_TOTAL_BYTES


def patch(body):
    return parse('*** Begin Patch\n' + body + '*** End Patch\n')


def deletes(names):
    return patch(''.join(f'*** Delete File: {name}\n' for name in names))


def tree(root):
    import hashlib
    return {str(p.relative_to(root)): (p.stat().st_mode,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in root.rglob('*')}


def spy_reads(monkeypatch, root, substitutes=None):
    original = Path.open
    reads = []

    class Reader:
        def __init__(self, handle, name):
            self.handle, self.name = handle, name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size=-1):
            data = self.handle.read(size)
            reads.append((self.name, size, len(data)))
            return data

    def opened(path, mode='r', *args, **kwargs):
        if path.parent == root and mode == 'rb':
            handle = (io.BytesIO(substitutes[path.name])
                      if substitutes and path.name in substitutes
                      else original(path, mode, *args, **kwargs))
            return Reader(handle, path.name)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', opened)
    return reads


def forbid(*args, **kwargs):
    pytest.fail('refusal must precede content processing or staging')


def test_nine_real_sources_stop_before_ninth_open(tmp_path, monkeypatch):
    data = b'x' * (MAX_FILE_BYTES - 1) + b'\n'
    for i in range(9):
        (tmp_path / f'old{i}').write_bytes(data)
    request = deletes([f'old{i}' for i in range(9)])
    assert len(request.to_text().encode()) == 228
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    monkeypatch.setattr(engine.TextFile, 'read', forbid)
    with pytest.raises(PatchError, match='total source data exceeds 64 MiB limit'):
        plan(request, tmp_path)
    assert reads == [(f'old{i}', MAX_FILE_BYTES + 1, MAX_FILE_BYTES) for i in range(8)]
    print({'patchBytes': 228, 'reads': reads, 'sourceBytesRead': sum(n for _, _, n in reads)})
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'equal', 'above'])
@pytest.mark.parametrize('mixed', [False, True], ids=['deletes', 'mixed'])
def test_actual_64_mib_boundary(tmp_path, monkeypatch, delta, mixed):
    data = b'x' * (MAX_FILE_BYTES - 1) + b'\n'
    for i in range(8):
        (tmp_path / f'old{i}').write_bytes(data if i < 7 or not mixed
                                              else b'old\n' + data[8:])
    # Mixed snapshot total: seven full files, one four bytes shorter, and extra.
    extra = 4 + delta if mixed else max(delta, 0)
    if not mixed and delta < 0:
        (tmp_path / 'old7').write_bytes(data[1:])
    (tmp_path / 'extra').write_bytes(b'\n' * extra)
    body = ''.join(f'*** Delete File: old{i}\n' for i in range(7 if mixed else 8))
    if mixed:
        body += ('*** Update File: old7\n*** Move to: nested/moved\n@@\n-old\n+new\n'
                 '*** Add File: created\n')
    request = patch(body + '*** Delete File: extra\n')
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    if delta > 0:
        monkeypatch.setattr(engine.TextFile, 'read', forbid)
        with pytest.raises(PatchError, match='total source data exceeds 64 MiB limit'):
            plan(request, tmp_path)
    else:
        prepared = plan(request, tmp_path)
        assert prepared.ok
        assert sum(len(s.data or b'') for s in prepared.snapshots.values()) == MAX_TOTAL_BYTES + delta
        if mixed:
            assert prepared.snapshots['nested/moved'].data is None
            assert prepared.changes[7].after.startswith(b'new\n')
            assert prepared.changes[8].after == b''
    assert sum(n for _, _, n in reads) == MAX_TOTAL_BYTES + (delta if delta <= 0 else (-4 if mixed else 0))
    assert [name for name, _, _ in reads] == [f'old{i}' for i in range(8)] + ([] if delta > 0 else ['extra'])
    assert reads[-1][1] == (MAX_FILE_BYTES + 1 if delta > 0 else 5 if mixed else 1 if delta == 0 else 2)
    print({'delta': delta, 'mixed': mixed, 'reads': reads})
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_actual_per_file_boundary(tmp_path, monkeypatch, delta):
    data = b'x' * (MAX_FILE_BYTES + delta - 1) + b'\n'
    (tmp_path / 'old').write_bytes(data)
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    if delta > 0:
        with pytest.raises(PatchError, match='old: file exceeds 8 MiB limit'):
            plan(deletes(['old']), tmp_path)
        assert reads == []
    else:
        prepared = plan(deletes(['old']), tmp_path)
        assert prepared.ok and prepared.snapshots['old'].data == data
        assert reads == [('old', MAX_FILE_BYTES + 1, len(data))]
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('kind', ['add', 'move'])
@pytest.mark.parametrize('delta', [0, 1])
def test_existing_destination_counts_before_conflict_diagnosis(tmp_path, monkeypatch, kind, delta):
    (tmp_path / 'source').write_bytes(b'old\n')
    (tmp_path / 'target').write_bytes(b'\xef\xbb\xbf\xc3\xa9\r\n')
    (tmp_path / 'source').chmod(0o751)
    (tmp_path / 'target').chmod(0o640)
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 11 - delta)
    body = ('*** Delete File: source\n*** Add File: target\n+new\n' if kind == 'add' else
            '*** Update File: source\n*** Move to: target\n@@\n-old\n+new\n')
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    if delta:
        with pytest.raises(PatchError, match='total source data exceeds'):
            plan(patch(body), tmp_path)
        assert reads == [('source', 11, 4)]
    else:
        prepared = plan(patch(body), tmp_path)
        assert not prepared.ok
        assert prepared.diagnostics[-1]['code'] == ('ADD_TARGET_EXISTS' if kind == 'add' else 'MOVE_TARGET_EXISTS')
        assert prepared.snapshots['source'] == engine.Snapshot(b'old\n', 0o751)
        assert prepared.snapshots['target'] == engine.Snapshot(b'\xef\xbb\xbf\xc3\xa9\r\n', 0o640)
        assert reads == [('source', 12, 4), ('target', 8, 7)]
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('budget', ['file', 'aggregate', 'zero'])
@pytest.mark.parametrize('overflow', [False, True])
def test_post_stat_growth_capped_and_actual_bytes_counted(tmp_path, monkeypatch, budget, overflow):
    (tmp_path / 'first').write_bytes(b'first\n')
    (tmp_path / 'growing').touch()  # stat underreports the stream observed at open
    (tmp_path / 'later').touch()
    allowance = MAX_FILE_BYTES if budget == 'file' else 7 if budget == 'aggregate' else 0
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 6 + allowance)
    data = b'x' * allowance if not overflow else b'x' * (allowance + 100)
    reads = spy_reads(monkeypatch, tmp_path, {'growing': data})
    decoded = []
    original = engine.TextFile.read
    def decode(data, path):
        decoded.append(path)
        return original(data, path)
    monkeypatch.setattr(engine.TextFile, 'read', decode)
    if overflow:
        reason = 'growing: file exceeds 8 MiB limit' if budget == 'file' else 'total source data exceeds 64 MiB limit'
        with pytest.raises(PatchError, match=reason):
            plan(deletes(['first', 'growing', 'later']), tmp_path)
        assert decoded == []
    else:
        prepared = plan(deletes(['first', 'growing', 'later']), tmp_path)
        assert prepared.ok and prepared.snapshots['growing'].data == data
        assert decoded == ['first', 'growing', 'later']
    expected = [('first', min(MAX_FILE_BYTES, 6 + allowance) + 1, 6),
                ('growing', allowance + 1, allowance + 1 if overflow else allowance)]
    assert reads == expected + ([] if overflow else [('later', 1, 0)])


@pytest.mark.parametrize('route', ['plan', 'apply', 'dry-run', 'preview', 'saved-regeneration'])
def test_shared_routes_refuse_before_staging(tmp_path, monkeypatch, route):
    (tmp_path / 'source').write_bytes(b'old\n')
    request = patch('*** Add File: nested/created\n+new\n*** Delete File: source\n')
    # Saved preflight passes real bytes, then the planner sees post-stat growth.
    saved = preview(request, tmp_path) if route == 'saved-regeneration' else None
    before = tree(tmp_path)
    monkeypatch.setattr(transaction, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    if saved:
        original = Path.open
        count = 0
        def opened(path, mode='r', *args, **kwargs):
            nonlocal count
            if path == tmp_path / 'source' and mode == 'rb':
                count += 1
                if count == 2:
                    return io.BytesIO(b'x' * (MAX_FILE_BYTES + 100))
            return original(path, mode, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', opened)
    else:
        monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 3)
    with pytest.raises(PatchError, match='file exceeds 8 MiB' if saved else 'total source data exceeds'):
        if route == 'plan':
            plan(request, tmp_path)
        elif route in ('apply', 'dry-run'):
            apply(request, tmp_path, dry_run=route == 'dry-run')
        elif route == 'preview':
            preview(request, tmp_path)
        else:
            load_plan(saved, tmp_path)
    monkeypatch.undo()
    assert tree(tmp_path) == before


def test_snapshot_error_order_and_independent_result_gate(tmp_path, monkeypatch):
    (tmp_path / 'bad-text').write_bytes(b'\xff')
    (tmp_path / 'excess').write_bytes(b'xxxx')
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 4)
    reads = spy_reads(monkeypatch, tmp_path)
    # Source-budget refusal precedes content decoding and missing-source diagnosis.
    with pytest.raises(PatchError, match='total source data exceeds'):
        plan(deletes(['missing', 'bad-text', 'excess', 'later']), tmp_path)
    assert reads == [('bad-text', 5, 1)]
    monkeypatch.undo()
    (tmp_path / 'bad-text').write_bytes(b'old\n')
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 4)
    with pytest.raises(PatchError, match='total result data exceeds'):
        plan(patch('*** Update File: bad-text\n@@\n-old\n+longer\n'), tmp_path)


def test_path_safety_and_per_file_error_precedence(tmp_path, monkeypatch):
    (tmp_path / 'old').write_bytes(b'oversize')
    (tmp_path / 'link').symlink_to(tmp_path / 'old')
    monkeypatch.setattr(engine, 'MAX_FILE_BYTES', 4)
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 2)
    reads = spy_reads(monkeypatch, tmp_path)
    with pytest.raises(PatchError, match='symlink'):
        plan(deletes(['link', 'old']), tmp_path)
    with pytest.raises(PatchError, match='old: file exceeds 8 MiB limit'):
        plan(deletes(['old', 'link']), tmp_path)
    assert reads == []


@pytest.mark.parametrize('route', [
    ['apply-plan'], ['apply-plan', '--dry-run'], ['apply-plan', '--check-only'],
    ['check-plan', '--json']])
def test_saved_cli_regeneration_retains_invalid_plan_category(tmp_path, monkeypatch, capsys, route):
    import json
    import agentwrench.patch.cli as cli
    (tmp_path / 'source').write_bytes(b'old\n')
    saved = preview(deletes(['source']), tmp_path)
    input_file = tmp_path / 'saved.json'
    input_file.write_text(saved)
    before = tree(tmp_path)
    # Saved preflight uses its unchanged limits; only regenerated planning refuses.
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 3)
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    assert cli.main([route[0], str(input_file), '--root', str(tmp_path), *route[1:]]) == 2
    output = capsys.readouterr()
    if '--check-only' in route or '--json' in route:
        doc = json.loads(output.out)
        assert not output.err and not doc['ok'] and not doc['diff_verified']
        assert doc['diagnostics'][0]['codes'] == ['INVALID_PLAN']
        reason = doc['diagnostics'][0]['reason']
    else:
        assert not output.out
        reason = output.err
    assert 'total source data exceeds 64 MiB limit' in reason
    monkeypatch.undo()
    assert tree(tmp_path) == before


def test_bom_crlf_snapshot_and_actual_growth_leave_next_allowance(tmp_path, monkeypatch):
    original = b'\xef\xbb\xbf\xc3\xa9\r\n'
    (tmp_path / 'source').touch()
    (tmp_path / 'empty').touch()
    (tmp_path / 'later').write_bytes(b'end\n')
    (tmp_path / 'source').chmod(0o751)
    monkeypatch.setattr(engine, 'MAX_TOTAL_BYTES', 11)
    reads = spy_reads(monkeypatch, tmp_path, {'source': original})
    prepared = plan(patch('*** Update File: source\n*** Move to: nested/moved\n@@\n é\n'
                          '*** Delete File: empty\n*** Delete File: later\n'), tmp_path)
    assert prepared.ok and prepared.changes[0].before == prepared.changes[0].after == original
    assert prepared.changes[0].mode == 0o751
    assert prepared.snapshots['nested/moved'].data is None
    assert reads == [('source', 12, 7), ('empty', 5, 0), ('later', 5, 4)]
