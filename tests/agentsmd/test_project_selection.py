"""V2 selects at most one project candidate per lexical directory."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save
from .test_snapshot_v2 import document, v2


def dual_project(snapshot):
    winner, shadow = snapshot['entries']
    assert winner['scope'] == shadow['scope'] == 'project'
    assert winner['selected'] and not shadow['selected']
    shadow.update(selected=True, status='loaded', bytes_loaded=3, text_bytes=3)
    snapshot['effective']['rendered_bytes'] = 8  # Correct sum and join, impossible selection.


@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
@pytest.mark.parametrize('position', ['before', 'after', 'self'])
def test_dual_project_reproducer(tree, tmp_path, isolated_codex_home, manifest, position):
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    put(root/'AGENTS.md', 'LOW')
    original = (capture(root, isolated_codex_home, root, cwd, options=['--snapshot-version', 2])
                if manifest else v2(cwd, isolated_codex_home))
    bad = copy.deepcopy(document(original))
    dual_project(bad['directories'][-1]['snapshot'] if manifest else bad)
    good = save(tmp_path/'good.json', original)
    impossible = put(tmp_path/'impossible.json', json.dumps(bad))
    mapping = put(tmp_path/'map.json', json.dumps({'pkg/api': 'pkg/api'}))
    frozen = {p: p.read_bytes() for p in (good, impossible, mapping)}
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    left, right = {'before': (impossible, good), 'after': (good, impossible),
                   'self': (impossible, impossible)}[position]
    options = [[], ['--json'], ['--fail-on-loss'], ['--fail-on-loss', '--json']]
    if manifest:
        options += [['--include-file-changes'], ['--include-file-changes', '--json']]
        options += [['--cwd-map', mapping, *flags] for flags in options.copy()]
        options += [['--cwd-map', mapping, '--include-file-changes', '--fail-on-loss', '--json']]
    for flags in options:
        result = cli('diff', left, right, *flags)
        assert result.returncode == 2, (flags, result.stdout, result.stderr)
        assert not result.stdout
        assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr
    assert all(p.read_bytes() == data for p, data in frozen.items())


@pytest.mark.parametrize('override,regular,rules,budget,winner,status,rendered', [
    (b'WIN', b'LOW', b'RULE', 100, 'AGENTS.override.md', 'loaded', b'WIN\n\nC'),
    (None, b'LOW', b'RULE', 100, 'AGENTS.md', 'loaded', b'LOW\n\nC'),
    (None, None, b'RULE', 100, 'RULES.md', 'loaded', b'RULE\n\nC'),
    (None, None, None, 100, 'OTHER.md', 'loaded', b'OTHER\n\nC'),
    (b'', b'LOW', b'RULE', 100, 'AGENTS.override.md', 'skipped-empty', b'C'),
    (b' \r\n', b'LOW', b'RULE', 100, 'AGENTS.override.md', 'skipped-empty', b'C'),
    (b'', b'LOW', b'RULE', 0, 'AGENTS.override.md', 'dropped-over-budget', b''),
    (b'WIN', b'LOW', b'RULE', 1, 'AGENTS.override.md', 'partial', b'W'),
    (b' \n tail', b'LOW', b'RULE', 1, 'AGENTS.override.md', 'skipped-empty', b'C'),
    (b'\xff', b'LOW', b'RULE', 100, 'AGENTS.override.md', 'loaded', b'\xef\xbf\xbd\n\nC'),
])
def test_authentic_distinct_directory_selection_source_free(tree, isolated_codex_home, monkeypatch,
        override, regular, rules, budget, winner, status, rendered):
    import hashlib
    from agentwrench.agentsmd.core import Reader, Settings, load_settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    for name, body in (('AGENTS.override.md', override), ('AGENTS.md', regular),
                       ('RULES.md', rules), ('OTHER.md', b'OTHER')):
        if body is not None:
            put(root/name, body)
    put(cwd/'RULES.md', 'C')
    # Duplicate/default fallbacks are normalized before discovery.
    settings, _ = load_settings(isolated_codex_home, budget,
                               ['RULES.md', 'OTHER.md', 'RULES.md', 'AGENTS.md'])
    assert settings == Settings(budget, ['RULES.md', 'OTHER.md'])
    modern = create_snapshot(cwd, isolated_codex_home, settings, snapshot_version=2)
    legacy = create_snapshot(cwd, isolated_codex_home, settings, snapshot_version=1)
    selected = [r for r in modern['entries'] if r['selected']]
    assert [r['path'] for r in selected] == [winner, 'pkg/api/RULES.md']
    assert selected[0]['status'] == status
    assert modern['effective'] == dict(sha256=hashlib.sha256(rendered).hexdigest(), rendered_bytes=len(rendered))
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical comparison read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy((modern, legacy))
    assert validate_snapshot(modern) == modern
    for left, right in ((modern, modern), (legacy, modern), (modern, legacy)):
        result = diff_artifacts(left, right)
        assert result['schema_version'] == 1 and not result['effective_changed'] and not result['changes']
    assert diff_artifacts(modern, modern, fail_on_loss=True)['loss_policy']['passed']
    assert (modern, legacy) == frozen


@pytest.mark.parametrize('budget,regular', [(100, b''), (0, b'LOW'), (1, b'LOW')])
def test_empty_selected_winner_still_excludes_fallback(tree, isolated_codex_home, budget, regular):
    from agentwrench.agentsmd.core import ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, validate_snapshot
    root, cwd = tree
    put(root/'AGENTS.override.md', b'')
    put(root/'AGENTS.md', regular)
    good = create_snapshot(cwd, isolated_codex_home, Settings(budget), snapshot_version=2)
    bad = copy.deepcopy(good)
    shadow = bad['entries'][1]
    shadow.update(selected=True, status='skipped-empty' if budget else 'dropped-over-budget')
    if regular and budget:
        shadow.update(status='partial', bytes_loaded=1, text_bytes=1,
                      cut=dict(byte_offset=1, line=1, splits_utf8_character=False))
        bad['effective']['rendered_bytes'] = 1
    elif regular:
        shadow['cut'] = dict(byte_offset=0, line=1, splits_utf8_character=False)
    assert validate_snapshot(good) == good
    with pytest.raises(ScopeError, match='invalid snapshot'):
        validate_snapshot(bad)


def test_project_shadow_failure_v1_and_same_path_global(tree, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader, ScopeError, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, diff_artifacts, validate_snapshot
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    shadow = put(root/'AGENTS.md', 'LOW')
    read = Reader.read
    def optional_failure(self, path):
        if path == shadow:
            raise PermissionError('synthetic optional project hash failure')
        return read(self, path)
    monkeypatch.setattr(Reader, 'read', optional_failure)
    original = create_snapshot(cwd, isolated_codex_home, Settings(), snapshot_version=2)
    assert original['entries'][1]['sha256'] is None
    assert original['entries'][1]['hash_error'] == 'PermissionError'
    good = copy.deepcopy(original)
    good['entries'][1].update(sha256='a'*64, hash_error=None)
    bad = copy.deepcopy(good)
    dual_project(bad)
    legacy = copy.deepcopy(bad)
    legacy['schema_version'] = 1
    for row in legacy['entries']:
        row.pop('cut')
    # Same relative basename and directory in two scopes remain valid.
    assembled = create_snapshot(cwd, root, Settings(), snapshot_version=2)
    assert [(r['scope'], r['path']) for r in assembled['entries'] if r['selected']] == [
        ('global', 'AGENTS.override.md'), ('project', 'AGENTS.override.md')]
    assert assembled['effective']['rendered_bytes'] == 3 + 23 + 3
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('historical validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    frozen = copy.deepcopy((original, good, bad, legacy, assembled))
    for doc in (original, good, legacy, assembled):
        assert validate_snapshot(doc) == doc
    assert diff_artifacts(original, good, fail_on_loss=True)['loss_policy']['passed']
    for left, right in ((good, bad), (bad, good), (bad, bad), (legacy, bad), (bad, legacy)):
        for flags in ({}, dict(fail_on_loss=True)):
            with pytest.raises(ScopeError, match='invalid snapshot'):
                diff_artifacts(left, right, **flags)
    assert (original, good, bad, legacy, assembled) == frozen


@pytest.mark.parametrize('position', ['before', 'after'])
def test_unpaired_dual_project_checked_before_projection(tree, isolated_codex_home, monkeypatch, position):
    from agentwrench.agentsmd.core import Reader, ScopeError
    from agentwrench.agentsmd.snapshot import diff_artifacts
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    put(root/'AGENTS.md', 'LOW')
    bad = document(capture(root, isolated_codex_home, root, cwd, options=['--snapshot-version', 2]))
    other = copy.deepcopy(bad)
    other['directories'].pop()  # No paired comparison would visit the invalid row.
    dual_project(bad['directories'][-1]['snapshot'])
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_projection_or_reads(*args):
        pytest.fail('projected artifacts or read source before validating all nested snapshots')
    monkeypatch.setattr('agentwrench.agentsmd.snapshot.project_manifest', no_projection_or_reads)
    monkeypatch.setattr(Reader, 'read', no_projection_or_reads)
    frozen = copy.deepcopy((bad, other))
    left, right = (bad, other) if position == 'before' else (other, bad)
    for flags in ({}, dict(include_file_changes=True), dict(fail_on_loss=True),
                  dict(cwd_map={'.': '.'}, include_file_changes=True, fail_on_loss=True)):
        with pytest.raises(ScopeError, match='invalid snapshot'):
            diff_artifacts(left, right, **flags)
    assert (bad, other) == frozen
