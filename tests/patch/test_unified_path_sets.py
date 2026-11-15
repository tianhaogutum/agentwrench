"""Unified import must not emit file path sets the planner cannot represent."""
import pytest

from agentwrench.patch import PatchError, apply, from_unified, parse
import agentwrench.patch.cli as cli
import agentwrench.patch.transaction as transaction
import agentwrench.patch.unified as unified


def add(path):
    return f'diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+file\n'


def delete(path):
    return f'--- a/{path}\n+++ /dev/null\n@@ -1 +0,0 @@\n-file\n'


def move(source, destination, *, pure=False):
    metadata = (f'diff --git a/{source} b/{destination}\n'
                f'rename from {source}\nrename to {destination}\n')
    return metadata if pure else metadata + f'--- a/{source}\n+++ b/{destination}\n@@ -1 +1 @@\n-old\n+new\n'


CONFLICTS = [
    pytest.param((add('node'), delete('node/child')), ('node', 'node/child'), id='transition'),
    pytest.param((add('node'), add('node-other'), add('node/child')), ('node', 'node/child'), id='nonadjacent'),
    pytest.param((add('node'), move('source', 'node/child')), ('node', 'node/child'), id='move-child'),
    pytest.param((move('source', 'node'), add('node/child')), ('node', 'node/child'), id='move-parent'),
    pytest.param((move('source', 'node'), move('other', 'node/child')), ('node', 'node/child'), id='move-destinations'),
    pytest.param((move('node', 'elsewhere'), delete('node/child')), ('node', 'node/child'), id='move-source'),
    pytest.param((move('node', 'node/child'),), ('node', 'node/child'), id='within-move-down'),
    pytest.param((move('node/child', 'node'),), ('node', 'node/child'), id='within-move-up'),
    pytest.param((add('node'), add('node')), ('node',), id='duplicate'),
    pytest.param((delete('node'), add('node')), ('node',), id='delete-add'),
    pytest.param((move('source', 'node'), move('other', 'node')), ('node',), id='duplicate-destination'),
    pytest.param((move('source', 'node'), add('source')), ('source',), id='duplicate-source'),
    pytest.param(('diff --git a/node b/node\nnew file mode 100644\n',
                  'diff --git a/node/child b/node/child\ndeleted file mode 100644\n'),
                 ('node', 'node/child'), id='metadata-only'),
]


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


def forbid(*args, **kwargs):
    pytest.fail('conversion must not commit or stage')


@pytest.mark.parametrize('sections,paths', CONFLICTS)
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('route', ['api', 'cli'])
def test_import_rejects_impossible_path_sets(tmp_path, monkeypatch, capsys, sections, paths, reverse, route):
    text = ''.join(reversed(sections) if reverse else sections)
    input_file = tmp_path / 'input.diff'
    input_file.write_text(text)
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'keep').write_bytes(b'unchanged\r\n')
    (root / 'keep').chmod(0o751)
    before = tree(tmp_path)
    monkeypatch.setattr(cli, 'commit', forbid)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbid)
    # These imports have hunk context or metadata-only add/delete paths; no
    # target-tree read is needed to diagnose their final path set.
    monkeypatch.setattr(unified, 'safe_path', forbid)
    if route == 'api':
        with pytest.raises(PatchError) as error:
            from_unified(text, root=root)
        reason = str(error.value)
    else:
        assert cli.main(['from-unified', str(input_file), '--root', str(root)]) == 2
        output = capsys.readouterr()
        assert output.out == ''
        reason = output.err
    assert all(path in reason for path in paths)
    assert ('file/descendant overlap' if len(paths) == 2 else 'only once') in reason
    assert tree(tmp_path) == before


@pytest.mark.parametrize('contents', [b'', b'keep\n'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('case', ['within-move', 'destination', 'duplicate'])
def test_pure_rename_final_paths_checked_after_context_read(tmp_path, contents, reverse, case):
    source = 'node' if case == 'within-move' else 'source'
    destination = 'node/child' if case == 'within-move' else 'node'
    (tmp_path / source).write_bytes(contents)
    sections = [move(source, destination, pure=True)]
    if case != 'within-move':
        sections.append(add('node/child' if case == 'destination' else 'node'))
    before = tree(tmp_path)
    with pytest.raises(PatchError) as error:
        from_unified(''.join(reversed(sections) if reverse else sections), root=tmp_path)
    assert 'node' in str(error.value)
    assert ('only once' if case == 'duplicate' else 'node/child') in str(error.value)
    assert tree(tmp_path) == before


@pytest.mark.parametrize('empty_rename', [False, True])
def test_final_import_operation_budget(tmp_path, empty_rename):
    sections = [add(f'file{i}') for i in range(999)]
    if empty_rename:
        (tmp_path / 'source').write_bytes(b'')
        sections.append(move('source', 'destination', pure=True))  # Expands to delete + add.
    else:
        sections.extend([add('source'), add('destination')])
    with pytest.raises(PatchError, match='at most 1000 file operations'):
        from_unified(''.join(sections), root=tmp_path)
    patch = from_unified(''.join(sections[:998] + sections[999:]), root=tmp_path)
    assert len(patch.files) == 1000


def test_supported_segment_boundaries_and_operations_apply(tmp_path):
    for path in ('source', 'update', 'gone'):
        (tmp_path / path).write_bytes(b'old\n')
    (tmp_path / 'source').chmod(0o751)
    text = (add('node') + add('node2/child') + add('shared/a')
            + move('source', 'shared/b') + delete('gone')
            + '--- a/update\n+++ b/update\n@@ -1 +1 @@\n-old\n+new\n')
    before = tree(tmp_path)
    patch = from_unified(text)
    assert tree(tmp_path) == before
    apply(parse(patch.to_text()), tmp_path)
    assert tree(tmp_path).keys() == {'node', 'node2', 'node2/child', 'shared', 'shared/a', 'shared/b', 'update'}
    assert (tmp_path / 'shared/b').stat().st_mode & 0o7777 == 0o751
    assert (tmp_path / 'shared/b').read_bytes() == (tmp_path / 'update').read_bytes() == b'new\n'
    for path in ('node', 'node2/child', 'shared/a'):
        assert (tmp_path / path).read_bytes() == b'file\n'
