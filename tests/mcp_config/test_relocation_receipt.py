"""Exact-byte relocation receipts and refusal without applied/partial claims."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agentwrench.mcpconfig import cli
from agentwrench.mcpconfig.formats import FORMATS
from .test_bridge import invoke
from .test_relocation_manifest import explicit, setup_files, snapshot


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def expected_receipt(source, plan, output, provenance, status='applied'):
    return {'schemaVersion': 1, 'operation': 'relocation-application', 'status': status,
            'manifestSha256': digest(plan.read_bytes()), 'sourceSha256': digest(source),
            'outputSha256': digest(output), 'provenance': provenance, 'error': None}


def error_receipt(text):
    record = json.loads(text)
    assert record == {'schemaVersion': 1, 'operation': 'relocation-application',
                      'status': 'error', 'manifestSha256': None, 'sourceSha256': None,
                      'outputSha256': None, 'provenance': None, 'error': record['error']}
    assert isinstance(record['error'], str) and record['error']
    for value in ('/old/project', '/new/project', 'hostile-value', 'synthetic-private', 'worker.工具'):
        assert value not in text
    return record


@pytest.mark.parametrize('fmt', FORMATS)
@pytest.mark.parametrize('mode', ['stdout', 'new', 'in-place'])
def test_receipt_binds_exact_artifact_source_and_output(tmp_path, fmt, mode):
    source, plan, _ = setup_files(tmp_path, fmt)
    original = source.read_bytes()
    # Exercise non-ASCII, CRLF and the lack of a terminal newline.
    if fmt != 'continue':
        source.write_bytes(original.replace(b'\n', b'\r\n').rstrip(b'\r\n'))
        original = source.read_bytes()
        data = json.loads(plan.read_text())
        data['sourceSha256'] = digest(original)
        plan.write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode() + b'\r\n')
    direct = subprocess.run([sys.executable, '-m', 'agentwrench', 'mcp-config', *map(str, explicit(source, fmt))],
                            capture_output=True, timeout=10)
    assert direct.returncode == 0, direct.stderr
    output = tmp_path / 'output'
    extra = {'stdout': [], 'new': ['-o', output], 'in-place': ['--in-place']}[mode]
    result = subprocess.run([sys.executable, '-m', 'agentwrench', 'mcp-config', 'relocate', str(source),
                             '--manifest', str(plan), '--receipt', *map(str, extra)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr) == expected_receipt(
        original, plan, direct.stdout, json.loads(direct.stderr))
    if mode == 'stdout':
        assert result.stdout == direct.stdout and source.read_bytes() == original
    else:
        assert result.stdout == b''
        written = source if mode == 'in-place' else output
        assert written.read_bytes() == direct.stdout
        assert written.stat().st_mode & 0o777 == 0o600
    if mode == 'in-place':
        backup = Path(str(source) + '.bak')
        assert backup.read_bytes() == original and backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('mode', ['stdout', 'new', 'in-place'])
def test_preview_is_planned_not_applied_and_never_calls_write(tmp_path, monkeypatch, capsys, mode):
    source, plan, _ = setup_files(tmp_path)
    before = snapshot(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run attempted a write')
    monkeypatch.setattr(cli, 'atomic_write', forbidden)
    monkeypatch.setattr(cli.tempfile, 'NamedTemporaryFile', forbidden)
    extra = {'stdout': [], 'new': ['-o', str(tmp_path / 'output')], 'in-place': ['--in-place']}[mode]
    direct = invoke(*explicit(source))
    assert cli.main(['relocate', str(source), '--manifest', str(plan), '--receipt',
                     '--dry-run', *extra]) == 0
    out = capsys.readouterr()
    assert out.err == ''
    assert json.loads(out.out) == expected_receipt(source.read_bytes(), plan,
                                                   direct.stdout.encode(), json.loads(direct.stderr), 'planned')
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('variant', ['whitespace', 'key-order', 'crlf', 'unicode'])
def test_manifest_identity_is_exact_bytes_not_normalized_json(tmp_path, variant):
    source, plan, manifest = setup_files(tmp_path)
    first = invoke('relocate', source, '--manifest', plan, '--receipt', '--dry-run')
    assert first.returncode == 0, first.stderr
    raw = {'whitespace': json.dumps(manifest, indent=4) + '\n',
           'key-order': json.dumps(dict(reversed(list(manifest.items())))),
           'crlf': json.dumps(manifest) + '\r\n',
           'unicode': json.dumps(manifest, ensure_ascii=False)}[variant].encode()
    plan.write_bytes(raw)
    second = invoke('relocate', source, '--manifest', plan, '--receipt', '--dry-run')
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout), json.loads(second.stdout)
    assert a['manifestSha256'] != b['manifestSha256'] == digest(raw)
    assert {k: v for k, v in a.items() if k != 'manifestSha256'} == {
        k: v for k, v in b.items() if k != 'manifestSha256'}


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('failure', ['stale', 'json', 'duplicate', 'version', 'missing-key', 'extra-key',
                                    'root', 'selector', 'credential', 'source-missing', 'plan-link',
                                    'source-utf8', 'existing', 'backup', 'missing-parent', 'output-link'])
def test_refusal_has_no_digests_provenance_writes_or_applied_status(tmp_path, dry_run, failure):
    source, plan, data = setup_files(tmp_path)
    extra = ['--in-place']
    if failure == 'stale':
        source.write_bytes(b'# changed\n' + source.read_bytes())
    elif failure == 'json':
        plan.write_text('{"hostile-value":')
    elif failure == 'duplicate':
        plan.write_text(plan.read_text().replace('{', '{"scope":"hostile-value",', 1))
    elif failure in ('version', 'missing-key', 'extra-key', 'root', 'selector'):
        if failure == 'version': data['schemaVersion'] = 2
        elif failure == 'missing-key': del data['scope']
        elif failure == 'extra-key': data['extra'] = 'hostile-value'
        elif failure == 'root': data['targetRoot'] = 'hostile-value'
        else: data['paths'] = [['hostile-value', 'args', 0]]
        plan.write_text(json.dumps(data))
    elif failure == 'credential':
        source.write_bytes(source.read_bytes() + b'\n# sk-' + b'A' * 32 + b'\n')
        data['sourceSha256'] = digest(source.read_bytes())
        plan.write_text(json.dumps(data))
    elif failure == 'source-missing': source.unlink()
    elif failure == 'source-utf8': source.write_bytes(b'\xffhostile-value')
    elif failure == 'plan-link':
        link = tmp_path / 'link'
        link.symlink_to(plan)
        plan = link
    elif failure == 'backup': Path(str(source) + '.bak').write_text('original backup')
    else:
        output = tmp_path / 'output'
        if failure == 'existing': output.write_text('original')
        elif failure == 'missing-parent': output = tmp_path / 'missing' / 'output'
        else: output.symlink_to(source)
        extra = ['-o', output]
    before = snapshot(tmp_path)
    result = invoke('relocate', source, '--manifest', plan, '--receipt',
                    *extra, *(['--dry-run'] if dry_run else []))
    assert result.returncode == 2
    assert (result.stderr if dry_run else result.stdout) == ''
    error_receipt(result.stdout if dry_run else result.stderr)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('extra', [[], ['--plan'], ['--format', 'codex']])
def test_receipt_requires_manifest_and_refuses_overrides(tmp_path, extra):
    source, plan, _ = setup_files(tmp_path)
    args = explicit(source) if not extra else ['relocate', source, *extra]
    if extra == ['--format', 'codex']: args += ['--manifest', plan]
    before = snapshot(tmp_path)
    result = invoke(*args, '--receipt')
    assert result.returncode == 2 and result.stdout == ''
    error_receipt(result.stderr)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('mode', ['new', 'in-place'])
@pytest.mark.parametrize('failure', ['temp-write', 'file-fsync', 'publish'])
def test_failed_atomic_write_never_claims_applied_or_leaves_partial_config(tmp_path, monkeypatch, capsys,
                                                                        mode, failure):
    source, plan, _ = setup_files(tmp_path)
    original = source.read_bytes()
    output = tmp_path / 'output'
    before = snapshot(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('hostile-value')
    if failure == 'file-fsync': monkeypatch.setattr(cli.os, 'fsync', fail)
    elif failure == 'publish': monkeypatch.setattr(cli.os, 'link' if mode == 'new' else 'replace', fail)
    else:
        real_temp = cli.tempfile.NamedTemporaryFile
        class BadWrite:
            def __enter__(self):
                self.tmp = real_temp(mode='wb', dir=tmp_path, delete=False)
                self.name = self.tmp.name
                return self
            def write(self, data):
                self.tmp.write(data[:10])
                raise OSError('hostile-value')
            def __exit__(self, *args): self.tmp.close()
        monkeypatch.setattr(cli.tempfile, 'NamedTemporaryFile', lambda **kw: BadWrite())
    extra = ['-o', str(output)] if mode == 'new' else ['--in-place']
    assert cli.main(['relocate', str(source), '--manifest', str(plan), '--receipt', *extra]) == 2
    out = capsys.readouterr()
    assert out.out == ''
    error_receipt(out.err)
    assert source.read_bytes() == original and not output.exists()
    after = snapshot(tmp_path)
    if mode == 'in-place' and failure == 'publish':
        assert after.pop('source.bak') == original
    assert after == before


@pytest.mark.parametrize('failure', ['write', 'flush'])
def test_failed_stdout_write_or_flush_cannot_emit_applied(tmp_path, monkeypatch, capsys, failure):
    source, plan, _ = setup_files(tmp_path)
    before = snapshot(tmp_path)
    class BadStream(io.BytesIO):
        def write(self, value):
            if failure == 'write': raise OSError('hostile-value')
            return super().write(value)
        def flush(self): raise OSError('hostile-value')
    class Stdout:
        buffer = BadStream()
    monkeypatch.setattr(cli.sys, 'stdout', Stdout())
    assert cli.main(['relocate', str(source), '--manifest', str(plan), '--receipt']) == 2
    error_receipt(capsys.readouterr().err)
    assert snapshot(tmp_path) == before


def test_short_stdout_write_never_claims_applied(tmp_path, monkeypatch, capsys):
    source, plan, _ = setup_files(tmp_path)
    class ShortStream(io.BytesIO):
        def write(self, value): return super().write(value[:10])
    class Stdout:
        buffer = ShortStream()
    monkeypatch.setattr(cli.sys, 'stdout', Stdout())
    assert cli.main(['relocate', str(source), '--manifest', str(plan), '--receipt']) == 2
    error_receipt(capsys.readouterr().err)


@pytest.mark.parametrize('failure', ['backup-fsync', 'directory-fsync'])
def test_late_write_failure_has_error_with_complete_original_or_published_output(tmp_path, monkeypatch,
                                                                               capsys, failure):
    source, plan, _ = setup_files(tmp_path)
    original = source.read_bytes()
    direct = invoke(*explicit(source))
    real_fsync = cli.os.fsync
    calls = 0
    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == (2 if failure == 'backup-fsync' else 3):
            raise OSError('hostile-value')
        return real_fsync(fd)
    monkeypatch.setattr(cli.os, 'fsync', fail)
    assert cli.main(['relocate', str(source), '--manifest', str(plan), '--receipt', '--in-place']) == 2
    out = capsys.readouterr()
    assert out.out == ''
    error_receipt(out.err)
    backup = Path(str(source) + '.bak')
    assert backup.read_bytes() == original
    assert source.read_bytes() == (original if failure == 'backup-fsync' else direct.stdout.encode())
    assert sorted(p.name for p in tmp_path.iterdir()) == ['plan.json', 'source', 'source.bak']


def test_unknown_receipt_arguments_retain_masked_parser_usage(tmp_path):
    source, plan, _ = setup_files(tmp_path)
    result = invoke('relocate', source, '--manifest', plan, '--receipt', '--unknown')
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.startswith('usage:') and 'unrecognized arguments' in result.stderr


def test_closed_stdout_pipe_exits_2_with_one_error_receipt(tmp_path):
    source, plan, _ = setup_files(tmp_path)
    before = snapshot(tmp_path)
    process = subprocess.Popen([sys.executable, '-m', 'agentwrench', 'mcp-config', 'relocate', str(source),
                                '--manifest', str(plan), '--receipt'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdout.close()
    error = process.stderr.read()
    assert process.wait(timeout=10) == 2, error
    error_receipt(error.decode())
    assert snapshot(tmp_path) == before
