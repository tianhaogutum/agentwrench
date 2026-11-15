"""Source-independent v2 status/retention contracts, before any diff verdict."""
import copy
import json
import shutil

import pytest

from .conftest import put
from .test_cli import cli
from .test_manifest import capture
from .test_snapshot import save, snapshot
from .test_snapshot_v2 import document, v2


# Authentic captures are mutated without touching their effective fingerprint.
# The first case is round 9's retained reproduce_uncut_status.py reproducer.
@pytest.mark.parametrize('data,budget,scope,changes', [
    pytest.param(b'BASE', 100, 'project', dict(status='partial'), id='uncut-partial'),
    pytest.param(b'BASE', 100, 'project', dict(status='dropped-over-budget'), id='uncut-dropped'),
    pytest.param(b'BASE', 100, 'project', dict(bytes_loaded=3), id='uncut-short-retention'),
    pytest.param(b'BASE', 100, 'project', dict(bytes_loaded=0), id='loaded-uncharged'),
    pytest.param(b'BASE', 100, 'project', dict(text_bytes=0), id='loaded-empty-text'),
    pytest.param(b'BASE', 100, 'project', dict(text_bytes=3), id='project-text-shrinks'),
    pytest.param(b'BASE', 100, 'project', dict(text_bytes=13), id='project-text-overexpands'),
    pytest.param(b'BASE', 100, 'project', dict(status='skipped-empty'), id='skipped-charged'),
    pytest.param(b'BASE', 100, 'project', dict(status='skipped-empty', bytes_loaded=0), id='skipped-retains-text'),
    pytest.param(b'', 0, 'project', dict(status='skipped-empty'), id='zero-budget-empty-skipped'),
    pytest.param(b'', 1, 'project', dict(status='loaded'), id='empty-loaded'),
    pytest.param(b'BASE', 3, 'project', dict(text_bytes=0), id='partial-empty-text'),
    pytest.param(b'BASE', 3, 'project', dict(text_bytes=2), id='partial-text-shrinks'),
    pytest.param(b'BASE', 3, 'project', dict(text_bytes=10), id='partial-text-overexpands'),
    pytest.param(b' \n tail', 3, 'project', dict(text_bytes=1), id='skipped-cut-retains-text'),
    pytest.param(b'BASE', 0, 'project', dict(text_bytes=1), id='dropped-retains-text'),
    pytest.param(b'BASE', 100, 'global', dict(status='partial', selected=False), id='global-partial'),
    pytest.param(b'BASE', 100, 'global', dict(status='dropped-over-budget', selected=False), id='global-dropped'),
    pytest.param(b'BASE', 100, 'global', dict(bytes_loaded=2), id='global-short-retention'),
    pytest.param(b'BASE', 100, 'global', dict(text_bytes=0), id='global-loaded-empty-text'),
    pytest.param(b'BASE', 100, 'global', dict(text_bytes=10), id='global-text-overexpands'),
    pytest.param(b'BASE', 100, 'global', dict(status='skipped-empty', selected=False), id='global-skipped-charged'),
])
@pytest.mark.parametrize('manifest', [False, True], ids=['snapshot', 'manifest'])
def test_impossible_v2_retention_rejected(tree, tmp_path, isolated_codex_home,
                                         data, budget, scope, changes, manifest):
    root, cwd = tree
    put(root/'AGENTS.md', data)
    put(isolated_codex_home/'AGENTS.md', ' G ')
    before = (capture(root, isolated_codex_home, root, cwd,
                      options=['--snapshot-version', 2, '--max-bytes', budget]) if manifest
              else v2(cwd, isolated_codex_home, '--max-bytes', budget))
    doc = copy.deepcopy(document(before))
    nested = doc['directories'][-1]['snapshot'] if manifest else doc
    row = next(row for row in nested['entries'] if row['scope'] == scope)
    row.update(changes)
    old = save(tmp_path/'before.json', before)
    bad = put(tmp_path/'impossible.json', json.dumps(doc))
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    options = [[], ['--json'], ['--fail-on-loss'], ['--fail-on-loss', '--json']]
    if manifest:
        options.append(['--include-file-changes', '--fail-on-loss', '--json'])
    for left, right in ((old, bad), (bad, old)):
        for flags in options:
            result = cli('diff', left, right, *flags)
            assert result.returncode == 2, (flags, result.stdout, result.stderr)
            assert not result.stdout
            assert b'invalid snapshot' in result.stderr and b'Traceback' not in result.stderr


@pytest.mark.parametrize('data,budget,status,loaded,text_bytes', [
    (b'', 0, 'dropped-over-budget', 0, 0),
    (b'', 1, 'skipped-empty', 0, 0),
    (b' \n', 2, 'skipped-empty', 0, 0),
    (b' \n tail', 3, 'skipped-empty', 0, 0),
    ('\u2003tail'.encode(), 3, 'skipped-empty', 0, 0),
    (b'\xff', 1, 'loaded', 1, 3),
    (b'\xe2\x82', 2, 'loaded', 2, 3),
    ('A\n€Z'.encode(), 3, 'partial', 3, 5),
])
def test_authentic_retention_edges_source_free(tree, tmp_path, isolated_codex_home,
                                               data, budget, status, loaded, text_bytes):
    root, cwd = tree
    put(root/'AGENTS.md', data)
    put(cwd/'AGENTS.md', 'NEXT')
    put(isolated_codex_home/'AGENTS.override.md', ' \n')
    put(isolated_codex_home/'AGENTS.md', b' \xff \n')
    before = v2(cwd, isolated_codex_home, '--max-bytes', budget)
    doc = document(before)
    assert (doc['entries'][2]['status'], doc['entries'][2]['bytes_loaded'],
            doc['entries'][2]['text_bytes']) == (status, loaded, text_bytes)
    assert doc['entries'][0]['status'] == 'skipped-empty'
    assert doc['entries'][1]['bytes_loaded'] == 4 and doc['entries'][1]['text_bytes'] == 3
    assert doc['entries'][-1]['bytes_loaded'] == min(budget-loaded, 4)
    old = save(tmp_path/'before.json', before)
    legacy = save(tmp_path/'legacy.json', snapshot(cwd, isolated_codex_home, '--max-bytes', budget))
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    for left, right, flags in ((old, old, ['--fail-on-loss', '--json']),
                               (legacy, old, ['--json']), (old, legacy, ['--json'])):
        result = cli('diff', left, right, *flags)
        assert result.returncode == 0, result.stderr
        assert not json.loads(result.stdout)['effective_changed']



def test_optional_shadow_hash_failure_needs_no_source_for_validation(tree, isolated_codex_home, monkeypatch):
    from agentwrench.agentsmd.core import Reader, Settings
    from agentwrench.agentsmd.snapshot import create_snapshot, validate_snapshot
    root, cwd = tree
    put(root/'AGENTS.override.md', 'WIN')
    shadow = put(root/'AGENTS.md', 'HIDDEN')
    read = Reader.read
    def optional_failure(self, path):
        if path == shadow:
            raise PermissionError('synthetic optional hash error')
        return read(self, path)
    monkeypatch.setattr(Reader, 'read', optional_failure)
    doc = create_snapshot(cwd, isolated_codex_home, Settings(100), snapshot_version=2)
    hidden = doc['entries'][1]
    assert hidden['sha256'] is None and hidden['hash_error'] == 'PermissionError'
    shutil.rmtree(root)
    shutil.rmtree(isolated_codex_home)
    def no_reads(*args):
        pytest.fail('artifact validation read source')
    monkeypatch.setattr(Reader, 'read', no_reads)
    assert validate_snapshot(doc) == doc
