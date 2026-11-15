import json
from pathlib import Path
import subprocess
import sys

import pytest

FIXTURES = Path(__file__).parent / 'fixtures'


def cli(*args, data=None):
    return subprocess.run([sys.executable, '-m', 'agentwrench', 'patch', *map(str, args)],
                          input=data, capture_output=True)


def test_parse_json():
    r = cli('parse', FIXTURES / 'simple.patch', '--json')
    assert r.returncode == 0
    assert len(json.loads(r.stdout)['files']) == 4


def test_explain_bom_json():
    r = cli('explain', FIXTURES / 'bom.patch', '--root', FIXTURES / 'bom_tree', '--json')
    assert r.returncode == 0
    d = json.loads(r.stdout)
    assert d['ok'] and d['diagnostics'][0]['diagnosis']['bom']


def test_cli_failure_explanation_and_exit_codes(tmp_path):
    (tmp_path / 'x').write_text('actual\n')
    bad_context = b'*** Begin Patch\n*** Update File: x\n@@\n-expected\n+new\n*** End Patch\n'
    for command in ('apply', 'explain', 'to-unified'):
        r = cli(command, '-', '--root', tmp_path, data=bad_context)
        assert r.returncode == 1
        assert b'context' in r.stdout + r.stderr
    assert (tmp_path / 'x').read_text() == 'actual\n'
    r = cli('parse', '-', data=b'invalid')
    assert r.returncode == 2 and b'line 1' in r.stderr
    r = cli('apply', '-', '--root', tmp_path, data=b'*** Begin Patch\n*** Add File: ../x\n+x\n*** End Patch\n')
    assert r.returncode == 2 and b'unsafe' in r.stderr


def test_cli_stdin_dryrun_apply_convert(tmp_path):
    data = b'*** Begin Patch\n*** Add File: nested/new\n+new\n*** End Patch\n'
    assert cli('apply', '-', '--root', tmp_path, '--dry-run', '--atomic', data=data).returncode == 0
    assert list(tmp_path.iterdir()) == []
    diff = cli('to-unified', '-', '--root', tmp_path, data=data)
    assert diff.returncode == 0 and b'new file mode 100644' in diff.stdout
    imported = cli('from-unified', '-', '--context', '2', data=diff.stdout)
    assert imported.returncode == 0
    assert cli('apply', '-', '--root', tmp_path, data=imported.stdout).returncode == 0
    assert (tmp_path / 'nested/new').read_bytes() == b'new\n'


@pytest.mark.parametrize('command', ['from-unified', 'to-unified'])
def test_bad_context_option(command, tmp_path):
    args = [command, '-', '--context', '0']
    if command == 'to-unified':
        args += ['--root', tmp_path]
    r = cli(*args, data=b'*** Begin Patch\n*** End Patch\n' if command == 'to-unified' else b'not a diff')
    assert r.returncode == 2


def test_read_errors_and_invalid_encoding(tmp_path):
    assert cli('parse', tmp_path / 'absent').returncode == 1
    assert cli('parse', '-', data=b'\xff').returncode == 2


def test_explain_json_failure(tmp_path):
    r = cli('explain', '-', '--root', tmp_path, '--json',
            data=b'*** Begin Patch\n*** Delete File: absent\n*** End Patch\n')
    assert r.returncode == 1
    assert json.loads(r.stdout)['diagnostics'][0]['reason'] == 'source file does not exist'


PLAN_PATCH = b'''*** Begin Patch
*** Update File: hello.txt
@@
-old
+new
*** Delete File: gone.txt
*** Update File: old-name.txt
*** Move to: nested/renamed.txt
@@
 keep
*** Add File: nested/note.txt
+created
*** End Patch
'''


def plan_tree(root):
    """Include directories, modes and bytes to detect even staging leftovers."""
    return {str(p.relative_to(root)): (p.stat().st_mode & 0o7777,
                                     p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def preview_fixture(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    for name, data in [('hello.txt', b'old\n'), ('gone.txt', b'bye\n'),
                       ('old-name.txt', b'\xef\xbb\xbfkeep\r\n')]:
        (root / name).write_bytes(data)
        (root / name).chmod(0o644)
    (root / 'old-name.txt').chmod(0o751)
    saved = tmp_path / 'plan.json'
    before = plan_tree(root)
    r = cli('preview', '-', '--root', root, '--output', saved, data=PLAN_PATCH)
    assert r.returncode == 0, r.stderr
    assert plan_tree(root) == before
    return root, saved


def test_preview_file_apply_plan(tmp_path):
    root, saved = preview_fixture(tmp_path)
    document = json.loads(saved.read_text())
    import hashlib
    assert document['preconditions']['hello.txt'] == {
        'sha256': hashlib.sha256(b'old\n').hexdigest(), 'mode': '0644'}
    assert document['preconditions']['old-name.txt']['mode'] == '0751'
    assert document['preconditions']['nested/note.txt'] == {'sha256': None, 'mode': None}
    assert '-old\n+new' in document['diff']
    before = plan_tree(root)
    r = cli('apply-plan', saved, '--root', root, '--dry-run')
    assert r.returncode == 0 and b'Validated 4' in r.stdout, r.stderr
    assert plan_tree(root) == before
    # The saved plan is self-contained; the original patch was supplied on stdin.
    r = cli('apply-plan', '-', '--root', root, data=saved.read_bytes())
    assert r.returncode == 0 and b'Applied 4' in r.stdout, r.stderr
    assert (root / 'hello.txt').read_bytes() == b'new\n'
    assert not (root / 'gone.txt').exists() and not (root / 'old-name.txt').exists()
    assert (root / 'nested/renamed.txt').read_bytes() == b'\xef\xbb\xbfkeep\r\n'
    assert (root / 'nested/renamed.txt').stat().st_mode & 0o7777 == 0o751
    assert (root / 'nested/note.txt').read_bytes() == b'created\n'


@pytest.mark.parametrize('drift', ['bytes', 'mode', 'deleted', 'add-target', 'move-target'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_apply_stale_plan_changes_nothing(tmp_path, drift, dry_run):
    root, saved = preview_fixture(tmp_path)
    if drift == 'bytes':
        # Matching context survives; replanning alone would silently accept this.
        (root / 'hello.txt').write_bytes(b'old\nconcurrent addition\n')
    elif drift == 'mode':
        (root / 'gone.txt').chmod(0o600)
    elif drift == 'deleted':
        (root / 'gone.txt').unlink()
    else:
        (root / 'nested').mkdir()
        (root / ('nested/note.txt' if drift == 'add-target' else 'nested/renamed.txt')).write_text('writer\n')
    before = plan_tree(root)
    r = cli('apply-plan', saved, '--root', root, *(['--dry-run'] if dry_run else []))
    assert r.returncode == 1 and b'changed since planning' in r.stderr, r.stderr
    assert plan_tree(root) == before


def test_preview_failure_does_not_publish_plan(tmp_path):
    saved = tmp_path / 'plan.json'
    root = tmp_path / 'root'
    root.mkdir()
    r = cli('preview', '-', '--root', root, '--output', saved, data=PLAN_PATCH)
    assert r.returncode == 1, r.stderr
    assert not saved.exists() and list(root.iterdir()) == []


def test_preview_stdout_and_output_protection(tmp_path):
    root, saved = preview_fixture(tmp_path)
    result = cli('preview', '-', '--root', root, data=PLAN_PATCH)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(saved.read_bytes())
    before = plan_tree(tmp_path)
    # Existing review artifacts and patch paths must never be overwritten.
    for output, code in [(saved, 1), (root / 'hello.txt', 2),
                         (root / 'nested/note.txt', 2), (root / 'nested', 2)]:
        result = cli('preview', '-', '--root', root, '--output', output, data=PLAN_PATCH)
        assert result.returncode == code, result.stderr
        assert plan_tree(tmp_path) == before


@pytest.mark.parametrize('path', ['hello.txt', 'gone.txt', 'old-name.txt',
                                  'nested/renamed.txt', 'nested/note.txt'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_apply_plan_invalid_operation_roles(tmp_path, path, dry_run):
    root, saved = preview_fixture(tmp_path)
    doc = json.loads(saved.read_text())
    if path.startswith('nested/'):
        import hashlib
        doc['preconditions'][path] = {'sha256': hashlib.sha256(b'').hexdigest(), 'mode': '0644'}
        (root / 'nested').mkdir()
        (root / path).touch()
    else:
        doc['preconditions'][path] = {'sha256': None, 'mode': None}
        (root / path).unlink()
    saved.write_text(json.dumps(doc))
    before = plan_tree(root)
    result = cli('apply-plan', saved, '--root', root, *(['--dry-run'] if dry_run else []))
    assert result.returncode == 2 and result.stdout == b''
    assert path.encode() in result.stderr
    assert plan_tree(root) == before
