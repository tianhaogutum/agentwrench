"""Hypothetical update/move results must fit the source logical-newline budget."""
import json

import pytest

from agentwrench.patch import (PatchError, apply, check_plan, commit, load_plan,
                              parse, plan, preview, to_unified)
import agentwrench.patch.cli as cli
from agentwrench.patch.engine import Change, Plan, Snapshot, TextFile, update
from agentwrench.patch.model import FilePatch, Hunk
from agentwrench.patch.planfile import fingerprint
import agentwrench.patch.transaction as transaction

LIMIT = 100_000
REASON = 'result exceeds 100,000-line limit'


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def forbid(*args, **kwargs):
    pytest.fail('rejection must precede commit/staging')


def fixture(root, ending=b'\n', delta=1, layout='append', move=False):
    target = LIMIT + delta
    if layout == 'combined':
        original = b'head' + ending + (b'x' + ending) * (target - 4) + b'tail' + ending
        body = '@@\n head\n+y\n@@\n tail\n+z\n'
        expected = b'head' + ending + b'y' + ending + (b'x' + ending) * (target - 4) + b'tail' + ending + b'z' + ending
    elif layout == 'payload-cr':
        original = (b'x' + ending) * (target - 2)
        body = '@@\n+y\rz\n'
        expected = original + b'y\rz' + ending
    elif layout == 'unterminated':
        original = (b'x' + ending) * (target - 1) + b'tail'
        body = '@@\n tail\n'
        expected = original + ending
    else:
        original = (b'x' + ending) * (target - 1)
        body = '@@\n+y\n'
        expected = original + b'y' + ending
    (root / 'source').write_bytes(original)
    (root / 'source').chmod(0o751)
    (root / 'unrelated').write_bytes(b'keep\n')
    header = '*** Update File: source\n' + ('*** Move to: nested/moved\n' if move else '')
    patch = parse('*** Begin Patch\n' + header + body + '*** End Patch\n')
    # The real source passes the original reader, even with an unterminated tail.
    TextFile.read(original, 'source')
    assert len(original) < 8 * 1024 * 1024
    assert len(patch.to_text().encode()) < 200
    assert expected.count(b'\n') + expected.count(b'\r') - expected.count(b'\r\n') == target
    return patch, original, expected


def test_append_above_result_line_budget(tmp_path):
    source = tmp_path / 'source'
    original = b'x\n' * 100_000
    source.write_bytes(original)
    patch = parse('*** Begin Patch\n*** Update File: source\n@@\n+y\n*** End Patch\n')
    with pytest.raises(PatchError, match='source: result exceeds 100,000-line limit'):
        plan(patch, tmp_path)
    assert source.read_bytes() == original


@pytest.mark.parametrize('ending', [b'\n', b'\r\n', b'\r'], ids=['LF', 'CRLF', 'CR'])
@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'equal', 'above'])
@pytest.mark.parametrize('layout', ['append', 'combined', 'payload-cr', 'unterminated'])
@pytest.mark.parametrize('move', [False, True], ids=['update', 'move'])
def test_actual_result_boundaries(tmp_path, ending, delta, layout, move):
    patch, original, expected = fixture(tmp_path, ending, delta, layout, move)
    before = tree(tmp_path)
    if delta > 0:
        destination = 'nested/moved' if move else 'source'
        with pytest.raises(PatchError, match=destination + ': ' + REASON):
            plan(patch, tmp_path)
        assert tree(tmp_path) == before
    else:
        prepared = plan(patch, tmp_path)
        assert prepared.ok and prepared.changes[0].after == expected
        assert prepared.changes[0].before == original
        assert all(d['tier'] == 'exact' for d in prepared.diagnostics)
        TextFile.read(expected, 'accepted-result')
        assert tree(tmp_path) == before
        commit(prepared)
        result = tmp_path / ('nested/moved' if move else 'source')
        assert result.read_bytes() == expected
        assert result.stat().st_mode & 0o7777 == 0o751
        assert (tmp_path / 'unrelated').read_bytes() == b'keep\n'
        assert (tmp_path / 'source').exists() is (not move)


def test_net_result_across_hunks_accepts_equality(tmp_path):
    # An early expansion is offset by a later contraction; only the final result matters.
    original = b'head\n' + b'x\n' * (LIMIT - 4) + b'cut1\ncut2\ntail\n'
    (tmp_path / 'source').write_bytes(original)
    patch = parse('*** Begin Patch\n*** Update File: source\n@@\n head\n+y\n+z\n'
                  '@@\n-cut1\n-cut2\n tail\n*** End Patch\n')
    prepared = plan(patch, tmp_path)
    expected = b'head\ny\nz\n' + b'x\n' * (LIMIT - 4) + b'tail\n'
    assert prepared.ok and prepared.changes[0].after == expected
    assert expected.count(b'\n') == LIMIT


def test_bom_mixed_endings_and_unicode_separators_at_equality(tmp_path):
    original = b'\xef\xbb\xbfhead\r\n' + b'x\n' * (LIMIT - 3) + b'tail\r'
    (tmp_path / 'source').write_bytes(original)
    payload = '\v\f\x85\u2028\u2029'
    patch = parse('*** Begin Patch\n*** Update File: source\n@@\n+' + payload + '\n*** End Patch\n')
    prepared = plan(patch, tmp_path)
    expected = original + payload.encode() + b'\r\n'
    assert prepared.ok and prepared.changes[0].after == expected
    TextFile.read(expected, 'result')


def test_direct_update_counts_crlf_pairs_after_assembly():
    file = TextFile.read(b'x\n' * (LIMIT - 1), 'source')
    operation = FilePatch('update', 'source', hunks=[Hunk(lines=[('+', 'y\r')])])
    # Programmatic payload CR and preferred LF coalesce to one ending.
    after, reports = update(file, operation)
    assert after == b'x\n' * (LIMIT - 1) + b'y\r\n' and reports[0]['status'] == 'matched'
    operation.hunks[0].lines = [('+', 'y\rz\r')]
    with pytest.raises(PatchError, match='source: ' + REASON):
        update(file, operation)


def legacy_saved(root, patch, original, expected):
    """Correct preconditions/diff for an artifact accepted by the previous runtime."""
    snapshots = {'source': Snapshot(original, 0o751), 'created': Snapshot(None)}
    destination = patch.files[-1].move_to or 'source'
    if destination != 'source':
        snapshots[destination] = Snapshot(None)
    changes = [Change('created', 'created', None, b'created\n', 0o644, 'add'),
               Change('source', destination, original, expected, 0o751, 'update')]
    diff = to_unified(Plan(root, changes, snapshots, []))
    assert len(diff.encode()) < 500 and diff.count('\n') < 20
    return json.dumps(dict(format='v4a-plan', version=1, root=str(root), patch=patch.to_text(),
                           preconditions={p: fingerprint(s) for p, s in snapshots.items()}, diff=diff))


@pytest.mark.parametrize('move', [False, True], ids=['update', 'move'])
@pytest.mark.parametrize('route', ['plan', 'apply', 'apply-dry', 'preview', 'load', 'check'])
def test_shared_apis_reject_before_staging(tmp_path, monkeypatch, move, route):
    patch, original, expected = fixture(tmp_path, move=move)
    patch.files.insert(0, FilePatch('add', 'created', contents=['created']))
    saved = legacy_saved(tmp_path, patch, original, expected)
    before = tree(tmp_path)
    monkeypatch.setattr(transaction, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    with pytest.raises(PatchError, match=REASON):
        if route == 'plan':
            plan(patch, tmp_path)
        elif route.startswith('apply'):
            apply(patch, tmp_path, dry_run=route == 'apply-dry')
        elif route == 'preview':
            preview(patch, tmp_path)
        else:
            (load_plan if route == 'load' else check_plan)(saved, tmp_path)
    assert tree(tmp_path) == before


@pytest.mark.parametrize('route', [
    ['apply'], ['apply', '--atomic'], ['apply', '--dry-run'], ['apply', '--check-only'],
    ['explain', '--json'], ['to-unified'], ['preview'], ['preview', '--output'],
    ['apply-plan'], ['apply-plan', '--dry-run'], ['apply-plan', '--check-only'],
    ['check-plan'], ['check-plan', '--json']])
def test_cli_result_limit_classification(tmp_path, monkeypatch, capsys, route):
    patch, original, expected = fixture(tmp_path, move=True)
    patch.files.insert(0, FilePatch('add', 'created', contents=['created']))
    saved = legacy_saved(tmp_path, patch, original, expected)
    input_file = tmp_path / 'input'
    is_saved = route[0] in ('apply-plan', 'check-plan')
    input_file.write_text(saved if is_saved else patch.to_text())
    output_file = tmp_path / 'must-not-exist.json'
    flags = route[1:] + ([str(output_file)] if '--output' in route else [])
    before = tree(tmp_path)
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    assert cli.main([route[0], str(input_file), '--root', str(tmp_path), *flags]) == 2
    output = capsys.readouterr()
    if '--check-only' in route or route == ['check-plan', '--json']:
        doc = json.loads(output.out)
        assert output.err == '' and doc['ok'] is False
        d, = doc['diagnostics']
        if is_saved:
            assert doc['format'] == 'v4a-plan-check' and doc['diff_verified'] is False
            assert d['codes'] == ['INVALID_PLAN']
        else:
            assert doc['format'] == 'v4a-check' and d['code'] == 'INVALID_PATCH'
        assert 'nested/moved: ' + REASON in d['reason']
    else:
        assert output.out == '' and 'nested/moved: ' + REASON in output.err
        assert 'Traceback' not in output.err
    assert tree(tmp_path) == before and not output_file.exists()
