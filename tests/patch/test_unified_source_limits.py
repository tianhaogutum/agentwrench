"""Source-read budgets precede context construction; output limits are separate."""
from pathlib import Path

import pytest

from agentwrench.patch import PatchError, from_unified
from agentwrench.patch.engine import MAX_FILE_BYTES, MAX_TOTAL_BYTES
from agentwrench.patch.model import Patch
import agentwrench.patch.cli as cli
import agentwrench.patch.transaction as transaction
import agentwrench.patch.unified as unified


def rename(old, new):
    return (f'diff --git a/{old} b/{new}\nsimilarity index 100%\n'
            f'rename from {old}\nrename to {new}\n')


class ReadSpy:
    def __init__(self, handle, events, name):
        self.handle, self.events, self.name = handle, events, name

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.handle.close()

    def read(self, size=-1):
        data = self.handle.read(size)
        self.events.append((self.name, size, len(data)))
        return data


def spy_reads(monkeypatch, root, substitutes=None):
    import io
    original = Path.open
    events = []

    def opened(path, mode='r', *args, **kwargs):
        if path.parent == root and mode == 'rb' and path.name != 'input.diff':
            handle = (io.BytesIO(substitutes[path.name])
                      if substitutes and path.name in substitutes
                      else original(path, mode, *args, **kwargs))
            return ReadSpy(handle, events, path.name)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', opened)
    return events


def tree(root):
    import hashlib
    return {str(p.relative_to(root)): (p.stat().st_mode,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in root.rglob('*')}


@pytest.fixture(autouse=True)
def no_staging(monkeypatch):
    def forbid(*args, **kwargs):
        pytest.fail('import must not commit or stage')
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)


def test_nine_real_sources_stop_at_aggregate_budget(tmp_path, monkeypatch):
    data = b'x' * (MAX_FILE_BYTES - 1) + b'\n'
    for i in range(9):
        (tmp_path / f'old{i}').write_bytes(data)
    diff = ''.join(rename(f'old{i}', f'new{i}') for i in range(9))
    assert len(diff.encode()) == 711
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    with pytest.raises(PatchError, match='pure rename.*aggregate.*64 MiB.*source.*budget'):
        from_unified(diff, root=tmp_path)
    assert reads == [(f'old{i}', MAX_FILE_BYTES + 1, MAX_FILE_BYTES) for i in range(8)]
    assert sum(count for _, _, count in reads) == MAX_TOTAL_BYTES
    print({'inputBytes': 711, 'reads': reads, 'sourceBytesRead': MAX_TOTAL_BYTES})
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'equal', 'above'])
@pytest.mark.parametrize('mixed', [False, True], ids=['renames', 'with-hunks'])
def test_real_64_mib_boundary(tmp_path, monkeypatch, delta, mixed):
    data = b'x' * (MAX_FILE_BYTES - 1) + b'\n'
    for i in range(8):
        (tmp_path / f'old{i}').write_bytes(data[:-2] + b'\n' if i == 7 and delta < 0 else data)
    (tmp_path / 'extra').write_bytes(b'\n' if delta > 0 else b'')
    diff = ''.join(rename(f'old{i}', f'new{i}') for i in range(8)) + rename('extra', 'extra-new')
    if mixed:
        # Deliberately absent: ordinary hunks never read these source paths.
        diff = ('--- a/update\n+++ b/update\n@@ -1 +1 @@\n-old\n+new\n'
                '--- a/delete\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n' + diff +
                'diff --git a/add b/add\nnew file mode 100644\n'
                '--- /dev/null\n+++ b/add\n@@ -0,0 +1 @@\n+new\n')
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    decoded = []
    original_decode = unified.TextFile.read
    def decode(data, path):
        decoded.append(path)
        return original_decode(data, path)
    monkeypatch.setattr(unified.TextFile, 'read', decode)
    serialized = []
    original_text = Patch.to_text
    def serialize(patch):
        serialized.append(True)
        return original_text(patch)
    monkeypatch.setattr(Patch, 'to_text', serialize)
    reason = 'aggregate 64 MiB source byte budget' if delta > 0 else 'converted V4A patch exceeds'
    with pytest.raises(PatchError, match=reason):
        from_unified(diff, root=tmp_path)
    assert sum(n for _, _, n in reads) == MAX_TOTAL_BYTES + min(delta, 0)
    assert [name for name, _, _ in reads] == [f'old{i}' for i in range(8)] + ([] if delta > 0 else ['extra'])
    assert decoded == [name for name, _, _ in reads]
    assert serialized == ([] if delta > 0 else [True])
    assert all(0 < size <= MAX_FILE_BYTES + 1 for _, size, _ in reads)
    print({'sourceDelta': delta, 'mixed': mixed, 'reads': reads, 'serialized': bool(serialized)})
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0, 1])
@pytest.mark.parametrize('route', ['api', 'cli'])
def test_raw_byte_accounting_multiple_empty_bom_crlf(tmp_path, monkeypatch, capsys, delta, route):
    contents = {'first': b'\xef\xbb\xbf\xc3\xa9\r\n', 'empty': b'', 'last': b'end\n'}
    for name, data in contents.items():
        (tmp_path / name).write_bytes(data)
    total = sum(map(len, contents.values()))
    monkeypatch.setattr(unified, 'MAX_TOTAL_BYTES', total - delta)
    diff = ''.join(rename(name, name + '-new') for name in contents)
    # Rename with edits must not consume source bytes either.
    diff += ('diff --git a/edited b/edited-new\nrename from edited\nrename to edited-new\n'
             '--- a/edited\n+++ b/edited-new\n@@ -1 +1 @@\n-old\n+new\n')
    input_file = tmp_path / 'input.diff'
    input_file.write_text(diff)
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    if route == 'api':
        if delta > 0:
            with pytest.raises(PatchError, match='aggregate 64 MiB source byte budget'):
                from_unified(diff, root=tmp_path)
        else:
            result = from_unified(diff, root=tmp_path)
    else:
        assert cli.main(['from-unified', str(input_file), '--root', str(tmp_path)]) == (2 if delta > 0 else 0)
        output = capsys.readouterr()
        if delta > 0:
            assert not output.out and 'aggregate 64 MiB source byte budget' in output.err
            assert 'Traceback' not in output.err
        else:
            from agentwrench.patch import parse
            assert not output.err
            result = parse(output.out)
    expected_names = ['first', 'empty'] + ([] if delta > 0 else ['last'])
    assert [name for name, _, _ in reads] == expected_names
    if delta <= 0:
        assert sum(n for _, _, n in reads) == total
        assert [f.kind for f in result.files] == ['update', 'delete', 'add', 'update', 'update']
        assert result.files[0].hunks[0].lines == [(' ', 'é')]
        # Planning restores the source BOM/CRLF without any import normalization change.
        from agentwrench.patch import plan
        monkeypatch.undo()
        # Restore real aggregate budget before planning; provide ordinary-hunk source.
        (tmp_path / 'edited').write_bytes(b'old\n')
        expected = tree(tmp_path)
        prepared = plan(result, tmp_path)
        assert prepared.ok and prepared.changes[0].after == contents['first']
        assert tree(tmp_path) == expected
        (tmp_path / 'edited').unlink()
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('budget', ['file', 'aggregate'])
def test_stat_excess_rejected_before_open(tmp_path, monkeypatch, budget):
    limit = MAX_FILE_BYTES if budget == 'file' else 10
    (tmp_path / 'old').write_bytes(b'x' * limit + b'\n')
    if budget == 'aggregate':
        monkeypatch.setattr(unified, 'MAX_TOTAL_BYTES', limit)
    reads = spy_reads(monkeypatch, tmp_path)
    with pytest.raises(PatchError, match='8 MiB' if budget == 'file' else 'aggregate 64 MiB'):
        from_unified(rename('old', 'new'), root=tmp_path)
    assert not reads


@pytest.mark.parametrize('budget', ['file', 'aggregate', 'exhausted'])
def test_growth_after_stat_reads_only_allowance_plus_sentinel(tmp_path, monkeypatch, budget):
    (tmp_path / 'first').write_bytes(b'first\n')
    (tmp_path / 'old').write_bytes(b'')  # stat underreports the simulated stream
    limit = MAX_FILE_BYTES if budget == 'file' else 7 if budget == 'aggregate' else 0
    monkeypatch.setattr(unified, 'MAX_TOTAL_BYTES', 6 + limit)
    reads = spy_reads(monkeypatch, tmp_path, {'old': b'x' * (limit + 100)})
    decoded = []
    original = unified.TextFile.read
    def decode(data, path):
        decoded.append(path)
        return original(data, path)
    monkeypatch.setattr(unified.TextFile, 'read', decode)
    with pytest.raises(PatchError, match='8 MiB byte budget' if budget == 'file' else 'aggregate 64 MiB'):
        from_unified(rename('first', 'first-new') + rename('old', 'new') + rename('later', 'later-new'), root=tmp_path)
    assert reads == [('first', min(MAX_FILE_BYTES, 6 + limit) + 1, 6), ('old', limit + 1, limit + 1)]
    assert decoded == ['first']  # No context decoding of overflow or later source.


@pytest.mark.parametrize('data,reason', [
    (b'\xff\n', 'only UTF-8'), (b'\x00\n', 'binary/NUL'),
    (b'x\n' * 100001, 'source exceeds 100,000-line'),
    (b'unterminated', 'unterminated pure rename'),
    (b'\xef\xbb\xbf', 'empty BOM/executable'),
])
def test_existing_source_refusals(tmp_path, data, reason):
    (tmp_path / 'old').write_bytes(data)
    before = tree(tmp_path)
    with pytest.raises(PatchError, match=reason):
        from_unified(rename('old', 'new'), root=tmp_path)
    assert tree(tmp_path) == before
