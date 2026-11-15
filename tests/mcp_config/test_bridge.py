import json
from pathlib import Path
import subprocess
import sys

import pytest

from agentwrench.mcpconfig.cli import atomic_write, main
from agentwrench.mcpconfig.core import diff_paths, project, redacted, semantic, to_ir
from agentwrench.mcpconfig.formats import ConfigError, FORMATS, loads, new_document

FIX = Path(__file__).parent / 'fixtures'
FILES = {'claude': 'claude/.mcp.json', 'codex': 'codex/config.toml',
         'cline': 'cline/cline_mcp_settings.json', 'continue': 'continue/config.yaml',
         'vscode': 'vscode/mcp.json'}


def fixture(fmt):
    return loads((FIX / FILES[fmt]).read_text(), fmt)


def invoke(*args):
    return subprocess.run([sys.executable, '-m', 'agentwrench', 'mcp-config', *map(str, args)],
                          text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize('fmt', FORMATS)
def test_golden_native_roundtrip(fmt):
    doc = fixture(fmt)
    ir = to_ir(doc)
    out, report = project(ir, fmt)
    rebuilt = loads(new_document(fmt, out), fmt)
    assert rebuilt.servers == doc.servers
    assert not [f for f in report if f.kind in ('loss', 'error')]
    assert doc.render(out) == doc.text
    assert ir[0].raw == doc.servers[ir[0].name]


@pytest.mark.parametrize('source', FORMATS)
@pytest.mark.parametrize('target', FORMATS)
def test_all_cross_format_roundtrips(source, target):
    # Real fixture shapes reduced to transport-common representable fields.
    raw = {'command': 'npx', 'args': ['-y', 'fixture-server'], 'env': {'REGION': 'eu'}}
    initial = loads(new_document(source, {'quoted.name / unicode-工具': raw}), source)
    projected, findings = project(to_ir(initial), target)
    assert not [f for f in findings if f.kind in ('loss', 'error')]
    emitted = loads(new_document(target, projected), target)
    back, _ = project(to_ir(emitted), source)
    final = loads(new_document(source, back), source)
    assert semantic(to_ir(initial)[0]) == semantic(to_ir(final)[0])
    assert list(final.servers) == list(initial.servers)


@pytest.mark.parametrize('source', FORMATS)
@pytest.mark.parametrize('target', FORMATS)
def test_full_fixtures_loss_accounting(source, target):
    original = to_ir(fixture(source))
    output, findings = project(original, target)
    assert len(output) + len({f.server for f in findings if f.kind == 'error'}) == len(original)
    converted = to_ir(loads(new_document(target, output), target))
    back, _ = project(converted, source)
    restored = {s.name: s for s in to_ir(loads(new_document(source, back), source))}
    for before in original:
        if before.name not in restored:
            assert any(f.server == before.name and f.kind == 'error' for f in findings)
            continue
        after = restored[before.name]
        # Every common canonical field survives. Unmapped native keys are reported.
        for key in before.fields.keys() & after.fields.keys():
            assert before.fields[key] == after.fields[key]
        if source != target:
            for key in before.passthrough[source]:
                assert any(f.server == before.name and (f.field == key or f.field.startswith(key + '.'))
                           and f.kind == 'loss' for f in findings)


@pytest.mark.parametrize('fmt', FORMATS)
def test_same_format_cli_is_byte_identical(fmt):
    result = invoke('convert', '--from', fmt, '--to', fmt, FIX / FILES[fmt])
    assert result.returncode == 0, result.stderr
    assert result.stdout == fixture(fmt).text


def test_disabled_mapping_and_timeout_report():
    out, report = project(to_ir(fixture('cline')), 'codex')
    assert out['github']['enabled'] is False
    assert 'autoApprove' not in out['github'] and 'timeout' not in out['github']
    assert {f.field for f in report if f.kind == 'loss'} >= {'autoApprove', 'timeout', 'metadata.owner', 'oauth.clientId'}
    back, _ = project(to_ir(loads(new_document('codex', out), 'codex')), 'cline')
    assert back['github']['disabled'] is True


@pytest.mark.parametrize('fmt,transport', [('claude', 'http'), ('cline', 'streamableHttp'),
                                         ('continue', 'streamable-http'), ('vscode', 'http')])
def test_remote_header_mapping(fmt, transport):
    initial = loads(new_document(fmt, {'remote': {'type': transport, 'url': 'https://example.test/mcp',
                       **({'requestOptions': {'headers': {'X-Region': 'eu'}}} if fmt == 'continue'
                          else {'headers': {'X-Region': 'eu'}})}}), fmt)
    out, findings = project(to_ir(initial), 'codex')
    assert out['remote']['http_headers'] == {'X-Region': 'eu'}
    assert not [f for f in findings if f.kind in ('error', 'loss')]


@pytest.mark.parametrize('fmt', ['cline', 'continue'])
def test_omitted_remote_transport_is_sse(fmt):
    out, findings = project(to_ir(loads(new_document(fmt, {'legacy': {'url': 'https://example.test/sse'}}), fmt)), 'codex')
    assert not out
    assert any(f.kind == 'error' for f in findings)


def test_partial_conversion_exit3(tmp_path):
    path = tmp_path / 'source.json'
    path.write_text(json.dumps({'mcpServers': {'good': {'command': 'echo'},
                              'old': {'type': 'sse', 'url': 'https://example.test/events'}}}))
    result = invoke('convert', '--from', 'claude', '--to', 'codex', path)
    assert result.returncode == 3
    assert set(loads(result.stdout, 'codex').servers) == {'good'}
    assert 'unsupported' in result.stderr


@pytest.mark.parametrize('fmt', FORMATS)
def test_merge_preserves_unrelated_and_comments(fmt):
    doc = fixture(fmt)
    out = doc.render({'github': {'args': ['new', '--flag']}, 'added': {'command': 'fixture'}})
    parsed = loads(out, fmt)
    assert parsed.servers['github']['args'] == ['new', '--flag']
    assert parsed.servers['added']['command'] == 'fixture'
    assert parsed.servers['remote.tools'] == doc.servers['remote.tools']
    # The complete untouched trailing server and document-level contents survive byte-for-byte.
    if fmt == 'codex':
        untouched = doc.text[doc.text.index('[mcp_servers."remote.tools"]'):]
        assert untouched in out
        assert out.startswith('# Synthetic Codex config\nmodel = "example-model" # keep me\n')
    if fmt == 'continue':
        tail = doc.text[doc.text.index('  - name: remote.tools'):doc.text.index('rules:')]
        assert tail in out
        assert out.endswith('rules: [] # keep trailing content\n')
        assert '# retain comment' in out and 'models: [] # unrelated' in out
    if fmt == 'vscode':
        assert '// Synthetic VS Code' in out
        assert '"inputs": [{"id": "github-token", "type": "promptString", "password": true}]' in out
        assert '"dev": {"watch": "src/**/*.js"}' in out


def test_cli_merge_backup_collision_and_noop(tmp_path):
    src = FIX / FILES['claude']
    dest = tmp_path / 'config.toml'
    original = '[mcp_servers.github]\ncommand = "other" # retain\n\n[other]\nx = "unrelated"\n'
    dest.write_text(original)
    args = ['convert', '--from', 'claude', '--to', 'codex', src, '-o', dest, '--merge']
    assert invoke(*args).returncode == 2
    assert dest.read_text() == original and not dest.with_suffix('.toml.bak').exists()
    result = invoke(*args, '--overwrite')
    assert result.returncode == 0, result.stderr
    assert dest.with_suffix('.toml.bak').read_text() == original
    assert '[other]\nx = "unrelated"\n' in dest.read_text()
    assert 'command = "npx" # retain' in dest.read_text()
    # An identical second merge is a no-op; does not require another backup.
    assert invoke(*args, '--overwrite').returncode == 0
    assert invoke('check', '--from', 'claude', '--to', 'codex', '--source', src, '--target', dest).returncode == 0


def test_backup_never_clobbered(tmp_path):
    dest = tmp_path / 'out'
    dest.write_text('old')
    Path(str(dest) + '.bak').write_text('precious backup')
    with pytest.raises(ConfigError, match='backup already exists'):
        atomic_write(dest, 'new', b'old')
    assert dest.read_text() == 'old'
    assert Path(str(dest) + '.bak').read_text() == 'precious backup'


def test_atomic_stale_write_and_symlink(tmp_path):
    dest = tmp_path / 'out'
    dest.write_text('other writer')
    with pytest.raises(ConfigError, match='concurrently'):
        atomic_write(dest, 'new', b'old')
    link = tmp_path / 'link'
    link.symlink_to(dest)
    with pytest.raises(ConfigError, match='symlinks'):
        atomic_write(link, 'new', None)
    assert dest.read_text() == 'other writer'
    assert not list(tmp_path.glob('.*out.*'))


def test_file_permissions(tmp_path):
    target = tmp_path / 'new.toml'
    result = invoke('convert', '--from', 'claude', '--to', 'codex', FIX / FILES['claude'], '-o', target)
    assert result.returncode == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.new.toml.*'))


def test_dryrun_writes_nothing(tmp_path):
    out = tmp_path / 'config.toml'
    result = invoke('convert', '--from', 'claude', '--to', 'codex', FIX / FILES['claude'], '-o', out, '--dry-run')
    assert result.returncode == 0 and 'DRY RUN' in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_drift_exit_codes(tmp_path):
    base = ['check', '--source', FIX / FILES['claude'], '--from', 'claude', '--to', 'codex', '--target']
    drift = invoke(*base, FIX / 'codex/drifted.toml')
    assert drift.returncode == 1 and 'github.args' in drift.stdout
    assert 'WRONG' not in drift.stdout
    assert invoke(*base, tmp_path / 'absent').returncode == 2
    target = tmp_path / 'ok.toml'
    invoke('convert', '--from', 'claude', '--to', 'codex', FIX / FILES['claude'], '-o', target)
    assert invoke(*base, target).returncode == 0
    target.write_text(target.read_text() + '\n[mcp_servers.unrelated]\ncommand = "another"\n')
    assert invoke(*base, target).returncode == 0


def test_unrepresentable_policy_cannot_claim_in_sync(tmp_path):
    target = tmp_path / 'out.toml'
    invoke('convert', '--from', 'cline', '--to', 'codex', FIX / FILES['cline'], '-o', target)
    result = invoke('check', '--source', FIX / FILES['cline'], '--from', 'cline', '--to', 'codex', '--target', target)
    assert result.returncode == 1 and 'unrepresentable' in result.stdout


@pytest.mark.parametrize('secret', ['ghp_' + 'a'*30, 'Bearer literal-example', 'synthetic-password'])
def test_no_secret_in_any_diagnostic_or_output(tmp_path, secret):
    src = tmp_path / 'source.json'
    src.write_text(json.dumps({'mcpServers': {'test': {'command': 'echo', 'env': {'API_TOKEN': secret}}}}))
    target = tmp_path / 'out.toml'
    report = invoke('report', '--from', 'claude', '--to', 'codex', src, '--json')
    assert report.returncode == 0 and 'secret' in report.stdout
    assert secret not in report.stdout + report.stderr
    assert json.loads(report.stdout)['findings']
    refused = invoke('convert', '--from', 'claude', '--to', 'codex', src, '-o', target)
    assert refused.returncode == 2 and not target.exists()
    assert secret not in refused.stdout + refused.stderr
    result = invoke('convert', '--from', 'claude', '--to', 'codex', src, '--redact-secrets', '-o', target)
    assert result.returncode == 0, result.stderr
    assert secret not in target.read_text() + result.stdout + result.stderr
    assert '[REDACTED]' in target.read_text()


def test_secret_drift_masks_both_values(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.write_text('{"mcpServers":{"s":{"command":"echo","env":{"TOKEN":"sensitive-a"}}}}')
    b.write_text('[mcp_servers.s]\ncommand="echo"\nenv={TOKEN="sensitive-b"}\n')
    result = invoke('check', '--from', 'claude', '--to', 'codex', '--source', a, '--target', b)
    assert result.returncode == 1 and 'env.TOKEN' in result.stdout
    assert 'sensitive-' not in result.stdout + result.stderr


@pytest.mark.parametrize('fmt,text', [
    ('claude', '{"mcpServers": {\n "s":'), ('codex', '[mcp_servers.s\n'),
    ('continue', 'mcpServers: [\n bad: ['), ('vscode', '{/*unterminated'),
    ('claude', '{"a":1,"a":2}'), ('codex', 'a=1\na=2'), ('continue', 'a: 1\na: 2'),
    ('continue', 'mcpServers: !!python/object:os.system {}'),
    ('continue', 'a: &x [1]\nb: *x'), ('continue', 'a: 2020-01-01'),
    ('claude', '{"a":NaN}'), ('claude', '{"a":Infinity}'),
])
def test_invalid_and_unsafe_formats(fmt, text):
    with pytest.raises(ConfigError):
        loads(text, fmt)


@pytest.mark.parametrize('fmt,text', [('claude','{"mcpServers": {'), ('codex','[broken'), ('continue','x: [')])
def test_parser_errors_have_positions_and_no_source_echo(tmp_path, fmt, text):
    src = tmp_path / 'bad'
    src.write_text(text + ' ghp_verySensitiveSecret')
    result = invoke('convert', '--from', fmt, '--to', 'codex', src)
    assert result.returncode == 2
    assert 'line' in result.stderr and 'column' in result.stderr
    assert 'verySensitiveSecret' not in result.stderr + result.stdout


@pytest.mark.parametrize('server', [
    {'command': 'x', 'url': 'https://example.test'}, {'args': []}, {'command': 'x', 'args': 'bad'},
    {'command': 'x', 'env': {'PORT': 123}}, {'command': 'x', 'enabled': 'false'},
    {'command': 'x', 'startup_timeout_sec': -1}, {'type': 'http', 'command': 'x'},
    {'url': 'https://example.test', 'env': {'A': 'B'}},
])
def test_invalid_server_shapes(server):
    with pytest.raises(ConfigError):
        to_ir(loads(new_document('codex' if 'type' not in server else 'claude', {'s': server}),
                    'codex' if 'type' not in server else 'claude'))


def test_input_depth_size_and_symlink(tmp_path):
    with pytest.raises(ConfigError, match='nesting'):
        loads('[' * 70 + '0' + ']' * 70, 'claude')
    with pytest.raises(ConfigError, match='1 MiB'):
        loads(' ' * (1024 * 1024 + 1), 'claude')
    link = tmp_path / 'link'
    link.symlink_to(FIX / FILES['claude'])
    assert invoke('report', '--from', 'claude', '--to', 'codex', link).returncode == 2


def test_vscode_numeric_env_is_explicit_error():
    doc = loads(new_document('vscode', {'s': {'command': 'x', 'env': {'PORT': 12, 'UNSET': None}}}), 'vscode')
    out, findings = project(to_ir(doc), 'claude')
    assert not out and any(f.kind == 'error' for f in findings)
    assert project(to_ir(doc), 'vscode')[0] == doc.servers


def test_interpolation_is_never_evaluated(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'must-not-be-read')
    out, findings = project(to_ir(fixture('claude')), 'codex')
    assert out['github']['env']['GITHUB_TOKEN'] == '${GITHUB_TOKEN}'
    assert any(f.kind == 'interpolation' for f in findings)
    assert 'must-not-be-read' not in repr(out) + repr(findings)


def test_document_fields_reported(tmp_path):
    result = invoke('report', '--from', 'vscode', '--to', 'claude', FIX / FILES['vscode'], '--json')
    findings = json.loads(result.stdout)['findings']
    assert any(f['field'] == 'inputs' and f['kind'] == 'loss' for f in findings)


def test_no_server_execution(tmp_path):
    marker = tmp_path / 'must-not-exist'
    src = tmp_path / 'input.json'
    src.write_text(json.dumps({'mcpServers': {'s': {'command': 'sh', 'args': ['-c', 'touch ' + str(marker)]}}}))
    result = invoke('convert', '--from', 'claude', '--to', 'codex', src, '--dry-run')
    assert result.returncode == 0 and not marker.exists()


def test_yaml_flow_merge_fails_explicitly():
    doc = loads('mcpServers: [{name: a, command: x}]\n', 'continue')
    with pytest.raises(ConfigError, match='block-style'):
        doc.render({'a': {'args': ['new']}})


def test_bearer_reference_not_falsely_rejected(tmp_path):
    src = tmp_path / 'ref.json'
    src.write_text('{"mcpServers":{"s":{"type":"http","url":"https://example.test/mcp",'
                   '"headers":{"Authorization":"Bearer ${TEAM_MCP_TOKEN}"}}}}')
    result = invoke('convert', '--from', 'claude', '--to', 'codex', src)
    assert result.returncode == 0, result.stderr
    assert 'Bearer ${TEAM_MCP_TOKEN}' in result.stdout


def test_secret_default_reference_detected(tmp_path):
    src = tmp_path / 'ref.json'
    src.write_text('{"mcpServers":{"s":{"command":"x","env":{"TOKEN":"${TOKEN:-literal-fallback}"}}}}')
    result = invoke('report', '--from', 'claude', '--to', 'codex', src, '--json')
    assert any(f['kind'] == 'secret' for f in json.loads(result.stdout)['findings'])
    assert 'literal-fallback' not in result.stdout + result.stderr
    assert invoke('convert', '--from', 'claude', '--to', 'codex', src).returncode == 2


def test_extra_env_values_are_drift(tmp_path):
    source = tmp_path / 'source.json'
    target = tmp_path / 'target.toml'
    source.write_text('{"mcpServers":{"s":{"command":"x"}}}')
    target.write_text('[mcp_servers.s]\ncommand="x"\nenv={EXTRA="changes behavior"}\n')
    result = invoke('check', '--from', 'claude', '--to', 'codex', '--source', source, '--target', target)
    assert result.returncode == 1 and 'env.EXTRA' in result.stdout


@pytest.mark.parametrize('text', ['name: test\nmcpServers: [] # keep\nrules: []\n',
                                  '# begin\nname: test\nrules: []\n'])
def test_yaml_empty_or_absent_collection_merge(text):
    doc = loads(text, 'continue')
    result = doc.render({'a': {'command': 'x'}})
    assert loads(result, 'continue').servers == {'a': {'command': 'x'}}
    assert 'rules: []' in result and 'name: test' in result
    if '# keep' in text:
        assert '# keep' in result


def test_jsonc_surgical_merge_keeps_unrelated_bytes():
    text = '{\n "servers": {"a": {"command": "old"}, /* border */ "b": {"command":"b",},},\n "inputs" : [ /*keep*/ ],\n "x":  7\n}\n'
    doc = loads(text, 'vscode')
    result = doc.render({'a': {'args': ['new'], 'command': 'new'}, 'c': {'command': 'c'}})
    assert '/* border */ "b": {"command":"b",}' in result
    assert result.endswith('\n "inputs" : [ /*keep*/ ],\n "x":  7\n}\n')
    assert loads(result, 'vscode').servers['a'] == {'command': 'new', 'args': ['new']}


def test_yaml_comment_between_servers_not_duplicated():
    text = ('mcpServers:\n  - name: a\n    command: x\n  # border\n'
            '  - name: b\n    command: y # untouched\n# root border\nrules: []\n')
    result = loads(text, 'continue').render({'a': {'command': 'new'}})
    assert result.count('# border') == 1
    assert result.count('# root border') == 1
    assert '  - name: b\n    command: y # untouched\n' in result
    assert loads(result, 'continue').servers['a']['command'] == 'new'


def test_secret_signature_in_comment_refused_or_stripped(tmp_path):
    secret = 'ghp_' + 'a' * 30
    src = tmp_path / 'source.json'
    src.write_text('{ // ' + secret + '\n"servers":{"s":{"type":"stdio","command":"x"}}}')
    denied = invoke('convert', '--from', 'vscode', '--to', 'vscode', src)
    assert denied.returncode == 2 and secret not in denied.stdout + denied.stderr
    allowed = invoke('convert', '--from', 'vscode', '--to', 'vscode', src, '--redact-secrets')
    assert allowed.returncode == 0 and secret not in allowed.stdout + allowed.stderr
    assert loads(allowed.stdout, 'vscode').servers['s']['command'] == 'x'


def test_all_unsupported_transport_makes_no_output(tmp_path):
    src, dest = tmp_path / 'source', tmp_path / 'output'
    src.write_text('{"mcpServers":{"old":{"type":"sse","url":"https://example.test/sse"}}}')
    result = invoke('convert', '--from', 'claude', '--to', 'codex', src, '-o', dest)
    assert result.returncode == 3 and not dest.exists()


def test_unknown_fields_and_input_document_not_mutated():
    doc = fixture('continue')
    before = json.dumps(doc.servers, sort_keys=True)
    servers = to_ir(doc)
    out, _ = project(servers, 'codex')
    out['github']['env']['GITHUB_TOKEN'] = 'changed'
    assert json.dumps(doc.servers, sort_keys=True) == before
    assert servers[1].passthrough['continue']['requestOptions'] == {'verifySsl': True}


def test_empty_unknown_object_is_reported():
    doc = loads('{"mcpServers":{"s":{"command":"x","metadata":{}}}}', 'claude')
    _, findings = project(to_ir(doc), 'codex')
    assert any(f.field == 'metadata' and f.kind == 'loss' for f in findings)


def test_cli_invalid_merge_flags(tmp_path):
    args = ['convert', '--from', 'claude', '--to', 'codex', FIX / FILES['claude']]
    assert invoke(*args, '--overwrite').returncode == 2
    assert invoke(*args, '--merge').returncode == 2
    out = tmp_path / 'exists'
    out.write_text('unchanged')
    assert invoke(*args, '-o', out).returncode == 2
    assert out.read_text() == 'unchanged'


@pytest.mark.parametrize('source', ['codex', 'cline'])
@pytest.mark.parametrize('target', ['claude', 'continue', 'vscode'])
def test_disabled_server_never_silently_enabled(source, target):
    raw = {'command': 'x', **({'enabled': False} if source == 'codex' else {'disabled': True})}
    out, findings = project(to_ir(loads(new_document(source, {'off': raw}), source)), target)
    assert out == {}
    assert any(f.kind == 'error' and 'accidental enablement' in f.message for f in findings)


def test_unix_endpoint_not_mislabeled_http():
    doc = loads('{"servers":{"s":{"type":"http","url":"unix:///tmp/example.sock"}}}', 'vscode')
    out, findings = project(to_ir(doc), 'codex')
    assert not out and any(f.kind == 'error' and f.field == 'url' for f in findings)
    assert project(to_ir(doc), 'vscode')[0] == doc.servers


def test_nested_empty_passthrough_reported():
    doc = loads('{"mcpServers":{"s":{"command":"x","custom":{"empty":[]}}}}', 'claude')
    _, findings = project(to_ir(doc), 'codex')
    assert any(f.kind == 'loss' and f.field == 'custom.empty' for f in findings)


def test_document_secret_report_no_values(tmp_path):
    src = tmp_path / 'source'
    src.write_text('[mcp_servers.s]\ncommand="x"\n[unrelated]\napi_key="sensitive-value"\n')
    result = invoke('report', '--from', 'codex', '--to', 'codex', src, '--json')
    assert result.returncode == 0
    assert 'sensitive-value' not in result.stdout + result.stderr
    assert any(f['kind'] == 'secret' and f['server'] == '(document)' for f in json.loads(result.stdout)['findings'])


@pytest.mark.parametrize('fmt,unknown', [
    ('claude', {'autoApprove': {'custom': 7}, 'timeout': 'client extension'}),
    ('codex', {'type': {'opaque': []}, 'disabled': 'metadata'}),
    ('continue', {'headers': ['unknown native extension'], 'disabled': 'opaque'}),
    ('vscode', {'timeout': {'opaque': None}}),
])
def test_unknown_keys_not_validated_as_other_clients_schema(fmt, unknown):
    raw = {'command': 'x', **unknown}
    doc = loads(new_document(fmt, {'s': raw}), fmt)
    out, _ = project(to_ir(doc), fmt)
    assert out['s'] == raw
    assert to_ir(doc)[0].passthrough[fmt] == unknown


def test_yaml_literal_comment_lines_and_final_root_comment():
    text = ('mcpServers:\n  - name: a\n    command: x\n    metadata: |\n'
            '      # literal content\n      # literal end\n# final root comment\nrules: []\n')
    doc = loads(text, 'continue')
    result = doc.render({'a': {'args': ['new']}})
    parsed = loads(result, 'continue')
    assert parsed.servers['a']['metadata'] == doc.servers['a']['metadata']
    assert result.count('# literal end') == 1
    assert result.count('# final root comment') == 1
    assert result.endswith('# final root comment\nrules: []\n')


@pytest.mark.parametrize('text', ['name: test\nrules: []\n\n\n', 'name: test\nrules: []'])
def test_yaml_absent_collection_preserves_original_prefix(text):
    result = loads(text, 'continue').render({'a': {'command': 'x'}})
    assert result.startswith(text)
    assert loads(result, 'continue').servers['a']['command'] == 'x'


def test_yaml_append_at_eof_without_newline():
    text = 'mcpServers:\n  - name: a\n    command: x'
    result = loads(text, 'continue').render({'b': {'command': 'y'}})
    assert result.startswith(text)
    assert set(loads(result, 'continue').servers) == {'a', 'b'}
