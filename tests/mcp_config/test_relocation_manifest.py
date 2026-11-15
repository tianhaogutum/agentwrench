"""Reviewed selectors bind to exact source bytes, never to a stale args index."""
import hashlib
import json
from pathlib import Path

import pytest

from agentwrench.mcpconfig.formats import ConfigError, FORMATS, MAX_BYTES, new_document, loads
from .test_bridge import invoke


def explicit(source, fmt='codex'):
    return ['relocate', source, '--format', fmt, '--from-root', '/old/project',
            '--to-root', '/new/project', '--path', '["worker.工具","args",1]']


def setup_files(tmp_path, fmt='codex'):
    source = tmp_path / 'source'
    source.write_text(new_document(fmt, {'worker.工具': {
        'command': '/old/project/server',
        'args': ['--data', '/old/project/data', '/old/project/unselected'],
        'env': {'ROOT': '${ROOT}'}, 'extension': 'keep'}}))
    manifest = {
        'schemaVersion': 1, 'operation': 'relocation-selection', 'scope': 'project',
        'format': fmt, 'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'sourceRoot': '/old/project', 'targetRoot': '/new/project',
        'paths': [['worker.工具', 'args', 1]],
    }
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps(manifest))
    return source, plan, manifest


def snapshot(root):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize('fmt', FORMATS)
def test_generated_manifest_is_reusable_for_identical_bytes_at_another_path(tmp_path, fmt):
    source, plan, expected = setup_files(tmp_path, fmt)
    before = snapshot(tmp_path)
    generated = invoke(*explicit(source, fmt), '--plan')
    assert generated.returncode == 0, generated.stderr
    assert generated.stderr == '' and json.loads(generated.stdout) == expected
    assert snapshot(tmp_path) == before
    plan.write_text(generated.stdout)
    copied = tmp_path / 'copied'
    copied.write_bytes(source.read_bytes())
    before = snapshot(tmp_path)
    preview = invoke('relocate', copied, '--manifest', plan, '--dry-run')
    assert preview.returncode == 0, preview.stderr
    direct_preview = invoke(*explicit(source, fmt), '--dry-run')
    assert preview.stdout == direct_preview.stdout and preview.stderr == ''
    output = tmp_path / 'output'
    result = invoke('relocate', copied, '--manifest', plan, '-o', output)
    assert result.returncode == 0 and result.stdout == '', result.stderr
    direct = invoke(*explicit(source, fmt))
    assert output.read_text() == direct.stdout and result.stderr == direct.stderr
    actual = loads(output.read_text(), fmt).servers['worker.工具']
    assert actual['args'] == ['--data', '/new/project/data', '/old/project/unselected']
    assert actual['command'] == '/old/project/server' and actual['env'] == {'ROOT': '${ROOT}'}
    assert output.stat().st_mode & 0o777 == 0o600
    assert {k: v for k, v in snapshot(tmp_path).items() if k != 'output'} == before


@pytest.mark.parametrize('change', ['reordered', 'comment', 'crlf'])
def test_changed_source_refuses_stale_indexes_before_any_output(tmp_path, change):
    source, plan, _ = setup_files(tmp_path)
    original = source.read_bytes()
    if change == 'reordered':
        # The old index remains a valid in-root path: plain relocation would
        # silently move the wrong argument, rather than report an invalid index.
        source.write_bytes(original.replace(b'"--data", ', b'"--data", "/old/project/inserted", '))
        direct = invoke(*explicit(source), '--dry-run')
        assert direct.returncode == 0, direct.stderr
        assert json.loads(direct.stdout)['relocations'][0]['before'] == '/old/project/inserted'
    elif change == 'comment':
        source.write_bytes(b'# changed comment only\n' + original)
    else:
        source.write_bytes(original.replace(b'\n', b'\r\n'))
    before = snapshot(tmp_path)
    for extra in ([], ['--dry-run'], ['--in-place'], ['--in-place', '--dry-run'],
                  ['-o', tmp_path / 'output']):
        result = invoke('relocate', source, '--manifest', plan, *extra)
        assert result.returncode == 2 and result.stdout == ''
        assert 'source digest mismatch' in result.stderr
        assert '/old/project' not in result.stderr
        assert snapshot(tmp_path) == before


def test_digest_uses_exact_crlf_bytes_and_in_place_retains_original_backup(tmp_path):
    source, plan, _ = setup_files(tmp_path)
    source.write_bytes(source.read_bytes().replace(b'\n', b'\r\n'))
    original = source.read_bytes()
    generated = invoke(*explicit(source), '--plan')
    assert generated.returncode == 0, generated.stderr
    assert json.loads(generated.stdout)['sourceSha256'] == hashlib.sha256(original).hexdigest()
    plan.write_text(generated.stdout)
    applied = invoke('relocate', source, '--manifest', plan, '--in-place')
    assert applied.returncode == 0, applied.stderr
    backup = Path(str(source) + '.bak')
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == source.stat().st_mode & 0o777 == 0o600
    assert loads(source.read_text(), 'codex').servers['worker.工具']['args'][1] == '/new/project/data'
    before = snapshot(tmp_path)
    repeated = invoke('relocate', source, '--manifest', plan, '--in-place')
    assert repeated.returncode == 2 and 'source digest mismatch' in repeated.stderr
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(('key', 'value'), [
    ('schemaVersion', 2), ('schemaVersion', True), ('schemaVersion', 1.0),
    ('operation', 'relocate'), ('scope', 'global'), ('format', 'unknown'),
    ('format', []), ('sourceSha256', 'A' * 64), ('sourceSha256', '0' * 63),
    ('sourceSha256', None), ('sourceRoot', '/'), ('sourceRoot', '/old/../project'),
    ('targetRoot', '/old/project'), ('targetRoot', '${ROOT}'),
    ('paths', []), ('paths', None), ('paths', [['worker.工具', 'args', True]]),
    ('paths', [['worker.工具', 'args', 1], ['worker.工具', 'args', 1]]),
    ('paths', [['worker.工具', 'command']]), ('paths', [['worker.工具', 'args', 50]]),
    ('paths', [['absent', 'args', 0]]), ('unexpected', 'hostile-value'),
])
def test_bad_manifest_refuses_entire_operation_without_disclosure(tmp_path, key, value):
    source, plan, data = setup_files(tmp_path)
    data[key] = value
    plan.write_text(json.dumps(data))
    before = snapshot(tmp_path)
    for extra in (['--dry-run'], ['--in-place']):
        result = invoke('relocate', source, '--manifest', plan, *extra)
        assert result.returncode == 2 and result.stdout == ''
        assert '/old/project' not in result.stderr and 'hostile-value' not in result.stderr
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize('key', ['schemaVersion', 'operation', 'scope', 'format',
                                'sourceSha256', 'sourceRoot', 'targetRoot', 'paths'])
def test_missing_manifest_key_is_not_defaulted(tmp_path, key):
    source, plan, data = setup_files(tmp_path)
    del data[key]
    plan.write_text(json.dumps(data))
    before = snapshot(tmp_path)
    result = invoke('relocate', source, '--manifest', plan, '--in-place')
    assert result.returncode == 2 and not result.stdout
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('bad', ['[]', 'null', '{', '{"x":1,"x":2}',
                                '{"x":NaN}', '/*comment*/ {}',
                                '[' * 66 + '0' + ']' * 66, ' ' * (1024 * 1024 + 1)],
                         ids=['array', 'null', 'malformed', 'duplicate', 'nonfinite',
                              'comment', 'depth', 'size'])
def test_manifest_parser_is_strict_and_bounded(tmp_path, bad):
    source, plan, _ = setup_files(tmp_path)
    plan.write_text(bad)
    before = snapshot(tmp_path)
    result = invoke('relocate', source, '--manifest', plan, '--in-place')
    assert result.returncode == 2 and result.stdout == ''
    assert 'Traceback' not in result.stderr and snapshot(tmp_path) == before


@pytest.mark.parametrize('override', [
    ['--format', 'codex'], ['--from-root', '/old/project'],
    ['--to-root', '/new/project'], ['--path', '["worker.工具","args",1]'], ['--plan'],
])
def test_manifest_overrides_are_refused_even_if_equal(tmp_path, override):
    source, plan, _ = setup_files(tmp_path)
    before = snapshot(tmp_path)
    result = invoke('relocate', source, '--manifest', plan, '--in-place', *override)
    assert result.returncode == 2 and not result.stdout
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('extra', [['--dry-run'], ['--in-place'], ['-o', 'unused']])
def test_plan_is_read_only_and_rejects_output_modes(tmp_path, extra):
    source, _, _ = setup_files(tmp_path)
    before = snapshot(tmp_path)
    result = invoke(*explicit(source), '--plan', *extra)
    assert result.returncode == 2 and not result.stdout
    assert snapshot(tmp_path) == before


def test_matching_digest_does_not_bypass_credentials_or_destination_guards(tmp_path):
    source, plan, data = setup_files(tmp_path)
    output = tmp_path / 'existing'
    output.write_text('keep')
    backup = Path(str(source) + '.bak')
    backup.write_text('keep backup')
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    for extra in (['-o', output], ['--in-place'], ['-o', link / 'new'],
                  ['-o', tmp_path / 'missing' / 'new']):
        before = snapshot(tmp_path)
        result = invoke('relocate', source, '--manifest', plan, *extra, '--dry-run')
        assert result.returncode == 2 and result.stdout == ''
        assert snapshot(tmp_path) == before
    source.write_text(new_document('codex', {'worker.工具': {
        'command': 'server', 'args': ['--data', '/old/project/data'],
        'env': {'API_TOKEN': 'synthetic-private'}}}))
    data['sourceSha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    plan.write_text(json.dumps(data))
    before = snapshot(tmp_path)
    for args in ([*explicit(source), '--plan'],
                 ['relocate', source, '--manifest', plan, '--dry-run']):
        refused = invoke(*args)
        assert refused.returncode == 2 and refused.stdout == ''
        assert 'credential' in refused.stderr and 'synthetic-private' not in refused.stderr
        assert snapshot(tmp_path) == before


def test_symlink_missing_and_invalid_utf8_manifest_refused(tmp_path):
    source, plan, _ = setup_files(tmp_path)
    link = tmp_path / 'link'
    link.symlink_to(plan)
    invalid = tmp_path / 'invalid'
    invalid.write_bytes(b'\xff')
    for candidate in (link, invalid, tmp_path / 'missing'):
        before = snapshot(tmp_path)
        result = invoke('relocate', source, '--manifest', candidate, '--in-place')
        assert result.returncode == 2 and not result.stdout
        assert snapshot(tmp_path) == before


def test_plan_generation_refuses_a_manifest_too_large_to_reuse():
    from agentwrench.mcpconfig.relocation import create_manifest
    # A bounded config can still yield a much larger manifest when a long
    # literal server name is repeated in thousands of distinct selectors.
    name = 'w' * 256
    text = new_document('codex', {name: {'command': 'server',
                                      'args': ['/old/project/data'] * 4000}})
    assert len(text.encode('utf-8')) < MAX_BYTES
    doc = loads(text, 'codex')
    with pytest.raises(ConfigError, match='manifest exceeds 1 MiB'):
        create_manifest(doc, '/old/project', '/new/project',
                        [[name, 'args', index] for index in range(4000)])
