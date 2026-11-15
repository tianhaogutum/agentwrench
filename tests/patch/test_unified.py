import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agentwrench.patch import PatchError, apply, from_unified, parse, plan, to_unified
from agentwrench.patch.unified import quote, unquote

FIXTURES = Path(__file__).parent / 'fixtures'


def git(root, *args, input=None):
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1'}
    return subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
                           '-c', 'core.autocrlf=false', '-c', 'commit.gpgSign=false',
                           '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                           *args], input=input, capture_output=True, check=True, env=env).stdout


def repo(root, files):
    root.mkdir()
    git(root, 'init', '-q')
    for path, value in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'synthetic baseline')


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and '.git' not in p.relative_to(root).parts}


def test_fixture_to_unified_real_git_apply(tmp_path):
    files = tree(FIXTURES / 'simple_tree')
    a, b = tmp_path / 'git', tmp_path / 'v4a'
    repo(a, files)
    repo(b, files)
    p = parse((FIXTURES / 'simple.patch').read_text())
    diff = to_unified(plan(p, a)).encode()
    git(a, 'apply', '--check', '-', input=diff)
    git(a, 'apply', '-', input=diff)
    apply(p, b)
    assert tree(a) == tree(b) == {
        'hello.txt': b'Hello, world!\nnew value\n', 'nested/new.txt': b'created\n',
        'new-name.txt': b'rename example\nafter\n'}


def test_from_real_git_diff_round_trip(tmp_path):
    original = {'hello.txt': b'Hello\nold\ntail\n', 'gone.txt': b'remove\n',
                'old name.txt': b'rename\nbefore\nlast\n'}
    source, target, reference = (tmp_path / s for s in ('source', 'target', 'reference'))
    for root in (source, target, reference):
        repo(root, original)
    (source / 'hello.txt').write_bytes(b'Hello\nnew\ntail\n')
    (source / 'gone.txt').unlink()
    git(source, 'mv', 'old name.txt', 'new name.txt')
    (source / 'new name.txt').write_bytes(b'rename\nafter\nlast\n')
    (source / 'added.txt').write_bytes(b'new file\n')
    git(source, 'add', '.')
    diff = git(source, 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--find-renames=10%')
    assert b'rename from old name.txt' in diff
    p = from_unified(diff.decode())
    assert any(f.move_to == 'new name.txt' for f in parse(p.to_text()).files)
    git(reference, 'apply', '--check', '-', input=diff)
    git(reference, 'apply', '-', input=diff)
    apply(p, target)
    assert tree(target) == tree(reference) == tree(source)


@pytest.mark.parametrize('filename', ['space name.txt', 'café.txt', 'quote"name.txt'])
def test_quoted_paths_git_round_trip(filename, tmp_path):
    root = tmp_path / 'root'
    repo(root, {filename: b'old\nkeep\n'})
    p = parse(f'*** Begin Patch\n*** Update File: {filename}\n@@\n-old\n+new\n keep\n*** End Patch\n')
    diff = to_unified(plan(p, root)).encode()
    git(root, 'apply', '--check', '-', input=diff)
    git(root, 'apply', '-', input=diff)
    assert (root / filename).read_bytes() == b'new\nkeep\n'
    assert from_unified(diff.decode()).files[0].path == filename


@pytest.mark.parametrize('data', [b'\xef\xbb\xbfkeep\r\nold\r\n', b'keep\nold', b'keep\r\nold\n'])
def test_to_unified_preserves_bytes_in_git(data, tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    for root in (a, b):
        repo(root, {'x': data})
    p = parse('*** Begin Patch\n*** Update File: x\n@@\n keep\n-old\n+new\n*** End Patch')
    diff = to_unified(plan(p, a)).encode()
    git(a, 'apply', '--check', '-', input=diff)
    git(a, 'apply', '-', input=diff)
    apply(p, b)
    assert tree(a) == tree(b)


def test_full_replacement_from_unified(tmp_path):
    (tmp_path / 'x').write_text('old\n')
    diff = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'
    apply(from_unified(diff), tmp_path)
    assert (tmp_path / 'x').read_text() == 'new\n'


def test_context_option_retains_internal_context():
    diff = '--- a/x\n+++ b/x\n@@ -1,7 +1,7 @@\n a\n b\n-c\n+C\n d\n-e\n+E\n f\n g\n'
    p = from_unified(diff, context=1)
    assert p.files[0].hunks[0].lines == [(' ', 'b'), ('-', 'c'), ('+', 'C'), (' ', 'd'),
                                         ('-', 'e'), ('+', 'E'), (' ', 'f')]


@pytest.mark.parametrize('diff', [
    '--- a/x\n+++ b/x\n@@ -1,2 +1 @@\n-old\n+new\n',
    '--- a/x\n+++ b/x\n@@ -0 +1 @@\n-old\n+new\n',
    '--- a/x\n+++ b/x\n@@ -1 +2 @@\n-old\n+new\n',
    '--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+new\n',
    '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n',
    'diff --git a/x b/x\nold mode 100644\nnew mode 100755\n',
    'diff --git a/x b/x\nGIT binary patch\nliteral 4\n',
    '--- a/../escape\n+++ b/../escape\n@@ -1 +1 @@\n-a\n+b\n',
    '--- a/x\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n',
    'diff --git a/x b/y\nrename from x\nrename to y\n',
    'diff --git a/wrong b/wrong\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n',
    '--- a/x\t2020-01-01\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n',
    'diff --git a/x b/x\nnew file mode 100755\n--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+a\n',
])
def test_reject_unsupported_or_malformed(diff):
    with pytest.raises(PatchError):
        from_unified(diff)


def test_quote_codec_and_invalid_escape():
    assert unquote(quote('café "hi".txt'), 1) == 'café "hi".txt'
    for value in ['"\\q"', '"\\777"', '"unclosed', '"\\377"']:
        with pytest.raises(PatchError):
            unquote(value, 1)


def test_executable_delete_git(tmp_path):
    root = tmp_path / 'r'
    repo(root, {'x': b'old\n'})
    (root / 'x').chmod(0o755)
    git(root, 'add', '.')
    diff = to_unified(plan(parse('*** Begin Patch\n*** Delete File: x\n*** End Patch'), root)).encode()
    assert b'deleted file mode 100755' in diff
    git(root, 'apply', '-', input=diff)
    assert not (root / 'x').exists()


@pytest.mark.parametrize('contents', [b'unchanged\n', b'', b'\xef\xbb\xbfunchanged\r\n'])
def test_pure_rename_from_native_git_with_root(contents, tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    for root in (source, target):
        repo(root, {'old name': contents})
    git(source, 'mv', 'old name', 'new name')
    diff = git(source, 'diff', '--cached', '--no-ext-diff', '--no-textconv', '-M')
    p = from_unified(diff.decode(), root=target)
    apply(p, target)
    assert tree(source) == tree(target) == {'new name': contents}


@pytest.mark.parametrize('operation', ['add', 'delete'])
def test_empty_file_git_roundtrip(operation, tmp_path):
    root = tmp_path / 'r'
    repo(root, {'keep': b'keep\n', **({'empty': b''} if operation == 'delete' else {})})
    p = parse(f'*** Begin Patch\n*** {operation.title()} File: empty\n*** End Patch')
    diff = to_unified(plan(p, root))
    parsed = from_unified(diff)
    assert parsed.files[0].kind == operation
    git(root, 'apply', '--check', '-', input=diff.encode())
    git(root, 'apply', '-', input=diff.encode())
    assert (root / 'empty').exists() == (operation == 'add')


def test_from_bom_crlf_diff(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    original = b'\xef\xbb\xbfkeep\r\nold\r\n'
    expected = b'\xef\xbb\xbfkeep\r\nnew\r\n'
    for root in (source, target):
        repo(root, {'x': original})
    (source / 'x').write_bytes(expected)
    diff = git(source, 'diff', '--no-ext-diff', '--no-textconv')
    apply(from_unified(diff.decode()), target)
    assert (target / 'x').read_bytes() == expected


def test_from_bom_changed_first_line(tmp_path):
    (tmp_path / 'x').write_bytes(b'\xef\xbb\xbfold\n')
    p = from_unified('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-\ufeffold\n+\ufeffnew\n')
    apply(p, tmp_path)
    assert (tmp_path / 'x').read_bytes() == b'\xef\xbb\xbfnew\n'


@pytest.mark.parametrize('diff', [
    '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-\ufeffold\n+new\n',
    '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\r\n+new\n',
    'diff --git a/x b/x\nnew file mode 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n',
])
def test_reject_representation_loss(diff):
    with pytest.raises(PatchError):
        from_unified(diff)


def test_unicode_separator_not_a_git_line_boundary(tmp_path):
    root = tmp_path / 'r'
    repo(root, {'x': 'a\u2028b\nold\n'.encode()})
    p = parse('*** Begin Patch\n*** Update File: x\n@@\n a\u2028b\n-old\n+new\n*** End Patch')
    diff = to_unified(plan(p, root))
    git(root, 'apply', '-', input=diff.encode())
    assert (root / 'x').read_text() == 'a\u2028b\nnew\n'


def test_native_empty_file_with_space_name(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    for root in (source, target):
        repo(root, {'keep': b'keep\n'})
    (source / 'empty name').touch()
    git(source, 'add', '.')
    diff = git(source, 'diff', '--cached', '--no-ext-diff', '--no-textconv')
    apply(from_unified(diff.decode()), target)
    assert tree(source) == tree(target)
