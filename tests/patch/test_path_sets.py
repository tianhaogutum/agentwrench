"""All touched paths name files; none may be another touched file's directory."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agentwrench.patch import PatchError, check_plan, commit, load_plan, parse, plan, preview
import agentwrench.patch.engine as engine
import agentwrench.patch.planfile as planfile


OVERLAPS = [
    ('*** Add File: node\n+file\n', '*** Add File: node-other\n', '*** Add File: node/child\n+child\n'),
    ('*** Add File: node\n', '*** Update File: source\n*** Move to: node/child\n@@\n keep\n'),
    ('*** Update File: source\n*** Move to: node\n@@\n keep\n', '*** Add File: node/child\n'),
    ('*** Update File: source\n*** Move to: node\n@@\n keep\n',
     '*** Update File: other\n*** Move to: node/child\n@@\n keep\n'),
    ('*** Update File: node\n@@\n keep\n', '*** Add File: node/child\n'),
    ('*** Delete File: node\n', '*** Add File: node/child\n'),
    ('*** Update File: node\n*** Move to: node/child\n@@\n keep\n',),
    ('*** Update File: node/child\n*** Move to: node\n@@\n keep\n',),
]


def envelope(operations):
    return '*** Begin Patch\n' + ''.join(operations) + '*** End Patch\n'


def document(text, root):
    """Handcrafted schema/role-valid artifact, whose path set is impossible."""
    present = {'sha256': hashlib.sha256(b'keep\n').hexdigest(), 'mode': '0644'}
    absent = {'sha256': None, 'mode': None}
    preconditions = {}
    for operation in parse(text).files:
        preconditions[operation.path] = absent if operation.kind == 'add' else present
        if operation.move_to:
            preconditions[operation.move_to] = absent
    return json.dumps({'format': 'v4a-plan', 'version': 1, 'root': str(root),
                       'patch': text, 'preconditions': preconditions, 'diff': ''})


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


@pytest.mark.parametrize('operations', OVERLAPS)
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('route', ['plan', 'preview', 'load_plan', 'check_plan'])
def test_overlap_rejected_before_target_access(tmp_path, monkeypatch, operations, reverse, route):
    # A prior unrelated source must never be read, even if it would be stale.
    text = envelope(('*** Update File: stale\n@@\n keep\n',)
                    + tuple(reversed(operations) if reverse else operations))
    saved = document(text, tmp_path)
    patch = parse(text)
    def forbidden(*args, **kwargs):
        pytest.fail('structural path conflicts must fail before target access')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(engine, 'safe_path', forbidden)
    monkeypatch.setattr(planfile, 'safe_path', forbidden)
    with pytest.raises(PatchError) as error:
        {'plan': plan, 'preview': preview, 'load_plan': load_plan,
         'check_plan': check_plan}[route](saved if route in ('load_plan', 'check_plan') else patch, tmp_path)
    assert 'node' in str(error.value) and 'node/child' in str(error.value)


ROUTES = [('preview',), ('to-unified',), ('apply',), ('apply', '--dry-run'),
          ('apply', '--check-only'), ('check-plan',), ('check-plan', '--json'),
          ('apply-plan',), ('apply-plan', '--dry-run'), ('apply-plan', '--check-only')]


@pytest.mark.parametrize('operations', [OVERLAPS[0], OVERLAPS[3]], ids=['adds', 'move-destinations'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('route', ROUTES)
def test_cli_overlap_is_malformed_and_read_only(tmp_path, operations, reverse, route):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'stale').write_bytes(b'changed\n')
    text = envelope(('*** Update File: stale\n@@\n keep\n',)
                    + tuple(reversed(operations) if reverse else operations))
    saved = document(text, root)
    before = tree(tmp_path)
    data = saved if route[0] in ('check-plan', 'apply-plan') else text
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'patch', route[0], '-',
                             '--root', str(root), *route[1:]], input=data.encode(), capture_output=True)
    assert result.returncode == 2, (result.stdout, result.stderr)
    if '--json' in route or '--check-only' in route:
        assert result.stderr == b''
        report = json.loads(result.stdout)
        assert not report['ok']
        diagnostic, = report['diagnostics']
        if route[0] == 'apply':
            assert diagnostic['code'] == 'INVALID_PATCH'
        else:
            assert diagnostic['codes'] == ['INVALID_PLAN'] and not report['diff_verified']
        reason = diagnostic['reason']
    else:
        assert result.stdout == b''
        reason = result.stderr.decode()
    assert 'node' in reason and 'node/child' in reason
    assert tree(tmp_path) == before


@pytest.mark.parametrize('route', ROUTES)
def test_cli_overlap_never_commits_or_stages(tmp_path, monkeypatch, capsys, route):
    import agentwrench.patch.cli as cli
    import agentwrench.patch.transaction as transaction
    text = envelope(OVERLAPS[0])
    saved = document(text, tmp_path / 'different-root')  # Schema precedes root binding.
    input_file = tmp_path / 'input'
    input_file.write_text(saved if route[0] in ('check-plan', 'apply-plan') else text)
    before = tree(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('malformed paths must not commit or stage')
    monkeypatch.setattr(cli, 'commit', forbidden)
    monkeypatch.setattr(transaction.tempfile, 'mkdtemp', forbidden)
    assert cli.main([route[0], str(input_file), '--root', str(tmp_path), *route[1:]]) == 2
    capsys.readouterr()
    assert tree(tmp_path) == before


def test_segment_boundaries_siblings_and_all_operations_remain_valid(tmp_path):
    for path, data in [('old', b'keep\n'), ('gone', b''), ('update', b'old\n')]:
        (tmp_path / path).write_bytes(data)
    (tmp_path / 'old').chmod(0o750)
    text = envelope(('*** Add File: node\n', '*** Add File: node2/child\n+child\n',
                     '*** Add File: shared/a\n+a\n',
                     '*** Update File: old\n*** Move to: shared/b\n@@\n keep\n',
                     '*** Delete File: gone\n', '*** Update File: update\n@@\n-old\n+new\n'))
    before = tree(tmp_path)
    prepared = plan(parse(text), tmp_path)
    assert prepared.ok
    saved = preview(parse(text), tmp_path)
    assert check_plan(saved, tmp_path)['diff_verified']
    commit(load_plan(saved, tmp_path), dry_run=True)
    assert tree(tmp_path) == before
    commit(load_plan(saved, tmp_path))
    assert (tmp_path / 'node').read_bytes() == b''
    assert (tmp_path / 'node2/child').read_bytes() == b'child\n'
    assert (tmp_path / 'shared/a').read_bytes() == b'a\n'
    assert (tmp_path / 'shared/b').read_bytes() == b'keep\n'
    assert (tmp_path / 'shared/b').stat().st_mode & 0o7777 == 0o750
    assert (tmp_path / 'update').read_bytes() == b'new\n'
    assert not (tmp_path / 'old').exists() and not (tmp_path / 'gone').exists()
