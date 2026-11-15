"""Saved hash-preflight bounds; regeneration/output are independent gates."""
import hashlib
import io
import json
from pathlib import Path
import re

import pytest

from agentwrench.patch import ApplyError, check_plan, load_plan, parse, preview
import agentwrench.patch.planfile as planfile
import agentwrench.patch.transaction as transaction
from agentwrench.patch.engine import MAX_FILE_BYTES, MAX_TOTAL_BYTES


def tree(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in root.rglob('*')}


def document(root, names, *, absent=(), data=None):
    body = ''.join(f'*** Delete File: {name}\n' for name in names)
    body += ''.join(f'*** Add File: {name}\n' for name in absent)
    conditions = {}
    for name in names:
        target = root / name
        raw = data[name] if data and name in data else target.read_bytes()
        conditions[name] = dict(sha256=hashlib.sha256(raw).hexdigest(),
                                mode=f'{target.stat().st_mode & 0o7777:04o}')
    conditions.update({name: dict(sha256=None, mode=None) for name in absent})
    return json.dumps(dict(format='v4a-plan', version=1, root=str(root),
        patch='*** Begin Patch\n' + body + '*** End Patch\n', preconditions=conditions, diff=''))


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
            raw = self.handle.read(size)
            reads.append((self.name, size, len(raw)))
            return raw

    def opened(path, mode='r', *args, **kwargs):
        if path.parent == root and mode == 'rb':
            handle = (io.BytesIO(substitutes[path.name])
                      if substitutes and path.name in substitutes else original(path, mode, *args, **kwargs))
            return Reader(handle, path.name)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', opened)
    return reads


def forbid(*args, **kwargs):
    pytest.fail('refusal/read-only validation must precede regeneration or staging')


@pytest.fixture(autouse=True)
def no_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(transaction, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)


class ReachedRegeneration(Exception):
    pass


def reached(*args, **kwargs):
    raise ReachedRegeneration


def refuse(saved, root, path, reason):
    with pytest.raises(ApplyError, match='^' + re.escape(f'tree changed since planning: {path}: {reason}') + '$'):
        load_plan(saved, root)


def test_nine_real_sources_stop_before_ninth_open(tmp_path, monkeypatch):
    data = b'x' * (MAX_FILE_BYTES - 1) + b'\n'
    names = [f'old{i}' for i in range(9)]
    for name in names:
        (tmp_path / name).write_bytes(data)
    saved = document(tmp_path, names)
    assert len(json.loads(saved)['patch'].encode()) == 228
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    monkeypatch.setattr(planfile, 'plan', forbid)
    refuse(saved, tmp_path, 'old8', 'total source data exceeds 64 MiB limit')
    assert reads == [(name, MAX_FILE_BYTES + 1, MAX_FILE_BYTES) for name in names[:8]]
    print({'patchBytes': 228, 'reads': reads, 'sourceBytesRead': sum(n for _, _, n in reads)})
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'equal', 'above'])
def test_real_aggregate_boundary_independent_of_output(tmp_path, monkeypatch, delta):
    raw = b'x' * (MAX_FILE_BYTES - 1) + b'\n'
    names = [f'old{i}' for i in range(8)] + ['extra', 'empty']
    for i, name in enumerate(names[:8]):
        (tmp_path / name).write_bytes(raw if i < 7 else raw[4:])
    (tmp_path / 'extra').write_bytes(b'\n' * (4 + delta))
    (tmp_path / 'empty').touch()
    saved = document(tmp_path, names, absent=['created'])
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    monkeypatch.setattr(planfile, 'plan', reached if delta <= 0 else forbid)
    if delta > 0:
        refuse(saved, tmp_path, 'extra', 'total source data exceeds 64 MiB limit')
    else:
        # Empty saved diff is intentionally not used: preflight acceptance alone
        # need not satisfy independent regeneration/export/preview byte limits.
        with pytest.raises(ReachedRegeneration):
            load_plan(saved, tmp_path)
    expected = [(n, MAX_FILE_BYTES + 1, MAX_FILE_BYTES if i < 7 else MAX_FILE_BYTES - 4)
                for i, n in enumerate(names[:8])]
    if delta <= 0:
        expected += [('extra', 5, 4 + delta), ('empty', 1 - delta, 0)]
    assert reads == expected
    assert sum(n for _, _, n in reads) == MAX_TOTAL_BYTES + (delta if delta <= 0 else -4)
    print({'delta': delta, 'reads': reads})
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_real_file_boundary(tmp_path, monkeypatch, delta):
    raw = b'x' * (MAX_FILE_BYTES + delta - 1) + b'\n'
    (tmp_path / 'old').write_bytes(raw)
    saved = document(tmp_path, ['old'])
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    monkeypatch.setattr(planfile, 'plan', reached if delta <= 0 else forbid)
    if delta > 0:
        refuse(saved, tmp_path, 'old', 'file exceeds 8 MiB limit')
        assert reads == []
    else:
        with pytest.raises(ReachedRegeneration):
            load_plan(saved, tmp_path)
        assert reads == [('old', MAX_FILE_BYTES + 1, len(raw))]
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('budget', ['file', 'aggregate', 'zero'])
@pytest.mark.parametrize('overflow', [False, True])
def test_stat_underreported_growth_is_bounded(tmp_path, monkeypatch, budget, overflow):
    (tmp_path / 'first').write_bytes(b'first\n')
    (tmp_path / 'growing').touch()
    (tmp_path / 'later').touch()
    allowance = MAX_FILE_BYTES if budget == 'file' else 7 if budget == 'aggregate' else 0
    raw = b'x' * (allowance + 100 if overflow else allowance)
    saved = document(tmp_path, ['first', 'growing', 'later'], data={'growing': raw})
    before = tree(tmp_path)
    monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 6 + allowance)
    reads = spy_reads(monkeypatch, tmp_path, {'growing': raw})
    monkeypatch.setattr(planfile, 'plan', forbid if overflow else reached)
    if overflow:
        reason = 'file exceeds 8 MiB limit' if budget == 'file' else 'total source data exceeds 64 MiB limit'
        refuse(saved, tmp_path, 'growing', reason)
    else:
        with pytest.raises(ReachedRegeneration):
            load_plan(saved, tmp_path)
    assert reads == [('first', min(MAX_FILE_BYTES, 6 + allowance) + 1, 6),
                     ('growing', allowance + 1, allowance + 1 if overflow else allowance)] + (
                     [] if overflow else [('later', 1, 0)])
    monkeypatch.undo()
    assert tree(tmp_path) == before


def test_raw_growth_counts_actual_bytes_and_modes(tmp_path, monkeypatch):
    raw = b'\xef\xbb\xbf\xc3\xa9\r\n'
    (tmp_path / 'source').touch()
    (tmp_path / 'source').chmod(0o751)
    (tmp_path / 'later').write_bytes(b'end\n')
    saved = document(tmp_path, ['source', 'later'], data={'source': raw})
    monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 11)
    reads = spy_reads(monkeypatch, tmp_path, {'source': raw})
    monkeypatch.setattr(planfile, 'plan', reached)
    with pytest.raises(ReachedRegeneration):
        load_plan(saved, tmp_path)
    assert reads == [('source', 12, 7), ('later', 5, 4)]


@pytest.mark.parametrize('drift', ['bytes', 'mode', 'missing', 'unexpected'])
@pytest.mark.parametrize('reverse', [False, True], ids=['stale-first', 'large-first'])
def test_json_precondition_order_and_early_drift(tmp_path, monkeypatch, drift, reverse):
    (tmp_path / 'early').write_bytes(b'old\n')
    (tmp_path / 'large').write_bytes(b'x' * 10)
    saved = document(tmp_path, ['early', 'large'], absent=['created'])
    doc = json.loads(saved)
    if drift == 'unexpected':
        doc['preconditions'] = {n: doc['preconditions'][n] for n in ['created', 'early', 'large']}
        (tmp_path / 'created').touch()
    elif drift == 'bytes':
        (tmp_path / 'early').write_bytes(b'new\n')
    elif drift == 'mode':
        (tmp_path / 'early').chmod(0o700)
    else:
        (tmp_path / 'early').unlink()
    if reverse:
        doc['preconditions'] = {'large': doc['preconditions'].pop('large'), **doc['preconditions']}
    before = tree(tmp_path)
    monkeypatch.setattr(planfile, 'MAX_FILE_BYTES', 8)
    monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 6)
    reads = spy_reads(monkeypatch, tmp_path)
    monkeypatch.setattr(planfile, 'plan', forbid)
    refuse(json.dumps(doc), tmp_path, 'large' if reverse else 'created' if drift == 'unexpected' else 'early',
           'file exceeds 8 MiB limit' if reverse else 'original bytes, permissions or absence no longer match')
    assert reads == ([] if reverse or drift == 'missing' else
                     [('created', 7, 0)] if drift == 'unexpected' else [('early', 7, 4)])
    monkeypatch.undo()
    assert tree(tmp_path) == before


def test_path_safety_precedes_size_and_no_following_reads(tmp_path, monkeypatch):
    (tmp_path / 'old').write_bytes(b'oversize')
    saved = document(tmp_path, ['old'])
    (tmp_path / 'old').unlink()
    (tmp_path / 'old').symlink_to(tmp_path / 'outside')
    reads = spy_reads(monkeypatch, tmp_path)
    with pytest.raises(ApplyError, match='tree changed since planning: old:.*symlink'):
        load_plan(saved, tmp_path)
    assert reads == []


def test_supported_plan_and_check_keep_bom_crlf_empty_absence_modes(tmp_path, monkeypatch):
    (tmp_path / 'source').write_bytes(b'\xef\xbb\xbfhead\r\nold\r\n')
    (tmp_path / 'source').chmod(0o751)
    (tmp_path / 'empty').touch()
    request = parse('*** Begin Patch\n*** Update File: source\n*** Move to: nested/moved\n@@\n head\n-old\n+new\n'
                    '*** Delete File: empty\n*** Add File: created\n*** End Patch\n')
    saved = preview(request, tmp_path)
    before = tree(tmp_path)
    reads = spy_reads(monkeypatch, tmp_path)
    prepared = load_plan(saved, tmp_path)
    assert prepared.ok and prepared.changes[0].after == b'\xef\xbb\xbfhead\r\nnew\r\n'
    assert prepared.changes[0].mode == 0o751
    assert prepared.snapshots['empty'].data == b''
    assert prepared.snapshots['created'].data is None
    assert reads == [('source', MAX_FILE_BYTES + 1, 14), ('empty', MAX_FILE_BYTES + 1, 0)] * 2
    assert check_plan(saved, tmp_path)['diff_verified']
    monkeypatch.undo()
    assert tree(tmp_path) == before


def test_check_plan_keeps_aggregation_patch_order_and_remaining_allowance(tmp_path, monkeypatch):
    for name, raw in [('large', b'x' * 8), ('one', b'old\n'), ('two', b'two\n'), ('empty', b'')]:
        (tmp_path / name).write_bytes(raw)
    saved = document(tmp_path, ['large', 'one', 'two', 'empty'], absent=['created'])
    doc = json.loads(saved)
    doc['preconditions'] = dict(reversed(list(doc['preconditions'].items())))
    (tmp_path / 'one').write_bytes(b'new\n')
    (tmp_path / 'one').chmod(0o700)
    (tmp_path / 'two').unlink()
    (tmp_path / 'created').touch()
    before = tree(tmp_path)
    monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 4)
    monkeypatch.setattr(planfile, 'plan', forbid)
    reads = spy_reads(monkeypatch, tmp_path)
    report = check_plan(json.dumps(doc), tmp_path)
    assert not report['ok'] and not report['diff_verified']
    assert [(d['path'], d['codes']) for d in report['diagnostics']] == [
        ('large', ['RESOURCE_LIMIT']), ('one', ['BYTES_MISMATCH', 'MODE_MISMATCH']),
        ('two', ['ABSENCE_MISMATCH']), ('created', ['ABSENCE_MISMATCH'])]
    assert report['diagnostics'][0]['actual'] is None
    assert reads == [('one', 5, 4), ('empty', 1, 0), ('created', 1, 0)]
    monkeypatch.undo()
    assert tree(tmp_path) == before


@pytest.mark.parametrize('route', [['apply-plan'], ['apply-plan', '--dry-run'],
                                   ['apply-plan', '--check-only'], ['check-plan', '--json']])
def test_cli_preflight_resource_categories_and_no_staging(tmp_path, monkeypatch, capsys, route):
    import agentwrench.patch.cli as cli
    (tmp_path / 'source').write_bytes(b'old\n')
    saved = document(tmp_path, ['source'])
    input_file = tmp_path / 'saved.json'
    input_file.write_text(saved)
    before = tree(tmp_path)
    monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 3)
    monkeypatch.setattr(planfile, 'plan', forbid)
    monkeypatch.setattr(cli, 'commit', forbid)
    reads = spy_reads(monkeypatch, tmp_path)
    assert cli.main([route[0], str(input_file), '--root', str(tmp_path), *route[1:]]) == 1
    output = capsys.readouterr()
    if '--check-only' in route or '--json' in route:
        diagnostic, = json.loads(output.out)['diagnostics']
        assert not output.err and diagnostic['path'] == 'source'
        assert diagnostic['codes'] == ['RESOURCE_LIMIT'] and diagnostic['actual'] is None
    else:
        assert not output.out
        assert output.err == 'tree changed since planning: source: total source data exceeds 64 MiB limit\n'
    # The CLI reads the saved document; no target source is opened.
    assert reads == [('saved.json', planfile.MAX_PLAN_BYTES + 1, len(saved.encode()))]
    monkeypatch.undo()
    assert tree(tmp_path) == before
