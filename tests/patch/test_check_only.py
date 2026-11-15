"""Synthetic integration cases for the read-only, versioned diagnostic contract."""
import json
import subprocess
import sys

import pytest

from agentwrench.patch import parse, plan


def envelope(body):
    return '*** Begin Patch\n' + body + '\n*** End Patch\n'


def span(path, start, end):
    return {'path': path, 'start_line': start, 'end_line': end}


def check(root, text, expected=0):
    result = subprocess.run(
        [sys.executable, '-m', 'agentwrench', 'patch', 'apply', '-', '--root', str(root), '--check-only'],
        input=text.encode() if isinstance(text, str) else text, capture_output=True)
    assert result.returncode == expected, (result.stdout, result.stderr)
    assert result.stderr == b''
    doc = json.loads(result.stdout)
    assert doc['format'] == 'v4a-check' and doc['version'] == 1
    assert doc['ok'] is (expected == 0)
    return doc['diagnostics']


def tree(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode,
                                     p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def test_check_only_rename_crlf_unterminated_example(tmp_path):
    (tmp_path / 'old.txt').write_bytes(b'head\r\nold')
    text = envelope('*** Update File: old.txt\n*** Move to: nested/new.txt\n@@\n head\n-old\n+new')
    before = tree(tmp_path)
    d, = check(tmp_path, text.replace('\n', '\r\n'))
    assert d['code'] == 'HUNK_MATCHED'
    assert d['patch_span'] == {'start_line': 4, 'end_line': 8}
    assert d['source_span'] == span('old.txt', 1, 3)
    assert d['result_span'] == span('nested/new.txt', 1, 3)
    assert d['diagnosis']['crlf'] and d['diagnosis']['missing_final_newline']
    assert tree(tmp_path) == before
    # Verify reported coordinates against the actual planned result, still without writes.
    prepared = plan(parse(text), tmp_path)
    assert prepared.changes[0].after == b'head\r\nnew\r\n'
    assert prepared.diagnostics == [d]


def test_check_only_all_hunks_with_drift_and_anchor_failure(tmp_path):
    (tmp_path / 'x').write_bytes(b'old\nkeep\n')
    text = envelope('*** Update File: x\n@@\n-old\n+new\n@@\n-missing\n+bad\n@@ absent\n-keep\n+K')
    before = tree(tmp_path)
    matched, failed, anchor = check(tmp_path, text, expected=1)
    assert [d['code'] for d in (matched, failed, anchor)] == [
        'HUNK_MATCHED', 'CONTEXT_NOT_FOUND', 'ANCHOR_NOT_FOUND']
    assert [d['patch_span'] for d in (matched, failed, anchor)] == [
        {'start_line': 3, 'end_line': 6}, {'start_line': 6, 'end_line': 9},
        {'start_line': 9, 'end_line': 12}]
    assert matched['source_span'] == span('x', 1, 2)
    assert failed['source_span'] is None and anchor['source_span'] is None
    assert all(d['result_span'] is None for d in (matched, failed, anchor))
    assert 'candidate' in failed and 'candidate' in anchor
    assert tree(tmp_path) == before


def test_check_only_result_spans_account_for_prior_hunks(tmp_path):
    (tmp_path / 'old').write_bytes(b'a\r\nb\r\nc\r\nd\r\n')
    text = envelope('*** Update File: old\n*** Move to: new\n@@\n-a\n+A\n+extra\n@@\n-c\n@@\n-d\n+D\n*** End of File')
    reports = check(tmp_path, text)
    assert [d['source_span'] for d in reports] == [span('old', 1, 2), span('old', 3, 4), span('old', 4, 5)]
    assert [d['result_span'] for d in reports] == [span('new', 1, 3), span('new', 4, 4), span('new', 4, 5)]
    assert reports[-1]['patch_span'] == {'start_line': 10, 'end_line': 14}
    assert plan(parse(text), tmp_path).changes[0].after == b'A\r\nextra\r\nb\r\nD\r\n'


def test_check_only_inserted_carriage_return_counts_result_lines(tmp_path):
    (tmp_path / 'x').write_bytes(b'old\r\ntail\r\n')
    text = envelope('*** Update File: x\n@@\n-old\n+one\rtwo\n@@\n-tail\n+last')
    first, second = check(tmp_path, text)
    assert first['result_span'] == span('x', 1, 3)
    assert second['source_span'] == span('x', 2, 3)
    assert second['result_span'] == span('x', 3, 4)
    assert plan(parse(text), tmp_path).changes[0].after == b'one\rtwo\r\nlast\r\n'


@pytest.mark.parametrize(('source', 'body', 'old_span', 'new_span', 'fallback'), [
    (b'a\na\n', '@@\n-a\n+A\n*** End of File', (2, 3), (2, 3), False),
    (b'a\nkeep\n', '@@\n-a\n+A\n*** End of File', (1, 2), (1, 2), True),
    (b'old', '@@\n-old\n+new\n \n*** End of File', (1, 2), (1, 2), False),
    (b'a\r\n', '@@\n+appended', (2, 2), (2, 3), False),
    (b'', '@@\n+first', (1, 1), (1, 2), False),
    (b'last', '@@\n-last', (1, 2), (1, 1), False),
    (b'\xef\xbb\xbf old \r\n', '@@\n-old\n+new', (1, 2), (1, 2), False),
])
def test_check_only_eof_and_zero_width_spans(tmp_path, source, body, old_span, new_span, fallback):
    (tmp_path / 'x').write_bytes(source)
    d, = check(tmp_path, envelope('*** Update File: x\n' + body))
    assert d['source_span'] == span('x', *old_span)
    assert d['result_span'] == span('x', *new_span)
    assert d['eof_fallback'] is fallback
    assert (tmp_path / 'x').read_bytes() == source


def test_check_only_physical_patch_lines_and_implicit_hunk(tmp_path):
    (tmp_path / 'x').write_bytes(b'old\n')
    text = '\n<<\'EOF\'\n' + envelope('*** Update File: x\n-old\n+new\n*** End of File\n\n@@\n+tail') + 'EOF\n'
    first, second = check(tmp_path, text)
    assert first['patch_span'] == {'start_line': 5, 'end_line': 8}
    assert second['patch_span'] == {'start_line': 9, 'end_line': 11}
    assert second['source_span'] == span('x', 2, 2)
    assert second['result_span'] == span('x', 2, 3)


def test_check_only_file_failures_and_unrelated_hunks(tmp_path):
    (tmp_path / 'exists').write_bytes(b'old\n')
    text = envelope('*** Add File: exists\n+no\n*** Delete File: absent\n*** Update File: exists\n@@\n-old\n+new')
    # Duplicate paths are malformed input, before ordinary tree validation.
    d, = check(tmp_path, text, expected=2)
    assert d['code'] == 'INVALID_PATCH'
    text = envelope('*** Add File: exists\n+no\n*** Delete File: absent\n*** Update File: missing\n@@\n-x\n+y')
    reports = check(tmp_path, text, expected=1)
    assert [d['code'] for d in reports] == ['ADD_TARGET_EXISTS', 'SOURCE_MISSING', 'SOURCE_MISSING']
    assert reports[0]['patch_span'] == {'start_line': 2, 'end_line': 4}
    assert reports[1]['patch_span'] == {'start_line': 4, 'end_line': 5}
    assert all(d['source_span'] is None and d['result_span'] is None for d in reports)


def test_check_only_move_conflict_and_later_file(tmp_path):
    for name in ('old', 'occupied', 'other'):
        (tmp_path / name).write_bytes(b'old\n')
    text = envelope('*** Update File: old\n*** Move to: occupied\n@@\n-old\n+new\n*** Update File: other\n@@\n-old\n+new')
    before = tree(tmp_path)
    conflict, matched = check(tmp_path, text, expected=1)
    assert conflict['code'] == 'MOVE_TARGET_EXISTS' and conflict['destination_path'] == 'occupied'
    assert matched['code'] == 'HUNK_MATCHED' and matched['result_span'] == span('other', 1, 2)
    assert tree(tmp_path) == before


@pytest.mark.parametrize(('text', 'line'), [('invalid', 1), (envelope('*** Add File: ../x\n+bad'), 2), (b'\xff', None)])
def test_check_only_malformed_input_is_json(tmp_path, text, line):
    d, = check(tmp_path, text, expected=2)
    assert d['code'] == 'INVALID_PATCH'
    assert d['patch_span'] == ({'start_line': line, 'end_line': line + 1} if line else None)
    assert d['source_span'] is None and d['result_span'] is None
    assert list(tmp_path.iterdir()) == []


def test_check_only_io_error_is_json(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'patch', 'apply', str(tmp_path / 'absent'),
                             '--root', str(tmp_path), '--check-only'], capture_output=True)
    assert result.returncode == 1 and result.stderr == b''
    assert json.loads(result.stdout)['diagnostics'][0]['code'] == 'IO_ERROR'


def test_check_only_non_directory_parent_is_json(tmp_path):
    (tmp_path / 'parent').write_bytes(b'file\n')
    d, = check(tmp_path, envelope('*** Add File: parent/new\n+created'), expected=1)
    assert d['code'] == 'VALIDATION_FAILED'
    assert (tmp_path / 'parent').read_bytes() == b'file\n'


def test_check_only_symlink_rejected_as_json(tmp_path):
    outside = tmp_path / 'outside'
    outside.write_bytes(b'keep\n')
    (tmp_path / 'link').symlink_to(outside)
    d, = check(tmp_path, envelope('*** Delete File: link'), expected=2)
    assert d['code'] == 'INVALID_PATCH'
    assert (tmp_path / 'link').is_symlink() and outside.read_bytes() == b'keep\n'


def test_check_only_never_calls_commit(tmp_path, monkeypatch, capsys):
    import agentwrench.patch.cli as cli
    text = envelope('*** Add File: nested/new\n+created')
    patch_file = tmp_path / 'input.patch'
    patch_file.write_text(text)
    def forbidden(*args, **kwargs):
        pytest.fail('check-only must never invoke commit or staging')
    monkeypatch.setattr(cli, 'commit', forbidden)
    assert cli.main(['apply', str(patch_file), '--root', str(tmp_path), '--check-only']) == 0
    assert json.loads(capsys.readouterr().out)['ok']
    assert not (tmp_path / 'nested').exists()
