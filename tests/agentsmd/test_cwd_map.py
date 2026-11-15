"""Explicit saved-v2 relocation; no filesystem discovery or inferred pairing."""
import copy
import hashlib
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save


def moved(tree, home, *, before_budget=100, after_budget=100, body='ABCDEF', after_body=None,
          root_marker=False):
    root, _ = tree
    old = root/'pkg'
    put(root/'AGENTS.md', 'ROOT')
    put(old/'AGENTS.md', body)
    if root_marker:
        put(old/'.git', 'gitdir: synthetic')
    before = capture(root, home, old, options=['--snapshot-version', 2, '--max-bytes', before_budget])
    target = root/'services'/'api'
    target.parent.mkdir()
    old.rename(target)
    if after_body is not None:
        put(target/'AGENTS.md', after_body)
    after = capture(root, home, target, options=['--snapshot-version', 2, '--max-bytes', after_budget])
    assert before.returncode == after.returncode == 0
    return json.loads(before.stdout), json.loads(after.stdout)


def compare(tmp_path, before, after, mapping, *options):
    left = put(tmp_path/'before.json', json.dumps(before))
    right = put(tmp_path/'after.json', json.dumps(after))
    path = put(tmp_path/'map.json', json.dumps(mapping) if not isinstance(mapping, str) else mapping)
    return cli('diff', left, right, '--cwd-map', path, *options)


@pytest.mark.parametrize('root_marker', [False, True])
def test_moved_unchanged_source_deleted(tree, tmp_path, isolated_codex_home, root_marker):
    before, after = moved(tree, isolated_codex_home, root_marker=root_marker)
    shutil.rmtree(tree[0])
    shutil.rmtree(isolated_codex_home)
    for flags in [[], ['--include-file-changes'], ['--fail-on-loss', '--include-file-changes']]:
        result = compare(tmp_path, before, after, {'pkg': 'services/api'}, '--json', *flags)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data['schema_version'] == 4 and data['directories'] == []
        assert data['summary']['changed_cwd_count'] == 0
        assert data['relocations'] == [dict(before_cwd='pkg', after_cwd='services/api',
            before_snapshot_cwd='.' if root_marker else 'pkg',
            after_snapshot_cwd='.' if root_marker else 'services/api',
            project_paths=[] if root_marker else [dict(before='pkg/AGENTS.md', after='services/api/AGENTS.md')])]
        if '--include-file-changes' in flags:
            assert data['file_changes'] == []
        if '--fail-on-loss' in flags:
            assert data['loss_policy']['passed'] and data['losses'][0]['cwd'] == 'services/api'
    human = compare(tmp_path, before, after, {'pkg': 'services/api'})
    assert human.returncode == 0 and b'relocated "pkg" -> "services/api"' in human.stdout
    # No map still means removal/addition, never guessed pairing.
    default = cli('diff', tmp_path/'before.json', tmp_path/'after.json', '--json')
    assert default.returncode == 1
    assert [(r['cwd'], r['kind']) for r in json.loads(default.stdout)['directories']] == [
        ('pkg', 'removed'), ('services/api', 'added')]


@pytest.mark.parametrize('case', ['content', 'loss', 'improvement', 'equal-tail', 'whitespace'])
def test_mapped_effective_and_retention(tree, tmp_path, isolated_codex_home, case):
    kwargs = {'after_body': 'UVWXYZ'}
    expected_loss = 0
    if case in ('loss', 'improvement'):
        kwargs = dict(before_budget=10 if case == 'loss' else 6,
                      after_budget=6 if case == 'loss' else 10)
        expected_loss = 4 if case == 'loss' else 0
    elif case == 'equal-tail':
        kwargs = dict(before_budget=6, after_budget=6, after_body='ABmoretail')
    elif case == 'whitespace':
        kwargs = dict(root_marker=True, body=' \n tail', before_budget=3, after_budget=1)
        put(isolated_codex_home/'AGENTS.md', 'A')
    before, after = moved(tree, isolated_codex_home, **kwargs)
    if case == 'whitespace':
        put(isolated_codex_home/'AGENTS.md', 'B')
        after = json.loads(capture(tree[0], isolated_codex_home, tree[0]/'services/api',
            options=['--snapshot-version', 2, '--max-bytes', 1]).stdout)
        expected_loss = 2
    result = compare(tmp_path, before, after, {'pkg': 'services/api'}, '--json',
                     '--include-file-changes', '--fail-on-loss')
    assert result.returncode == int(expected_loss > 0), result.stderr
    data = json.loads(result.stdout)
    assert data['loss_policy']['newly_lost_bytes'] == expected_loss
    assert data['loss_policy']['passed'] == (expected_loss == 0)
    loss = data['losses'][0]['loss']
    if expected_loss:
        assert loss['regressions'] == [dict(scope='project', path='AGENTS.md' if case == 'whitespace'
            else 'services/api/AGENTS.md', newly_lost_bytes=expected_loss, newly_lost_file=False)]
    if case == 'equal-tail':
        assert data['directories'] == [] and loss['regressions'] == []
    else:
        assert data['directories'][0]['cwd'] == 'services/api'
    # Source-independent library comparison must not mutate either input or read anything.
    from agentwrench.agentsmd.snapshot import diff_artifacts
    original = copy.deepcopy((before, after))
    data2 = diff_artifacts(before, after, cwd_map={'pkg': 'services/api'},
                           include_file_changes=True, fail_on_loss=True)
    assert data2 == data and (before, after) == original


def test_mixed_membership_and_empty_map(tree, tmp_path, isolated_codex_home):
    before, after = moved(tree, isolated_codex_home)
    old = copy.deepcopy(before['directories'][0]); old['cwd'] = 'removed'
    new = copy.deepcopy(after['directories'][0]); new['cwd'] = 'added'
    for row in (old, new):
        row['snapshot']['cwd'] = row['cwd']
        row['snapshot']['entries'] = []
        row['snapshot']['effective'] = dict(sha256=hashlib.sha256(b'').hexdigest(), rendered_bytes=0)
    before['directories'].append(old); after['directories'].append(new)
    # Synthetic membership rows retain valid snapshots but are not relocation endpoints.
    result = compare(tmp_path, before, after, {'pkg': 'services/api'}, '--json')
    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    assert [(r['cwd'], r['kind']) for r in data['directories']] == [('removed', 'removed'), ('added', 'added')]
    assert data['summary']['added_cwd_count'] == data['summary']['removed_cwd_count'] == 1
    rejected = compare(tmp_path, before, after, {'pkg': 'services/api'}, '--fail-on-loss')
    assert rejected.returncode == 2 and rejected.stdout == b''
    # Empty map preserves report values (only explicit schema/metadata differs).
    plain = cli('diff', tmp_path/'before.json', tmp_path/'after.json', '--json')
    explicit = compare(tmp_path, before, after, {}, '--json')
    assert plain.returncode == explicit.returncode == 1
    assert json.loads(explicit.stdout) == {**json.loads(plain.stdout), 'schema_version': 4, 'relocations': []}


@pytest.mark.parametrize('mapping', [[], None, 3, {'pkg': 4}, {'pkg': '../services/api'},
    {'./pkg': 'services/api'}, {'pkg': 'services//api'}, {'absent': 'services/api'},
    {'pkg': 'absent'}, '{"pkg":"services/api","pkg":"services/api"}',
    '{"pkg":"services/api",}', 'NaN'])
@pytest.mark.parametrize('flags', [[], ['--fail-on-loss', '--json']])
def test_invalid_maps(tree, tmp_path, isolated_codex_home, mapping, flags):
    before, after = moved(tree, isolated_codex_home)
    result = compare(tmp_path, before, after, mapping, *flags)
    assert result.returncode == 2 and result.stdout == b'' and b'Traceback' not in result.stderr


@pytest.mark.parametrize('case', ['v1', 'mixed-version', 'single', 'unknown', 'nested-cwd',
    'root-found', 'root-mismatch', 'duplicate-destination', 'membership-collision',
    'overlap', 'file-collision'])
def test_invalid_projection(tree, tmp_path, isolated_codex_home, case):
    before, after = moved(tree, isolated_codex_home)
    mapping = {'pkg': 'services/api'}
    if case in ('v1', 'mixed-version'):
        before['schema_version'] = 1
        before['directories'][0]['snapshot']['schema_version'] = 1
        for row in before['directories'][0]['snapshot']['entries']:
            row.pop('cut')
        if case == 'v1':
            after = copy.deepcopy(before)
            after['directories'][0]['cwd'] = 'services/api'
    elif case == 'single':
        before, after = before['directories'][0]['snapshot'], after['directories'][0]['snapshot']
    elif case == 'unknown':
        after['schema_version'] = 3
    elif case == 'nested-cwd':
        after['directories'][0]['snapshot']['cwd'] = 'wrong'
    elif case == 'root-found':
        after['directories'][0]['snapshot']['root_found'] = False
    elif case == 'root-mismatch':
        after['directories'][0]['snapshot']['cwd'] = '.'
    elif case in ('duplicate-destination', 'membership-collision', 'overlap'):
        row = copy.deepcopy(before['directories'][0])
        row['cwd'] = 'pkg/sub' if case == 'overlap' else 'services/api'
        row['snapshot']['cwd'] = row['cwd']
        before['directories'].append(row)
        if case == 'duplicate-destination':
            mapping[row['cwd']] = 'services/api'
        elif case == 'overlap':
            row = copy.deepcopy(after['directories'][0]); row['cwd'] = 'elsewhere'
            row['snapshot']['cwd'] = 'elsewhere'; after['directories'].append(row)
            mapping['pkg/sub'] = 'elsewhere'
    else:
        row = copy.deepcopy(before['directories'][0]['snapshot']['entries'][-1])
        row['path'] = 'services/api/AGENTS.md'; row['order'] += 1
        before['directories'][0]['snapshot']['entries'].append(row)
    for flags in [[], ['--include-file-changes', '--fail-on-loss', '--json']]:
        result = compare(tmp_path, before, after, mapping, *flags)
        assert result.returncode == 2 and result.stdout == b'' and b'Traceback' not in result.stderr


@pytest.mark.parametrize('case', ['overlap', 'swap', 'base', 'base-dot', 'identity', 'no-root'])
def test_bijective_moves_and_overlap_order(tree, tmp_path, isolated_codex_home, case):
    root, _ = tree
    if case in ('base', 'base-dot'):
        # Move the entire captured discovery root within a common base.
        before_root = root/'old'; after_root = root/'new'
        put(before_root/'.git', 'gitdir: synthetic')
        put(before_root/'AGENTS.md', 'BASE')
        before = capture(before_root if case == 'base-dot' else root, isolated_codex_home, before_root,
                         options=['--snapshot-version', 2])
        before_root.rename(after_root)
        after = capture(root, isolated_codex_home, after_root,
                        options=['--snapshot-version', 2])
        mapping = {'.' if case == 'base-dot' else 'old': 'new'}
    elif case == 'no-root':
        (root/'.git').unlink()
        put(root/'pkg/AGENTS.md', 'BODY')
        before = capture(root, isolated_codex_home, root/'pkg',
                         options=['--snapshot-version', 2, '--no-root-markers'])
        (root/'pkg').rename(root/'new')
        after = capture(root, isolated_codex_home, root/'new',
                        options=['--snapshot-version', 2, '--no-root-markers'])
        mapping = {'pkg': 'new'}
    else:
        put(root/'AGENTS.md', 'ROOT')
        put(root/'pkg/AGENTS.md', 'LEFT')
        put(root/'other/AGENTS.md', 'RIGHT')
        cwds = [root/'pkg', root/'pkg/api'] if case == 'overlap' else [root/'pkg', root/'other']
        before = capture(root, isolated_codex_home, *cwds, options=['--snapshot-version', 2])
        if case == 'overlap':
            (root/'pkg').rename(root/'new')
            new_cwds = [root/'new/api', root/'new']
            mapping = {'pkg/api': 'new/api', 'pkg': 'new'}  # report follows baseline, not map order
        elif case == 'swap':
            (root/'pkg').rename(root/'hold'); (root/'other').rename(root/'pkg')
            (root/'hold').rename(root/'other')
            new_cwds = [root/'pkg', root/'other']
            mapping = {'pkg': 'other', 'other': 'pkg'}
        else:
            new_cwds = list(reversed(cwds)); mapping = {'.': '.'}
            # Include '.' as an existing endpoint alongside unchanged cwds.
            before = capture(root, isolated_codex_home, root, *cwds, options=['--snapshot-version', 2])
            new_cwds.append(root)
        after = capture(root, isolated_codex_home, *new_cwds, options=['--snapshot-version', 2])
    assert before.returncode == after.returncode == 0
    left, right = json.loads(before.stdout), json.loads(after.stdout)
    result = compare(tmp_path, left, right, mapping, '--json', '--include-file-changes', '--fail-on-loss')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['directories'] == data['file_changes'] == [] and data['loss_policy']['passed']
    assert [r['before_cwd'] for r in data['relocations']] == [
        r['cwd'] for r in left['directories'] if r['cwd'] in mapping]


def test_no_map_library_compatibility_and_no_reads(tree, tmp_path, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader
    from agentwrench.agentsmd.snapshot import diff_artifacts
    before, after = moved(tree, isolated_codex_home, after_body='UVWXYZ')
    original = copy.deepcopy((before, after))
    def forbidden(*args, **kwargs):
        pytest.fail('historical comparison attempted a file read')
    monkeypatch.setattr(Reader, 'read', forbidden)
    mapped = diff_artifacts(before, after, cwd_map={'pkg': 'services/api'}, include_file_changes=True)
    assert [r['cwd'] for r in mapped['file_changes']] == ['services/api']
    detail = mapped['file_changes'][0]
    assert detail['context_changes'] == {}
    assert [(r['path'], r['kind']) for r in detail['changes']] == [('services/api/AGENTS.md', 'changed')]
    assert (before, after) == original
    default = diff_artifacts(before, after)
    assert default == diff_artifacts(before, after, cwd_map=None)
    assert default['schema_version'] == 1 and 'relocations' not in default
    assert default['summary'] == dict(before_cwd_count=1, after_cwd_count=1, changed_cwd_count=2,
                                      added_cwd_count=1, removed_cwd_count=1)


@pytest.mark.parametrize('contents', [b'\xff', b'{"pkg":"services/api","pkg":"absent"}',
                                      b'{"pkg":"/absolute"}', b'{"pkg":""}'])
def test_unreadable_maps_and_missing_file(tree, tmp_path, isolated_codex_home, contents):
    before, after = moved(tree, isolated_codex_home)
    left = put(tmp_path/'before.json', json.dumps(before)); right = put(tmp_path/'after.json', json.dumps(after))
    path = put(tmp_path/'map.json', contents)
    for target in (path, tmp_path/'missing.json'):
        result = cli('diff', left, right, '--cwd-map', target, '--json')
        assert result.returncode == 2 and result.stdout == b'' and b'Traceback' not in result.stderr


def test_outside_base_and_root_collisions_rejected(tree, tmp_path, isolated_codex_home):
    before, after = moved(tree, isolated_codex_home, root_marker=True)
    # Separate baseline discovery roots cannot collapse to one projected root,
    # even if no saved files happen to collide.
    row = copy.deepcopy(before['directories'][0]); row['cwd'] = 'services/api/sub'
    row['snapshot']['cwd'] = 'sub'; row['snapshot']['entries'] = []
    row['snapshot']['effective'] = dict(sha256=hashlib.sha256(b'').hexdigest(), rendered_bytes=0)
    before['directories'].append(row)
    result = compare(tmp_path, before, after, {'pkg': 'services/api'}, '--json')
    assert result.returncode == 2 and b'root coordinates collide' in result.stderr and result.stdout == b''
    # A root above the explicit base lacks the parent-name relationship needed
    # to project into another base. Reject rather than guessing or reading it.
    before['directories'].pop()
    before['directories'][0]['snapshot']['cwd'] = 'outside/pkg'
    result = compare(tmp_path, before, after, {'pkg': 'services/api'}, '--json')
    assert result.returncode == 2 and b'outside the saved base' in result.stderr and result.stdout == b''


def test_map_pair_limit_and_file_read_bound(tmp_path, monkeypatch):
    from agentwrench.agentsmd.core import MAX_FILE_BYTES, ScopeError
    from agentwrench.agentsmd.relocation import read_cwd_map, validate_cwd_map
    assert len(validate_cwd_map({str(i): str(i) for i in range(64)})) == 64
    with pytest.raises(ScopeError, match='at most 64'):
        validate_cwd_map({str(i): str(i) for i in range(65)})
    path = tmp_path/'oversized.json'
    with path.open('wb') as stream:
        stream.truncate(MAX_FILE_BYTES+1)
    with pytest.raises(ScopeError):
        read_cwd_map(path)


def test_unmapped_removed_descendant_keeps_raw_membership(tree, tmp_path, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'ROOT'); put(root/'pkg/AGENTS.md', 'BODY')
    before = capture(root, isolated_codex_home, root/'pkg', cwd, options=['--snapshot-version', 2])
    (root/'pkg').rename(root/'new')
    after = capture(root, isolated_codex_home, root/'new', root/'new/api', options=['--snapshot-version', 2])
    result = compare(tmp_path, json.loads(before.stdout), json.loads(after.stdout), {'pkg': 'new'},
                     '--json', '--include-file-changes')
    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    assert [(r['cwd'], r['kind']) for r in data['directories']] == [('pkg/api', 'removed'), ('new/api', 'added')]
    assert [(r['cwd'], r['kind']) for r in data['file_changes']] == [('pkg/api', 'removed'), ('new/api', 'added')]
    assert data['file_changes'][0]['changes'][-1]['path'] == 'pkg/AGENTS.md'
    assert data['file_changes'][1]['changes'][-1]['path'] == 'new/AGENTS.md'
