"""Unified export must fit the complete importer UTF-8 byte/LF budget."""
import hashlib
import json
import subprocess

import pytest

from agentwrench.patch import (PatchError, check_plan, from_unified, load_plan,
                              parse, plan, preview, to_unified)
import agentwrench.patch.cli as cli
from agentwrench.patch.planfile import fingerprint
import agentwrench.patch.transaction as transaction

MAX_BYTES = 8 * 1024 * 1024
MAX_LINES = 100_000
REASON = 'exported unified diff exceeds 8 MiB or 100,000-line limit'


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in root.rglob('*')}


def forbid(*args, **kwargs):
    pytest.fail('export must not commit or stage')


def deletion(name, values, terminated=True):
    count = len(values)
    span = '1' if count == 1 else f'1,{count}'
    # These fixture names are ASCII; Unicode path quoting is covered separately.
    result = (f'diff --git "a/{name}" "b/{name}"\n'
              'deleted file mode 100755\n'
              f'--- "a/{name}"\n+++ /dev/null\n@@ -{span} +0,0 @@\n')
    result += ''.join('-' + value + '\n' for value in values)
    if not terminated:
        result += '\\ No newline at end of file\n'
    return result


def fixture(root, budget, delta, layout):
    patch_body = ''
    prefix = ''
    if layout == 'mixed':
        (root / 'empty').write_bytes(b'')
        (root / 'old').write_bytes(b'old\nkeep\n')
        (root / 'old').chmod(0o751)
        patch_body = ('*** Add File: added\n*** Delete File: empty\n'
                      '*** Update File: old\n*** Move to: nested/new\n@@\n-old\n+new\n keep\n')
        prefix = ('diff --git "a/added" "b/added"\nnew file mode 100644\n'
                  'diff --git "a/empty" "b/empty"\ndeleted file mode 100644\n'
                  'diff --git "a/old" "b/nested/new"\nrename from "old"\n'
                  'rename to "nested/new"\n--- "a/old"\n+++ "b/nested/new"\n'
                  '@@ -1,2 +1,2 @@\n-old\n+new\n keep\n')
    names = ['source', 'other'] if layout == 'multiple' else ['source']
    terminated = layout != 'unterminated'
    limit = MAX_LINES if budget == 'lines' else MAX_BYTES
    if budget == 'lines':
        overhead = prefix.count('\n') + len(names) * (5 + (not terminated))
    else:
        overhead = len((prefix + ''.join(deletion(name, [''], terminated) for name in names)).encode())
    available = limit + delta - overhead
    expected = prefix
    for index, name in enumerate(names):
        count = available // len(names) + (index < available % len(names))
        # U+2028 is content, not an LF boundary; é distinguishes UTF-8 bytes
        # from Python characters. Each source and the V4A input fit their budgets.
        values = ['x'] * count if budget == 'lines' else ['\u2028' + 'é' * ((count-3)//2) + 'x' * ((count-3) % 2)]
        data = ('\n'.join(values) + ('\n' if terminated else '')).encode()
        assert len(data) <= MAX_BYTES and data.count(b'\n') <= MAX_LINES
        (root / name).write_bytes(data)
        (root / name).chmod(0o751)
        patch_body += f'*** Delete File: {name}\n'
        expected += deletion(name, values, terminated)
    (root / 'unrelated').mkdir()
    (root / 'unrelated' / 'keep').write_bytes(b'untouched\n')
    patch = parse('*** Begin Patch\n' + patch_body + '*** End Patch\n')
    assert (expected.count('\n') if budget == 'lines' else len(expected.encode())) == limit + delta
    return patch, expected


@pytest.mark.parametrize('budget', ['lines', 'bytes'])
@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'exact', 'above'])
@pytest.mark.parametrize('layout', ['single', 'multiple', 'mixed', 'unterminated'])
@pytest.mark.parametrize('route', ['api', 'cli'])
def test_complete_export_budget(tmp_path, monkeypatch, capsys, budget, delta, layout, route):
    patch, expected = fixture(tmp_path, budget, delta, layout)
    input_file = tmp_path / 'input.patch'
    input_file.write_text(patch.to_text())
    before = tree(tmp_path)
    prepared = plan(patch, tmp_path)
    assert prepared.ok
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    if delta > 0:
        # Independent importer control identifies the same input budget even for
        # an otherwise unsupported unterminated representation.
        with pytest.raises(PatchError, match='diff exceeds 8 MiB or 100,000-line limit'):
            from_unified(expected)
        if route == 'api':
            with pytest.raises(PatchError, match=REASON):
                to_unified(prepared)
        else:
            assert cli.main(['to-unified', str(input_file), '--root', str(tmp_path)]) == 2
            output = capsys.readouterr()
            assert output.out == ''
            assert REASON in output.err and 'Traceback' not in output.err
    else:
        if route == 'api':
            actual = to_unified(prepared)
        else:
            assert cli.main(['to-unified', str(input_file), '--root', str(tmp_path)]) == 0
            output = capsys.readouterr()
            assert output.err == ''
            actual = output.out
        assert actual == expected
        if layout != 'unterminated':
            imported = plan(parse(from_unified(actual).to_text()), tmp_path)
            assert imported.ok
            assert imported.changes == prepared.changes
        else:
            with pytest.raises(PatchError, match='missing-final-newline'):
                from_unified(actual)
        if delta == 0 and route == 'api':
            checked = subprocess.run(['git', '-C', str(tmp_path), 'apply', '--check', '-'],
                                     input=actual.encode(), capture_output=True)
            assert checked.returncode == 0, checked.stderr
    assert tree(tmp_path) == before


@pytest.mark.parametrize('budget', ['lines', 'bytes'])
@pytest.mark.parametrize('route', ['preview-api', 'preview-cli', 'preview-file', 'load-api',
                                   'check-api', 'apply-plan', 'apply-plan-dry-run', 'check-plan'])
def test_shared_export_callers_reject(tmp_path, monkeypatch, capsys, budget, route):
    patch, oversized = fixture(tmp_path, budget, 1, 'mixed')
    prepared = plan(patch, tmp_path)
    # Simulate a structurally valid artifact generated by the prior exporter.
    saved = json.dumps(dict(format='v4a-plan', version=1, root=str(tmp_path),
                            patch=patch.to_text(), diff=oversized,
                            preconditions={p: fingerprint(s) for p, s in prepared.snapshots.items()}))
    input_file = tmp_path / 'input'
    input_file.write_text(patch.to_text() if route.startswith('preview') else saved)
    output_file = tmp_path / 'must-not-exist.json'
    before = tree(tmp_path)
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    if route in ('preview-api', 'load-api', 'check-api'):
        with pytest.raises(PatchError, match=REASON):
            if route == 'preview-api':
                preview(patch, tmp_path)
            else:
                (load_plan if route == 'load-api' else check_plan)(saved, tmp_path)
    else:
        command = 'preview' if route.startswith('preview') else route.removesuffix('-dry-run')
        flags = ['--output', str(output_file)] if route == 'preview-file' else []
        if route == 'apply-plan-dry-run':
            flags = ['--dry-run']
        if route == 'check-plan':
            flags = ['--json']
        assert cli.main([command, str(input_file), '--root', str(tmp_path), *flags]) == 2
        output = capsys.readouterr()
        if route == 'check-plan':
            doc = json.loads(output.out)
            assert doc['diagnostics'][0]['codes'] == ['INVALID_PLAN']
            assert REASON in doc['diagnostics'][0]['reason']
            assert output.err == ''
        else:
            assert output.out == '' and REASON in output.err
    assert not output_file.exists()
    assert tree(tmp_path) == before
