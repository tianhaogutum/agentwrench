"""Declared unified source coordinates are strict and never relocated."""
import json
from pathlib import Path

from agentwrench.patch import from_unified, plan
from agentwrench.patch.cli import main


REPEATED = (Path(__file__).parent / 'fixtures/unified_wrong_location.diff').read_text()


def test_repeated_text_at_wrong_coordinate_is_not_relocated(tmp_path, capsys):
    source = tmp_path / 'source'
    source.write_bytes(b'old\nkeep\ngap\nold\nkeep\n')
    source.chmod(0o751)
    patch = tmp_path / 'input.diff'
    patch.write_text(REPEATED)
    before = source.read_bytes(), source.stat().st_mode
    # Existing V4A conversion deliberately finds text at another occurrence.
    assert plan(from_unified(REPEATED), tmp_path).ok
    assert main(['check-unified', str(patch), '--root', str(tmp_path)]) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert not output.err and report['format'] == 'v4a-unified-check' and report['version'] == 1
    hunk = report['files'][0]['hunks'][0]
    assert not report['ok'] and hunk['code'] == 'LOCATION_MISMATCH'
    assert hunk['old_range'] == {'start': 2, 'count': 2}
    assert hunk['source_span'] == {'start_line': 2, 'end_line': 4}
    assert hunk['mismatch_line'] == 2
    assert '"old"' not in output.out and 'keep' not in output.out
    assert (source.read_bytes(), source.stat().st_mode) == before


import io
import pytest

from agentwrench.patch import PatchError, check_unified
import agentwrench.patch.cli as cli
import agentwrench.patch.unified as unified
import agentwrench.patch.transaction as transaction


def diff(body, header='-1 +1', path='source'):
    return f'--- a/{path}\n+++ b/{path}\n@@ {header} @@\n{body}'


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def forbidden(*args, **kwargs):
    pytest.fail('read-only source checking must not plan, convert, commit, or stage')


@pytest.mark.parametrize('source,body,header,code,line', [
    (b'old\n', '-old\n+new\n', '-1 +1', 'HUNK_MATCHED', None),
    (b'old \n', '-old\n+new\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b' old\n', '-old\n+new\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    ('“old”\n'.encode(), '-"old"\n+new\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b'old\r\n', '-old\r\n+new\r\n', '-1 +1', 'HUNK_MATCHED', None),
    (b'old\r\n', '-old\n+new\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b'old\n', '-old\r\n+new\r\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b'old', '-old\n+new\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b'\xef\xbb\xbfold\r\n', '-\ufeffold\r\n+\ufeffnew\r\n', '-1 +1', 'HUNK_MATCHED', None),
    (b'\xef\xbb\xbfold\n', '-old\n+new\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b'old\n', '-\ufeffold\n+\ufeffnew\n', '-1 +1', 'LOCATION_MISMATCH', 1),
    (b'old\n', '-old\n+new\n', '-2 +2', 'LOCATION_OUT_OF_RANGE', None),
    (b'old\n', '-old\n+new\n', '-9999999999 +9999999999', 'LOCATION_OUT_OF_RANGE', None),
    (b'', '-old\n+new\n', '-1 +1', 'LOCATION_OUT_OF_RANGE', None),
    (b'old\n', '-old\n', '-1 +0,0', 'HUNK_MATCHED', None),
    (b'old\nlast\n', '-last\n', '-2 +1,0', 'HUNK_MATCHED', None),
    (b'', '+new\n', '-0,0 +1', 'HUNK_MATCHED', None),
    (b'old\n', '+new\n', '-0,0 +1', 'HUNK_MATCHED', None),
    (b'old\n', '+new\n', '-1,0 +2', 'HUNK_MATCHED', None),
    (b'old', '+new\n', '-1,0 +2', 'HUNK_MATCHED', None),
    (b'old\n', '+new\n', '-2,0 +3', 'LOCATION_OUT_OF_RANGE', None),
])
def test_exact_source_locations(tmp_path, monkeypatch, source, body, header, code, line):
    (tmp_path / 'source').write_bytes(source)
    (tmp_path / 'source').chmod(0o751)
    before = snapshot(tmp_path)
    monkeypatch.setattr(unified, 'from_unified', forbidden)
    monkeypatch.setattr(unified, 'to_unified', forbidden)
    monkeypatch.setattr(cli, 'plan', forbidden)
    monkeypatch.setattr(cli, 'commit', forbidden)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbidden)
    result = check_unified(diff(body, header), tmp_path)
    hunk = result['files'][0]['hunks'][0]
    assert hunk['code'] == code and hunk['mismatch_line'] == line
    assert result['ok'] == (code == 'HUNK_MATCHED')
    assert not result['diagnostics'] and snapshot(tmp_path) == before


def test_original_coordinates_and_every_context_line(tmp_path):
    (tmp_path / 'source').write_bytes(b'one\ntwo\nthree\nfour\nfive\n')
    text = diff('-one\n+ONE\n+extra\n', '-1 +1,2') + '@@ -3 +4 @@\n-three\n+THREE\n'
    result = check_unified(text, tmp_path)
    assert result['ok']
    assert [h['source_span'] for h in result['files'][0]['hunks']] == [
        {'start_line': 1, 'end_line': 2}, {'start_line': 3, 'end_line': 4}]
    assert result['files'][0]['hunks'][1]['patch_span'] == {'start_line': 7, 'end_line': 10}
    # The importer may trim edge context. The checker must check it all.
    text = diff(' WRONG\n two\n three\n four\n-five\n+FIVE\n', '-1,5 +1,5')
    assert check_unified(text, tmp_path)['files'][0]['hunks'][0]['mismatch_line'] == 1


def test_aggregate_files_hunks_and_source_failures(tmp_path):
    (tmp_path / 'source').write_bytes(b'old\nkeep\nold\n')
    (tmp_path / 'gone').write_bytes(b'delete\n')
    text = (diff('-wrong\n+new\n') + '@@ -3 +3 @@\n-old\n+new\n'
            + diff('-stale\n+new\n', path='missing')
            + '--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+created\n'
            + '--- a/gone\n+++ /dev/null\n@@ -1 +0,0 @@\n-delete\n')
    before = snapshot(tmp_path)
    result = check_unified(text, tmp_path)
    assert not result['ok'] and [f['file'] for f in result['files']] == [1, 2, 3, 4]
    assert [h['code'] for h in result['files'][0]['hunks']] == ['LOCATION_MISMATCH', 'HUNK_MATCHED']
    assert result['files'][1]['hunks'][0]['code'] == 'SOURCE_MISSING'
    assert result['files'][2]['old_path'] is None and result['files'][2]['ok']
    assert result['files'][3]['new_path'] is None and result['files'][3]['ok']
    assert snapshot(tmp_path) == before
    (tmp_path / 'new').write_bytes(b'exists\n')
    assert check_unified(text, tmp_path)['files'][2]['code'] == 'ADD_TARGET_EXISTS'


def test_metadata_only_and_rename_source_only(tmp_path):
    (tmp_path / 'source').write_bytes(b'')
    (tmp_path / 'destination').write_bytes(b'existing destination is outside source check\n')
    text = ('diff --git a/source b/destination\nrename from source\nrename to destination\n'
            'diff --git a/new b/new\nnew file mode 100644\n'
            'diff --git a/gone b/gone\ndeleted file mode 100644\n')
    result = check_unified(text, tmp_path)
    assert [f['code'] for f in result['files']] == ['SOURCE_PRESENT', 'SOURCE_ABSENT', 'SOURCE_MISSING']
    assert all(f['hunks'] == [] for f in result['files'])
    # No source context expansion/canonical V4A budget is required.
    (tmp_path / 'source').write_bytes(b'x\n' * 100000)
    assert check_unified(text.split('diff --git a/new')[0], tmp_path)['ok']


@pytest.mark.parametrize('text', [
    '', diff('-old\n+new\n', '-0 +1'), diff('-old\n+new\n', '-1 +2'),
    diff('-old\n+new\n', '-1,2 +1'), diff('-old\n+new\n', '-00000000001 +1'),
    diff('-old\n+new\n', path='../source'), diff('-old\n+new\n', path='.git/config'),
    diff('-old\n+new\n') * 2,
    diff('-old\n+new\n', path='node') + diff('-old\n+new\n', path='node/child'),
    diff('-old\n+new\n') + '@@ -1 +1 @@\n-old\n+new\n',
    diff('-old\n+new\n\\ No newline at end of file\n'),
    diff('-old\r\n+new\n'), diff('-\ufeffold\n+new\n'),
    diff(' context\n'), diff('', '-0,0 +0,0'), diff('-old\n+\x00\n'),
    'diff --git a/source b/source\nGIT binary patch\nsecret source text\n',
    'diff --git a/new b/new\nnew file mode 100755\n',
    '--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+new\r\n',
])
def test_strict_parser_rejections_before_source_reads(tmp_path, monkeypatch, capsys, text):
    patch = tmp_path / 'input.diff'
    patch.write_bytes(text.encode())
    monkeypatch.setattr(unified, 'safe_path', forbidden)
    with pytest.raises(PatchError):
        check_unified(text, tmp_path)
    assert main(['check-unified', str(patch), '--root', str(tmp_path)]) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not output.err and result['files'] == [] and not result['ok']
    assert result['diagnostics'][0]['code'] == 'INVALID_DIFF'
    assert 'secret source text' not in output.out


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'parent'])
def test_unsafe_tree_paths_exit_two(tmp_path, capsys, kind):
    (tmp_path / 'elsewhere').write_bytes(b'old\n')
    path = 'source'
    if kind == 'symlink':
        (tmp_path / path).symlink_to(tmp_path / 'elsewhere')
    elif kind == 'directory':
        (tmp_path / path).mkdir()
    else:
        (tmp_path / path).write_bytes(b'old\n')
        path += '/child'
    patch = tmp_path / 'input.diff'
    patch.write_text(diff('-old\n+new\n', path=path))
    assert main(['check-unified', str(patch), '--root', str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'INVALID_DIFF'


@pytest.mark.parametrize('data', [b'\xff', b'\x00', b'x\n' * 100001, b'x' * (8*1024*1024+1)])
def test_source_resource_and_representation_limits(tmp_path, capsys, data):
    (tmp_path / 'source').write_bytes(data)
    patch = tmp_path / 'input.diff'
    patch.write_text(diff('-old\n+new\n'))
    assert main(['check-unified', str(patch), '--root', str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'INVALID_DIFF'


def test_total_source_and_input_limits(tmp_path, monkeypatch):
    (tmp_path / 'one').write_bytes(b'old\n')
    (tmp_path / 'two').write_bytes(b'old\n')
    text = diff('-old\n+new\n', path='one') + diff('-old\n+new\n', path='two')
    monkeypatch.setattr(unified, 'MAX_TOTAL_BYTES', 8)
    assert check_unified(text, tmp_path)['ok']
    monkeypatch.setattr(unified, 'MAX_TOTAL_BYTES', 7)
    with pytest.raises(PatchError, match='byte budget'):
        check_unified(text, tmp_path)
    for text in ('\n' * 100001, 'x' * (8*1024*1024+1), ''.join(
            f'diff --git a/file{i} b/file{i}\nnew file mode 100644\n' for i in range(1001))):
        with pytest.raises(PatchError):
            check_unified(text, tmp_path)


def test_io_errors_and_root_failure(tmp_path, monkeypatch, capsys):
    patch = tmp_path / 'input.diff'
    patch.write_text(diff('-old\n+new\n'))
    assert main(['check-unified', str(patch), '--root', str(tmp_path / 'absent')]) == 1
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'ROOT_UNAVAILABLE'
    assert main(['check-unified', str(tmp_path / 'absent.diff'), '--root', str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'IO_ERROR'
    (tmp_path / 'source').write_bytes(b'old\n')
    original = Path.open
    def fail_source(self, *args, **kwargs):
        if self.name == 'source':
            raise PermissionError('synthetic refusal')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_source)
    assert main(['check-unified', str(patch), '--root', str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['files'][0]['hunks'][0]['code'] == 'IO_ERROR'


def test_stdin_and_invalid_utf8(tmp_path, monkeypatch, capsys):
    (tmp_path / 'source').write_bytes(b'old\n')
    for data, code in [(diff('-old\n+new\n').encode(), 0), (b'\xff', 2)]:
        monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(data)))
        assert main(['check-unified', '-', '--root', str(tmp_path)]) == code
        output = capsys.readouterr()
        assert json.loads(output.out)['ok'] == (code == 0) and not output.err


def test_quoted_multibyte_path_and_context_only_exact_occurrence(tmp_path):
    name = 'café space.txt'
    (tmp_path / name).write_bytes(b'old\nkeep\ngap\nold\nkeep\n')
    text = (f'--- {unified.quote("a/"+name)}\n+++ {unified.quote("b/"+name)}\n'
            '@@ -4,2 +4,2 @@\n-old\n+new\n keep\n')
    assert check_unified(text, tmp_path)['ok']
    # Conversion retains textual matching; its first occurrence remains line 1.
    prepared = plan(from_unified(text), tmp_path)
    assert prepared.ok and prepared.diagnostics[0]['source_span']['start_line'] == 1
