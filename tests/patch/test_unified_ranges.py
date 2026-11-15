"""Bound numeric encodings before Python's configurable integer conversion."""
import pytest

from agentwrench.patch import PatchError, apply, from_unified, parse, plan
import agentwrench.patch.cli as cli
import agentwrench.patch.transaction as transaction


REASON = 'unified hunk range fields support at most 10 decimal digits'


def replacement(fields):
    old_start, old_count, new_start, new_count = fields
    return (f'--- a/x\n+++ b/x\n@@ -{old_start},{old_count} +{new_start},{new_count} @@\n'
            '-old\n+new\n')


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def forbid(*args, **kwargs):
    pytest.fail('conversion must not commit or stage')


@pytest.mark.parametrize('field', range(4), ids=['old-start', 'old-count', 'new-start', 'new-count'])
@pytest.mark.parametrize('value', ['0' * 10 + '1', '0' * 5000 + '1', '9' * 11, '9' * 5000],
                         ids=['one-over-padded', 'guard-padded', 'one-over-large', 'guard-large'])
@pytest.mark.parametrize('route', ['api', 'cli'])
def test_oversized_unified_range(tmp_path, monkeypatch, capsys, field, value, route):
    (tmp_path / 'x').write_bytes(b'old\n')
    (tmp_path / 'x').chmod(0o751)
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/keep').write_bytes(b'unchanged\n')
    fields = ['1'] * 4
    fields[field] = value
    # Even after an earlier valid operation, a late error must emit no prefix.
    diff = '--- /dev/null\n+++ b/created\n@@ -0,0 +1 @@\n+created\n' + replacement(fields)
    input_file = tmp_path / 'input.diff'
    input_file.write_text(diff)
    before = tree(tmp_path)
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    if route == 'api':
        with pytest.raises(PatchError, match=REASON) as error:
            from_unified(diff, root=tmp_path)
        assert error.value.line == 7
    else:
        assert cli.main(['from-unified', str(input_file), '--root', str(tmp_path)]) == 2
        output = capsys.readouterr()
        assert output.out == ''
        assert f'line 7: {REASON}' in output.err and 'Traceback' not in output.err
    assert tree(tmp_path) == before


@pytest.mark.parametrize('fields', [
    ['1'] * 4,
    ['0000000001'] * 4,
    ['9999999999', '1', '9999999999', '1'],
    ['١'] * 4,  # Preserve the existing decimal-digit grammar.
], ids=['ordinary', 'exact-padded', 'exact-start', 'unicode-decimal'])
def test_supported_ranges_reparse_and_apply(tmp_path, fields):
    (tmp_path / 'x').write_bytes(b'old\n')
    (tmp_path / 'x').chmod(0o751)
    before = tree(tmp_path)
    patch = from_unified(replacement(fields), root=tmp_path)
    reparsed = parse(patch.to_text())
    assert plan(reparsed, tmp_path).ok
    assert tree(tmp_path) == before
    apply(reparsed, tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'new\n'
    assert (tmp_path / 'x').stat().st_mode & 0o7777 == 0o751


@pytest.mark.parametrize('old_count,new_count', [('9999999999', '1'), ('1', '9999999999')])
def test_bounded_large_counts_still_require_content(old_count, new_count):
    with pytest.raises(PatchError, match='truncated unified hunk'):
        from_unified(replacement(['1', old_count, '1', new_count]))


@pytest.mark.parametrize('kind', ['add', 'delete'])
def test_exact_padded_zero_counts(tmp_path, kind):
    if kind == 'add':
        diff = '--- /dev/null\n+++ b/x\n@@ -0000000000,0000000000 +0000000001,0000000001 @@\n+new\n'
    else:
        (tmp_path / 'x').write_bytes(b'old\n')
        diff = '--- a/x\n+++ /dev/null\n@@ -0000000001,0000000001 +0000000000,0000000000 @@\n-old\n'
    patch = parse(from_unified(diff).to_text())
    apply(patch, tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'new\n' if kind == 'add' else not (tmp_path / 'x').exists()
