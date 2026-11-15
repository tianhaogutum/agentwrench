"""Canonical imported V4A must fit the same input budget as parse()."""
import pytest

from agentwrench.patch import PatchError, from_unified, parse, plan
from agentwrench.patch.model import FilePatch, Hunk, Patch
import agentwrench.patch.cli as cli
import agentwrench.patch.transaction as transaction


MAX_BYTES = 8 * 1024 * 1024
MAX_LINES = 100_000


def rename(source, destination):
    return (f'diff --git a/{source} b/{destination}\n'
            f'rename from {source}\nrename to {destination}\n')


def fixture(root, budget, delta, layout):
    names = ['source', 'other'] if layout == 'multiple' else ['source']
    files = [FilePatch('update', name, move_to=name + '-moved', hunks=[Hunk()]) for name in names]
    if layout == 'with-empty':
        (root / 'empty').write_bytes(b'')
        files.extend([FilePatch('delete', 'empty'), FilePatch('add', 'empty-moved')])
    expected = Patch(files)
    limit = MAX_LINES if budget == 'lines' else MAX_BYTES
    # Count the exact canonical overhead, including the envelope and empty-rename
    # expansion. For bytes, reserve one context prefix + LF for each source.
    if budget == 'bytes':
        for f in files[:len(names)]:
            f.hunks[0].lines = [(' ', '')]
    overhead = expected.to_text()
    available = limit + delta - (overhead.count('\n') if budget == 'lines'
                                else len(overhead.encode('utf-8')))
    for index, name in enumerate(names):
        count = available // len(names) + (index < available % len(names))
        values = ['x'] * count if budget == 'lines' else ['é' * (count // 2) + 'x' * (count % 2)]
        data = ('\n'.join(values) + '\n').encode('utf-8')
        assert len(data) <= MAX_BYTES and data.count(b'\n') <= MAX_LINES
        (root / name).write_bytes(data)
        (root / name).chmod(0o751)
        files[index].hunks[0].lines = [(' ', value) for value in values]
    text = expected.to_text()
    assert (text.count('\n') if budget == 'lines' else len(text.encode('utf-8'))) == limit + delta
    diff = ''.join(rename(name, name + '-moved') for name in names)
    if layout == 'with-empty':
        diff += rename('empty', 'empty-moved')
    return diff, text


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def forbid(*args, **kwargs):
    pytest.fail('conversion must not commit or stage')


@pytest.mark.parametrize('budget', ['lines', 'bytes'])
@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'exact', 'above'])
@pytest.mark.parametrize('layout', ['single', 'multiple', 'with-empty'])
@pytest.mark.parametrize('route', ['api', 'cli'])
def test_canonical_import_output_budget(tmp_path, monkeypatch, capsys, budget, delta, layout, route):
    diff, expected = fixture(tmp_path, budget, delta, layout)
    input_file = tmp_path / 'input.diff'
    input_file.write_text(diff)
    before = tree(tmp_path)
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    if delta > 0:
        # Independent parser control proves the intended failure is the output
        # budget, although the small diff and each individual source fit.
        with pytest.raises(PatchError, match='8 MiB or 100,000-line limit'):
            parse(expected)
        if route == 'api':
            with pytest.raises(PatchError, match='converted V4A patch exceeds 8 MiB or 100,000-line limit'):
                from_unified(diff, root=tmp_path)
        else:
            exit_code = cli.main(['from-unified', str(input_file), '--root', str(tmp_path)])
            output = capsys.readouterr()
            assert exit_code == 2
            assert output.out == ''
            assert 'converted V4A patch exceeds 8 MiB or 100,000-line limit' in output.err
    else:
        if route == 'api':
            actual = from_unified(diff, root=tmp_path).to_text()
        else:
            assert cli.main(['from-unified', str(input_file), '--root', str(tmp_path)]) == 0
            output = capsys.readouterr()
            assert output.err == ''
            actual = output.out
        assert actual == expected
        reparsed = parse(actual)
        assert reparsed.to_text() == actual
        assert plan(reparsed, tmp_path).ok
    assert tree(tmp_path) == before
