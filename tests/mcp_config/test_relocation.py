"""Explicit project path selections in synthetic native configs; no filesystem lookup."""
import copy
import json
from pathlib import Path

import pytest

from agentwrench.mcpconfig.formats import FORMATS, loads, new_document
from .test_bridge import invoke


def relocation_args(source, fmt='codex', *paths):
    return ['relocate', '--format', fmt, source,
            '--from-root', '/old/project', '--to-root', '/new/project',
            *[arg for path in paths for arg in ('--path', json.dumps(path))]]


@pytest.mark.parametrize('fmt', FORMATS)
def test_only_explicit_argument_is_relocated_with_provenance(tmp_path, fmt):
    raw = {'worker.工具': {'command': '/old/project/bin/server',
                       'args': ['--data', '/old/project/data', '/old/project/literal',
                                '--url=https://example.test/old/project', '${ROOT}/data'],
                       'env': {'DATA': '/old/project/data', 'ROOT': '${ROOT}'}},
           'remote': {'url': 'https://example.test/old/project/data'},
           'unselected': {'command': 'server', 'args': ['../ambiguous']}}
    if fmt == 'continue':
        raw['remote']['type'] = 'streamable-http'
    source = tmp_path / 'source'
    original = new_document(fmt, raw)
    source.write_text(original)
    args = relocation_args(source, fmt, ['worker.工具', 'args', 1])
    preview = invoke(*args, '--dry-run')
    assert preview.returncode == 0, preview.stderr
    plan = json.loads(preview.stdout)
    assert plan == {
        'schemaVersion': 1, 'operation': 'relocate', 'scope': 'project', 'format': fmt,
        'sourceRoot': '/old/project', 'targetRoot': '/new/project',
        'relocations': [{'path': ['worker.工具', 'args', 1], 'basis': 'explicit-selection',
                         'relativePath': 'data', 'before': '/old/project/data',
                         'after': '/new/project/data'}],
    }
    assert source.read_text() == original and list(tmp_path.iterdir()) == [source]
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    expected = copy.deepcopy(raw)
    expected['worker.工具']['args'][1] = '/new/project/data'
    assert loads(result.stdout, fmt).servers == expected
    assert json.loads(result.stderr) == plan
    assert source.read_text() == original


@pytest.mark.parametrize('fmt', ['codex', 'continue', 'vscode'])
def test_cwd_and_root_itself_are_selected_paths(tmp_path, fmt):
    source = tmp_path / 'source'
    source.write_text(new_document(fmt, {'worker': {'command': 'server', 'cwd': '/old/project',
                                                   'args': ['/old/project/数据 folder']}}))
    result = invoke(*relocation_args(source, fmt, ['worker', 'cwd'], ['worker', 'args', 0]))
    assert result.returncode == 0, result.stderr
    assert loads(result.stdout, fmt).servers['worker'] == {
        'command': 'server', 'cwd': '/new/project', 'args': ['/new/project/数据 folder']}
    assert [r['relativePath'] for r in json.loads(result.stderr)['relocations']] == ['.', '数据 folder']


@pytest.mark.parametrize('value', [
    'data', './data', '../data', '~/data', '${ROOT}/data', '$env:ROOT/data',
    '/old/project/../outside', '/old/project/./data', '/old/project//data',
    '//old/project/data', '/old/project/data/', '/old/project-other/data', '/outside/data',
    '--data=/old/project/data', 'file:///old/project/data', 'https://example.test/data',
    r'C:\old\project\data', r'/old/project\data', '/old/project/${ENV}/data',
    '/old/project/*.json', '/old/project/%ROOT%', '/old/project/`pwd`',
    '/old/project/a:/old/project/b',
    '/old/project/data\nother',
])
def test_ambiguous_selected_path_refuses_entire_write(tmp_path, value):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {'worker': {'command': 'server',
                                                       'args': ['/old/project/good', value]}}))
    before = source.read_bytes()
    for extra in (['--in-place'], ['--in-place', '--dry-run'], ['-o', tmp_path / 'new']):
        result = invoke(*relocation_args(source, 'codex', ['worker', 'args', 0],
                                        ['worker', 'args', 1]), *extra)
        assert result.returncode == 2
        assert 'relocation' in result.stderr and result.stdout == ''
        assert value not in result.stderr
        assert source.read_bytes() == before
        assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize('path', [
    ['worker', 'command'], ['worker', 'env', 'DATA'], ['remote', 'url'],
    ['worker', 'extension'], ['worker', 'args'], ['worker', 'args', -1],
    ['worker', 'args', True], ['worker', 'args', 9], ['missing', 'args', 0],
    ['worker', 'args', '0'], ['worker', 'args', 0, 'nested'], [], {}, None,
])
def test_non_path_fields_and_bad_selectors_refused(tmp_path, path):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {
        'worker': {'command': '/old/project/server', 'args': ['/old/project/data'],
                   'env': {'DATA': '/old/project/data'}, 'extension': '/old/project/data'},
        'remote': {'url': 'https://example.test/mcp'},
    }))
    before = source.read_bytes()
    result = invoke(*relocation_args(source, 'codex', path), '--in-place')
    assert result.returncode == 2 and 'relocation' in result.stderr
    assert result.stdout == '' and source.read_bytes() == before
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize('fmt', ['claude', 'cline'])
def test_cwd_not_in_native_schema_is_not_guessed(tmp_path, fmt):
    source = tmp_path / 'source'
    source.write_text(new_document(fmt, {'worker': {'command': 'server', 'cwd': '/old/project'}}))
    result = invoke(*relocation_args(source, fmt, ['worker', 'cwd']))
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize('roots', [
    ('relative', '/new/project'), ('/old/project', 'relative'),
    ('/', '/new/project'), ('/old/project', '/'), ('/old/../project', '/new/project'),
    ('/old/project', '/new//project'), ('/old/project', '/new/${ROOT}'),
    ('/old/project', '/old/project'),
])
def test_invalid_roots_are_not_normalized_or_expanded(tmp_path, roots):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {'s': {'command': 'server', 'args': ['/old/project/data']}}))
    result = invoke('relocate', '--format', 'codex', source, '--from-root', roots[0],
                    '--to-root', roots[1], '--path', '["s","args",0]')
    assert result.returncode == 2 and not result.stdout


def test_duplicate_malformed_and_missing_selection_refused(tmp_path):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {'s': {'command': 'server', 'args': ['/old/project/data']}}))
    args = relocation_args(source, 'codex', ['s', 'args', 0])
    for extra in (['--path', '["s","args",0]'], ['--path', 'not-json']):
        result = invoke(*args, *extra)
        assert result.returncode == 2 and not result.stdout
    assert invoke(*relocation_args(source)).returncode == 2


@pytest.mark.parametrize('fmt', FORMATS)
def test_relocation_preserves_comments_and_unrelated_content(tmp_path, fmt):
    from .test_bridge import fixture
    doc = fixture(fmt)
    # Reuse existing native fixtures with a selected literal absolute argument.
    text = doc.render({'github': {'args': ['/old/project/data']}})
    source = tmp_path / 'source'
    source.write_text(text)
    result = invoke(*relocation_args(source, fmt, ['github', 'args', 0]))
    assert result.returncode == 0, result.stderr
    assert result.stdout == text.replace('/old/project/data', '/new/project/data')


def test_in_place_backup_and_existing_output_guards(tmp_path):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {'s': {'command': 'server', 'args': ['/old/project/data']}}))
    before = source.read_bytes()
    args = relocation_args(source, 'codex', ['s', 'args', 0])
    preview = invoke(*args, '--in-place', '--dry-run')
    assert preview.returncode == 0, preview.stderr
    assert list(tmp_path.iterdir()) == [source] and source.read_bytes() == before
    assert invoke(*args, '-o', source).returncode == 2
    result = invoke(*args, '--in-place')
    assert result.returncode == 0, result.stderr
    backup = Path(str(source) + '.bak')
    assert backup.read_bytes() == before
    assert source.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    # A reverse relocation is blocked by the exclusive backup before replacing.
    after = source.read_bytes()
    refused = invoke('relocate', '--format', 'codex', source, '--from-root', '/new/project',
                     '--to-root', '/old/project', '--path', '["s","args",0]', '--in-place')
    assert refused.returncode == 2 and 'backup already exists' in refused.stderr
    assert source.read_bytes() == after and backup.read_bytes() == before


def test_relocation_refuses_credentials_without_provenance_disclosure(tmp_path):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {'s': {'command': 'server', 'args': ['/old/project/data'],
                                                   'env': {'API_TOKEN': 'synthetic-private-value'}}}))
    result = invoke(*relocation_args(source, 'codex', ['s', 'args', 0]), '--in-place')
    assert result.returncode == 2 and 'credential' in result.stderr
    assert result.stdout == '' and '/old/project' not in result.stderr
    assert 'synthetic-private-value' not in result.stderr
    assert list(tmp_path.iterdir()) == [source]


def test_library_does_not_mutate_document_and_does_not_resolve_files():
    from agentwrench.mcpconfig.relocation import relocate
    doc = loads(new_document('codex', {'s': {'command': 'server', 'args': ['/old/project/data']}}), 'codex')
    original = copy.deepcopy(doc.servers)
    text, provenance = relocate(doc, '/old/project', '/new/project', [['s', 'args', 0]])
    assert doc.servers == original
    assert loads(text, 'codex').servers['s']['args'] == ['/new/project/data']
    assert provenance['relocations'][0]['relativePath'] == 'data'


def test_preview_and_write_refuse_symlink_destination_and_missing_parent(tmp_path):
    source = tmp_path / 'source'
    source.write_text(new_document('codex', {'s': {'command': 'server', 'args': ['/old/project/data']}}))
    original = source.read_bytes()
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    args = relocation_args(source, 'codex', ['s', 'args', 0])
    for target in (link / 'new', tmp_path / 'missing' / 'new'):
        for extra in ([], ['--dry-run']):
            result = invoke(*args, '-o', target, *extra)
            assert result.returncode == 2 and not result.stdout
            assert not target.exists() and source.read_bytes() == original
