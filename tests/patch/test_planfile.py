import hashlib
import json
from pathlib import Path

import pytest

from agentwrench.patch import ApplyError, PatchError, commit, load_plan, parse, preview
import agentwrench.patch.planfile as planfile
import agentwrench.patch.transaction as transaction


PATCH = '''*** Begin Patch
*** Update File: one
@@
-old
+new
*** Add File: nested/new
+created
*** Delete File: two
*** End Patch
'''


def fixture(root):
    (root / 'one').write_bytes(b'old\n')
    (root / 'one').chmod(0o750)
    (root / 'two').write_bytes(b'bye\n')
    return preview(parse(PATCH), root)


def tree(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode,
                                     p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def test_load_then_concurrent_edit_rejected_before_staging(tmp_path, monkeypatch):
    prepared = load_plan(fixture(tmp_path), tmp_path)
    (tmp_path / 'one').write_text('old\nextra\n')
    before = tree(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('stale plan must not stage data')
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbidden)
    with pytest.raises(ApplyError, match='changed since planning'):
        commit(prepared)
    assert tree(tmp_path) == before


def test_saved_plan_commit_failure_rolls_back(tmp_path, monkeypatch):
    prepared = load_plan(fixture(tmp_path), tmp_path)
    before = tree(tmp_path)
    unlink = Path.unlink
    def fail(self, *args, **kwargs):
        if self == tmp_path / 'two':
            raise OSError('injected final deletion failure')
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(ApplyError, match='rolled back'):
        commit(prepared)
    assert tree(tmp_path) == before


def test_wrong_root_even_when_bytes_match(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    saved = fixture(a)
    fixture(b)
    before = tree(tmp_path)
    with pytest.raises(ApplyError, match='different root'):
        load_plan(saved, b)
    assert tree(tmp_path) == before
    alias = tmp_path / 'alias'
    alias.symlink_to(a, target_is_directory=True)
    assert load_plan(saved, alias).root == a


@pytest.mark.parametrize('drift', ['symlink', 'parent-symlink', 'directory', 'binary', 'oversized'])
def test_stale_special_files_rejected_without_staging(tmp_path, monkeypatch, drift):
    root = tmp_path / 'root'
    root.mkdir()
    saved = fixture(root)
    if drift == 'symlink':
        outside = tmp_path / 'outside'
        outside.write_bytes(b'old\n')
        (root / 'one').unlink()
        (root / 'one').symlink_to(outside)
    elif drift == 'parent-symlink':
        (root / 'nested').symlink_to(tmp_path, target_is_directory=True)
    elif drift == 'directory':
        (root / 'two').unlink()
        (root / 'two').mkdir()
    elif drift == 'binary':
        (root / 'two').write_bytes(b'\x00\xff')
    else:
        monkeypatch.setattr(planfile, 'MAX_FILE_BYTES', 10)
        (root / 'two').write_bytes(b'x' * 11)
    before = tree(root)
    with pytest.raises(ApplyError, match='changed since planning'):
        load_plan(saved, root)
    assert tree(root) == before


@pytest.mark.parametrize('damage', ['format', 'version', 'bool-version', 'missing-field',
                                    'extra-field', 'missing-path', 'extra-path', 'unsafe-path',
                                    'hash', 'mode', 'null-mode', 'diff', 'patch', 'entry-type',
                                    'root-type', 'patch-type', 'preconditions-type', 'duplicate-path'])
def test_malformed_plan_is_rejected_without_writes(tmp_path, damage):
    doc = json.loads(fixture(tmp_path))
    if damage == 'format':
        doc['format'] = 'other'
    elif damage == 'version':
        doc['version'] = 2
    elif damage == 'bool-version':
        doc['version'] = True
    elif damage == 'missing-field':
        del doc['diff']
    elif damage == 'extra-field':
        doc['extra'] = 1
    elif damage == 'missing-path':
        del doc['preconditions']['two']
    elif damage == 'extra-path':
        doc['preconditions']['extra'] = {'sha256': None, 'mode': None}
    elif damage == 'unsafe-path':
        doc['preconditions']['../outside'] = {'sha256': None, 'mode': None}
    elif damage == 'hash':
        doc['preconditions']['one']['sha256'] = 'broken'
    elif damage == 'mode':
        doc['preconditions']['one']['mode'] = 0o750
    elif damage == 'null-mode':
        doc['preconditions']['one']['mode'] = None
    elif damage == 'diff':
        doc['diff'] += 'unreviewed change\n'
    elif damage == 'patch':
        doc['patch'] = doc['patch'].replace('+new\n', '+unreviewed\n')
    elif damage == 'entry-type':
        doc['preconditions']['one'] = []
    elif damage in ('root-type', 'patch-type', 'preconditions-type'):
        doc[damage.removesuffix('-type')] = []
    else:
        doc['patch'] = doc['patch'].replace('*** End Patch', '*** Delete File: one\n*** End Patch')
    before = tree(tmp_path)
    with pytest.raises(PatchError):
        load_plan(json.dumps(doc), tmp_path)
    assert tree(tmp_path) == before


@pytest.mark.parametrize('text', ['[]', '{', '{"version":1,"version":1}', '[' * 2000])
def test_invalid_json(text, tmp_path):
    with pytest.raises(PatchError):
        load_plan(text, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_plan_size_limit(tmp_path, monkeypatch):
    saved = fixture(tmp_path)
    before = tree(tmp_path)
    monkeypatch.setattr(planfile, 'MAX_PLAN_BYTES', len(saved.encode('utf-8')) - 1)
    with pytest.raises(PatchError, match='64 MiB'):
        load_plan(saved, tmp_path)
    with pytest.raises(PatchError, match='64 MiB'):
        preview(parse(PATCH), tmp_path)
    assert tree(tmp_path) == before


def test_source_total_limit(tmp_path, monkeypatch):
    saved = fixture(tmp_path)
    before = tree(tmp_path)
    monkeypatch.setattr(planfile, 'MAX_TOTAL_BYTES', 6)
    with pytest.raises(ApplyError, match='total source data exceeds 64 MiB'):
        load_plan(saved, tmp_path)
    assert tree(tmp_path) == before


def test_empty_files_unrelated_edits_and_replay(tmp_path):
    (tmp_path / 'empty').touch()
    (tmp_path / 'keep').write_text('unrelated\n')
    saved = preview(parse('*** Begin Patch\n*** Delete File: empty\n*** Add File: new\n*** End Patch\n'), tmp_path)
    doc = json.loads(saved)
    assert doc['preconditions']['empty']['sha256'] is not None
    assert doc['preconditions']['new']['sha256'] is None
    (tmp_path / 'keep').write_text('unrelated writer change\n')
    commit(load_plan(saved, tmp_path))
    assert not (tmp_path / 'empty').exists() and (tmp_path / 'new').read_bytes() == b''
    assert (tmp_path / 'keep').read_text() == 'unrelated writer change\n'
    before = tree(tmp_path)
    with pytest.raises(ApplyError, match='changed since planning'):
        load_plan(saved, tmp_path)
    assert tree(tmp_path) == before


@pytest.mark.parametrize('operation', ['update', 'delete', 'move-source', 'add', 'move-target'])
@pytest.mark.parametrize('loader', [planfile.check_plan, load_plan], ids=['check-plan', 'load-plan'])
def test_operation_roles_rejected_before_target_access(tmp_path, monkeypatch, operation, loader):
    if operation == 'add':
        patch = parse('*** Begin Patch\n*** Add File: new\n*** End Patch\n')
        path = 'new'
    elif operation.startswith('move'):
        (tmp_path / 'one').write_bytes(b'old\n')
        patch = parse('*** Begin Patch\n*** Update File: one\n*** Move to: new\n@@\n old\n*** End Patch\n')
        path = 'one' if operation == 'move-source' else 'new'
    else:
        (tmp_path / 'one').write_bytes(b'old\n' if operation == 'update' else b'')
        patch = parse('*** Begin Patch\n*** ' + operation.title() + ' File: one\n'
                      + ('@@\n-old\n+new\n' if operation == 'update' else '') + '*** End Patch\n')
        path = 'one'
    doc = json.loads(preview(patch, tmp_path))
    doc['preconditions'][path] = ({'sha256': hashlib.sha256(b'').hexdigest(), 'mode': '0644'} if path == 'new'
                                else {'sha256': None, 'mode': None})
    saved = json.dumps(doc)
    def forbidden(*args, **kwargs):
        pytest.fail('invalid operation roles must fail before accessing the target tree')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(planfile, 'safe_path', forbidden)
    monkeypatch.setattr(planfile, 'plan', forbidden)
    with pytest.raises(PatchError, match=path):
        loader(saved, tmp_path)
