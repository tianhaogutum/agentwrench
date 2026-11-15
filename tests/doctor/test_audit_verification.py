"""Read-only accepted-prefix replay: CLI contracts and adversarial metadata."""
import json
from pathlib import Path
from dataclasses import asdict
from io import BytesIO

import pytest

from agentwrench.doctor.audit import audit
from .make_fixtures import cnode, item, use
from .test_audit_migration import frozen
from .test_cli import cli
from .test_engine import raw
from .test_incremental_audit import SEQUENCES


def verify(path, checkpoint, *args):
    return cli('verify-audit', path, '--checkpoint', checkpoint, '--json', *args)


@pytest.mark.parametrize('fmt,record', [
    ('codex', item('function_call', 'c', arguments='PRIVATE-PAYLOAD')),
    ('claude', cnode('assistant', 'a', blocks=[use('c')]))])
def test_verification_cli_match_and_well_formed_mismatch(tmp_path, fmt, record):
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    path.write_bytes(raw(record) + b'{"unaccepted":')
    audit(path, checkpoint, fmt, True)
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint, '--format', fmt)
    assert run.returncode == 0, run.stderr
    expected = dict(file=str(path), checkpoint=str(checkpoint), version=2, format=fmt,
                    offset=len(raw(record)), status='verified', mismatches=[], history_verified=False)
    assert json.loads(run.stdout) == expected
    assert before == (frozen(path), frozen(checkpoint))
    state = json.loads(checkpoint.read_text())
    state['pairing']['events'][0]['call_id'] = 'tampered-private-id'
    checkpoint.write_text(json.dumps(state))
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint, '--format', fmt)
    assert run.returncode == 1, run.stderr
    assert json.loads(run.stdout) == {**expected, 'status': 'mismatch', 'mismatches': ['events']}
    assert 'tampered-private-id' not in run.stdout and 'PRIVATE-PAYLOAD' not in run.stdout
    assert before == (frozen(path), frozen(checkpoint))


def seed(tmp_path, fmt='codex'):
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    if fmt == 'codex':
        records = [item('function_call', 'c'), item('message', content='SECRET-é'),
                   item('function_call_output', 'c'), item('function_call_output'),
                   item('message', content='SECRET-TAIL')]
    else:
        from .make_fixtures import result
        records = [cnode('assistant', 'a', blocks=[use('c')]), {'unknown': 'SECRET-é'},
                   cnode('user', 'b', 'a', [result('c')]),
                   cnode('assistant', 'bad', 'b', [{'type': 'tool_use'}]), {'unknown': 'SECRET-TAIL'}]
    path.write_bytes(b''.join((json.dumps(r, ensure_ascii=False) + '\r\n').encode() for r in records))
    audit(path, checkpoint)
    return path, checkpoint, json.loads(checkpoint.read_text())


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('field', ['identifier', 'kind', 'event-line', 'issue-detail', 'issue-code',
    'issue-id', 'issue-block', 'lost-issue', 'extra-issue', 'lines', 'terminated',
    'range-start', 'range-end', 'lost-event', 'format'])
def test_internally_valid_tampering_detected(tmp_path, fmt, field):
    from agentwrench.doctor.audit import _validate_state
    from agentwrench.doctor.audit_verification import verify_audit
    path, checkpoint, state = seed(tmp_path, fmt)
    p = state['pairing']
    if field == 'identifier':
        p['events'][0]['call_id'] = 'forged'
    elif field == 'kind':
        if fmt == 'codex':
            p['events'][0].update(kind='custom_tool_call', output_kind='custom_tool_call_output')
        else:
            p['events'][0].update(kind='tool_result', output_kind='')
    elif field == 'event-line':
        p['events'][0]['line'] = 2
        if fmt == 'claude':
            p['nodes']['a']['line'] = 2
        p['locations'][0] = [2, p['locations'][0][2], p['locations'][1][1]]
    elif field.startswith('issue-'):
        key = {'issue-detail': 'detail', 'issue-code': 'code', 'issue-id': 'call_id', 'issue-block': 'block'}[field]
        p['issues'][0][key] = {'detail': 'forged-private-detail', 'code': 'RD207' if fmt == 'claude' else 'RD107',
                              'call_id': 'forged-id', 'block': 4}[key]
    elif field == 'lost-issue':
        p['issues'] = []
        if fmt == 'codex':
            p['locations'].pop()  # Claude retains the node on this line.
    elif field == 'extra-issue':
        p['issues'].append(dict(p['issues'][0]))
    elif field == 'lines':
        p['lines'] += 1  # Final payload-only line makes this schema-valid.
    elif field == 'terminated':
        p['terminated'] = False
    elif field == 'range-start':
        p['locations'][1][1] -= 1
    elif field == 'range-end':
        p['locations'][0][2] -= 1
    elif field == 'lost-event':
        p['events'].pop(0)
        if fmt == 'codex':
            p['locations'].pop(0)
    elif field == 'format':
        # A marker with no events/nodes lets either format pass strict schema.
        checkpoint.unlink()
        path.write_bytes(raw(item('message')))
        audit(path, checkpoint)
        state = json.loads(checkpoint.read_text())
        state['pairing']['format'] = 'claude'
    _validate_state(state)  # Proves this is semantic mismatch, not invalid schema.
    checkpoint.write_text(json.dumps(state))
    before = frozen(path), frozen(checkpoint), set(tmp_path.iterdir())
    report = verify_audit(path, checkpoint)
    assert report['status'] == 'mismatch' and report['mismatches']
    assert before == (frozen(path), frozen(checkpoint), set(tmp_path.iterdir()))
    assert 'forged' not in json.dumps(report)


@pytest.mark.parametrize('field', ['parent', 'session', 'sidechain', 'node-id', 'node-line', 'block', 'lost-node'])
def test_well_formed_graph_tampering_detected(tmp_path, field):
    from agentwrench.doctor.audit import _validate_state
    from agentwrench.doctor.audit_verification import verify_audit
    path, checkpoint, state = seed(tmp_path, 'claude')
    p = state['pairing']
    if field in ('parent', 'session', 'sidechain'):
        p['nodes']['b'][field] = {'parent': 'absent-parent', 'session': 'other', 'sidechain': True}[field]
    elif field == 'node-id':
        p['nodes']['forged'] = p['nodes'].pop('a')
        p['events'][0]['node'] = 'forged'
    elif field == 'node-line':
        p['nodes']['bad']['line'] = 5
        p['issues'][0]['line'] = 5
        p['locations'][-1] = [5, p['locations'][-1][2], state['offset']]
    elif field == 'block':
        p['events'][0]['block'] = 7
    elif field == 'lost-node':
        del p['nodes']['bad']  # The structural issue still references the line.
    _validate_state(state)
    checkpoint.write_text(json.dumps(state))
    before = frozen(path), frozen(checkpoint)
    report = verify_audit(path, checkpoint)
    assert report['status'] == 'mismatch'
    if field == 'block':
        assert report['mismatches'] == ['events']
    else:
        assert 'nodes' in report['mismatches']
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('case', ['v1', 'future', 'missing', 'node-ref', 'line', 'kind', 'output-kind',
    'location', 'lost-location', 'duplicate-location', 'derived-issue', 'unknown-field',
    'format', 'terminated', 'lines', 'node-line', 'node-scope', 'history', 'offset-bool',
    'duplicate-key', 'json-array', 'invalid-utf8', 'extra-envelope'])
def test_invalid_schema_is_refusal(tmp_path, case):
    from .test_incremental_audit import corrupt
    path, checkpoint, state = seed(tmp_path, 'claude')
    # corrupt() expects node a at line 1 and at least one event.
    corrupt(state, case)
    if case == 'history':
        state['emitted'] = ['not-a-hash']
    elif case == 'offset-bool':
        state['offset'] = True
    elif case == 'extra-envelope':
        state['unknown'] = 1
    checkpoint.write_text(json.dumps(state))
    if case == 'duplicate-key':
        checkpoint.write_bytes(checkpoint.read_bytes().replace(b'"version": 2', b'"version": 2, "version": 2'))
    elif case == 'json-array':
        checkpoint.write_bytes(b'[]')
    elif case == 'invalid-utf8':
        checkpoint.write_bytes(b'\xff')
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint)
    assert run.returncode == 2 and 'error' in json.loads(run.stdout), run.stdout
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('case', ['stale', 'truncated', 'rotated', 'path', 'device', 'inode', 'format', 'digest', 'split', 'joined'])
def test_identity_hash_and_boundary_refusals(tmp_path, case):
    import hashlib
    path, checkpoint, state = seed(tmp_path)
    data = path.read_bytes()
    if case == 'stale':
        path.write_bytes(data.replace(b'"c"', b'"d"'))
    elif case == 'truncated':
        path.write_bytes(data[:-10])
    elif case == 'rotated':
        path.rename(tmp_path / 'old-source')
        path.write_bytes(data)
    elif case == 'path':
        state['file'] = str(tmp_path / 'other')
    elif case in ('device', 'inode'):
        state[case] += 1
    elif case == 'digest':
        state['digest'] = 'f' * 64
    elif case in ('split', 'joined'):
        data = raw(item('function_call', 'c')).rstrip(b'\n')
        path.write_bytes(data + (b'{}\n' if case == 'joined' else b''))
        checkpoint.unlink()
        # Create a legitimate schema then move cursor/hash to an invalid boundary.
        path.write_bytes(data)
        audit(path, checkpoint)
        state = json.loads(checkpoint.read_text())
        if case == 'joined':
            path.write_bytes(data + b'{}\n')
        else:
            state['offset'] -= 2
            state['pairing']['locations'][0][2] -= 2
            state['digest'] = hashlib.sha256(data[:-2]).hexdigest()
    checkpoint.write_text(json.dumps(state))
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint, *(('--format', 'codex') if case == 'format' else ()))
    assert run.returncode == 2 and 'error' in json.loads(run.stdout), run.stdout
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('fmt', ['auto', 'codex', 'claude'])
@pytest.mark.parametrize('tail', [b'', b'{"type":', b'INVALID TAIL\n'])
def test_zero_cursor_does_not_decode_tail(tmp_path, fmt, tail):
    from agentwrench.doctor.audit_verification import verify_audit
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    path.write_bytes(b'')
    audit(path, checkpoint, fmt)
    path.write_bytes(tail)
    before = frozen(path), frozen(checkpoint)
    assert verify_audit(path, checkpoint, fmt)['status'] == 'verified'
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('fmt', ['codex', 'claude'])
@pytest.mark.parametrize('tail', [b'', b'\n', b'\r\n', b' \t\r\nINVALID TAIL\n', b' \t', b'\r'])
def test_unterminated_accepted_object_and_separator_only_lookahead(tmp_path, fmt, tail):
    from agentwrench.doctor.audit_verification import verify_audit
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    record = item('function_call', 'é') if fmt == 'codex' else cnode('assistant', 'a', blocks=[use('é')])
    prefix = raw(record).rstrip(b'\n')
    path.write_bytes(prefix)
    audit(path, checkpoint)
    with path.open('ab') as stream:
        stream.write(tail)
    before = frozen(path), frozen(checkpoint)
    report = verify_audit(path, checkpoint)
    assert report['status'] == 'verified' and report['offset'] == len(prefix)
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('records', SEQUENCES)
def test_replay_every_accepted_snapshot_once_without_tail(tmp_path, monkeypatch, records, explicit):
    import agentwrench.doctor.audit_verification as module
    from contextlib import contextmanager
    fmt = ('codex' if records[0]['type'] == 'response_item' else 'claude') if explicit else 'auto'
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    original_decode, original_open = module.decode, module.open_session
    chunks, decoded = [], []
    limit = 0
    class Bounded:
        def __init__(self, source):
            self.source = source
        def __getattr__(self, key):
            return getattr(self.source, key)
        def read(self, n=-1):
            assert n >= 0 and self.source.tell() + n <= limit, 'tail was read'
            return self.source.read(n)
        def readline(self, n=-1):
            assert n >= 0 and self.source.tell() + n <= limit, 'tail was read'
            return self.source.readline(n)
    @contextmanager
    def bounded_open(target):
        with original_open(target) as source:
            yield Bounded(source) if target == path else source
    def watch(data):
        if data != checkpoint.read_bytes():
            decoded.append(data)
        return original_decode(data)
    monkeypatch.setattr(module, 'decode', watch)
    monkeypatch.setattr(module, 'open_session', bounded_open)
    for record in records:
        chunks.append(raw(record).replace(b'\n', b'\r\n'))
        prefix = b''.join(chunks)
        limit = len(prefix)
        path.write_bytes(prefix + b'{"partial-tail":')
        audit(path, checkpoint, fmt, True)
        # Both resolved history and absent current hashes must be accepted unchanged.
        state = json.loads(checkpoint.read_text())
        from agentwrench.doctor.engine import scan
        full = scan(BytesIO(prefix), str(path), fmt)
        assert state['pairing']['events'] == [asdict(e) for e in full.events]
        assert state['pairing']['nodes'] == {uid: asdict(n) for uid, n in full.nodes.items()}
        assert state['pairing']['issues'] == [asdict(i) for i in full.issues
            if i.code in ('RD103', 'RD107', 'RD206') or i.detail.startswith('duplicate uuid:')]
        assert state['pairing']['lines'] == full.lines
        offset, ranges = 0, {}
        for line, chunk in enumerate(chunks, 1):
            ranges[line] = [line, offset, offset + len(chunk)]
            offset += len(chunk)
        for location in state['pairing']['locations']:
            assert location == ranges[location[0]]
        state['emitted'] = ['f' * 64, 'f' * 64]
        checkpoint.write_text(json.dumps(state))
        before = frozen(path), frozen(checkpoint)
        decoded.clear()
        report = module.verify_audit(path, checkpoint, fmt)
        assert report['status'] == 'verified' and report['history_verified'] is False
        assert decoded == chunks
        assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('endpoint', ['s', 'state', 'state.lock'])
@pytest.mark.parametrize('kind', ['missing', 'symlink', 'loop', 'fifo', 'directory'])
def test_unsafe_endpoints_refused(tmp_path, endpoint, kind):
    import os
    path, checkpoint, _ = seed(tmp_path)
    target = tmp_path / endpoint
    target.rename(tmp_path / 'saved')
    if kind == 'symlink':
        target.symlink_to(tmp_path / 'saved')
    elif kind == 'loop':
        target.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(target)
    elif kind == 'directory':
        target.mkdir()
    if kind == 'missing' and endpoint == 'state.lock':
        # Stable sibling locks may be created; no checkpoint replacement occurs.
        before = frozen(path), frozen(checkpoint)
        assert verify(path, checkpoint).returncode == 0
        assert before == (frozen(path), frozen(checkpoint))
        return
    before = frozen(tmp_path / 'saved')
    run = verify(path, checkpoint)
    assert run.returncode == 2, (run.stdout, run.stderr)
    assert frozen(tmp_path / 'saved') == before
    assert not list(tmp_path.glob('.rollout-doctor-*'))


@pytest.mark.parametrize('pair,alias', [(pair, 'hardlink') for pair in
    [('s', 'state'), ('s', 'state.lock'), ('state', 'state.lock')]] +
    [(('s', 'state'), alias) for alias in ('samepath', 'parent')])
def test_input_and_lock_aliases_refused(tmp_path, pair, alias):
    import os
    path, checkpoint, _ = seed(tmp_path)
    first, second = [tmp_path / x for x in pair]
    if alias == 'hardlink':
        second.unlink()
        os.link(first, second)
    elif alias == 'samepath':
        checkpoint = path
    else:
        (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
        checkpoint = tmp_path / 'alias' / 's'
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint)
    assert run.returncode == 2, (run.stdout, run.stderr)
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('endpoint', ['s', 'state', 'state.lock'])
def test_locked_endpoints_refused(tmp_path, endpoint):
    import fcntl
    path, checkpoint, _ = seed(tmp_path)
    before = frozen(path), frozen(checkpoint)
    with (tmp_path / endpoint).open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = verify(path, checkpoint)
    assert run.returncode == 2 and 'locked' in json.loads(run.stdout)['error']
    assert before == (frozen(path), frozen(checkpoint))


@pytest.mark.parametrize('endpoint', ['s', 'state'])
@pytest.mark.parametrize('change', ['append', 'rewrite-restored-mtime', 'rotate', 'unlink', 'chmod'])
@pytest.mark.parametrize('stage', ['hash', 'replay', 'compare'])
def test_changes_during_verification_refused(tmp_path, monkeypatch, endpoint, change, stage):
    import os
    import agentwrench.doctor.audit_verification as module
    path, checkpoint, _ = seed(tmp_path)
    target, other = (path, checkpoint) if endpoint == 's' else (checkpoint, path)
    before = frozen(other)
    changed = False
    def mutate():
        nonlocal changed
        if changed:
            return
        changed = True
        if change == 'append':
            with target.open('ab') as out:
                out.write(b' ')
        elif change == 'rewrite-restored-mtime':
            st = target.stat()
            data = target.read_bytes()
            target.write_bytes(data.replace(b'"c"', b'"d"'))
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        elif change == 'rotate':
            data = target.read_bytes()
            target.rename(tmp_path / 'rotated')
            target.write_bytes(data)
        elif change == 'unlink':
            target.unlink()
        elif change == 'chmod':
            target.chmod(0o400)
    key = {'hash': '_hash_prefix', 'replay': 'add_record', 'compare': 'pack'}[stage]
    original = getattr(module, key)
    def hook(*args, **kwargs):
        value = original(*args, **kwargs)
        mutate()
        return value
    monkeypatch.setattr(module, key, hook)
    with pytest.raises((module.DoctorError, OSError)):
        module.verify_audit(path, checkpoint)
    assert changed and frozen(other) == before
    assert not list(tmp_path.glob('.rollout-doctor-*'))


@pytest.mark.parametrize('accepted', [b'[]\n', b'{bad}\n', b'{"x":1,"x":2}\n', b'\xff\n',
    b'{"type":"response_item","payload":null}\n', b'{"unknown":true}\n'])
def test_invalid_accepted_prefix_refused_even_with_matching_hash(tmp_path, accepted):
    import hashlib
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    path.write_bytes(raw(item('message')))
    audit(path, checkpoint)
    state = json.loads(checkpoint.read_text())
    path.write_bytes(accepted)
    state.update(offset=len(accepted), digest=hashlib.sha256(accepted).hexdigest())
    checkpoint.write_text(json.dumps(state))
    before = frozen(path), frozen(checkpoint)
    assert verify(path, checkpoint).returncode == 2
    assert before == (frozen(path), frozen(checkpoint))


def test_bad_hash_is_refused_before_any_record_decoding(tmp_path, monkeypatch):
    import agentwrench.doctor.audit_verification as module
    path, checkpoint, _ = seed(tmp_path)
    path.write_bytes(path.read_bytes().replace(b'"c"', b'"d"'))
    original, decoded = module.decode, []
    def watch(data):
        decoded.append(data)
        return original(data)
    monkeypatch.setattr(module, 'decode', watch)
    with pytest.raises(module.DoctorError, match='SHA-256'):
        module.verify_audit(path, checkpoint)
    assert decoded == [checkpoint.read_bytes()]


def test_human_cli_and_no_write_helpers(tmp_path, monkeypatch, capsys):
    import tempfile
    import agentwrench.doctor.audit as audit_module
    from agentwrench.doctor.cli import main
    path, checkpoint, _ = seed(tmp_path)
    before = frozen(path), frozen(checkpoint), set(tmp_path.iterdir())
    def forbidden(*args, **kwargs):
        pytest.fail('verification must not write or pair diagnostic findings')
    monkeypatch.setattr(audit_module, '_save', forbidden)
    monkeypatch.setattr(audit_module, 'pair', forbidden)
    monkeypatch.setattr(tempfile, 'mkstemp', forbidden)
    assert main(['verify-audit', str(path), '--checkpoint', str(checkpoint)]) == 0
    assert 'verified at byte' in capsys.readouterr().out
    state = json.loads(checkpoint.read_text())
    state['pairing']['events'][0]['call_id'] = 'other'
    checkpoint.write_text(json.dumps(state))
    after_edit = frozen(checkpoint)
    assert main(['verify-audit', str(path), '--checkpoint', str(checkpoint)]) == 1
    assert 'differing cached fields: events' in capsys.readouterr().out
    assert before[0] == frozen(path) and after_edit == frozen(checkpoint)
    assert before[2] == set(tmp_path.iterdir())


def test_device_source_refused(tmp_path):
    path, checkpoint, _ = seed(tmp_path)
    before = frozen(checkpoint)
    run = verify(Path('/dev/null'), checkpoint)
    assert run.returncode == 2 and 'regular files' in json.loads(run.stdout)['error']
    assert frozen(checkpoint) == before


@pytest.mark.parametrize('tail', [b'{}\n', b' \t{}\n', b'\r{}\n'])
@pytest.mark.parametrize('cached_terminated', [True, False])
def test_boundary_checked_using_replay_not_cached_separator_flag(tmp_path, tail, cached_terminated):
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    prefix = raw(item('function_call', 'c')).rstrip(b'\n')
    path.write_bytes(prefix)
    audit(path, checkpoint)
    state = json.loads(checkpoint.read_text())
    state['pairing']['terminated'] = cached_terminated
    checkpoint.write_text(json.dumps(state))
    with path.open('ab') as stream:
        stream.write(tail)
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint)
    assert run.returncode == 2 and 'cursor boundary' in json.loads(run.stdout)['error']
    assert before == (frozen(path), frozen(checkpoint))


def test_multichunk_prefix_and_no_accepted_payloads_in_output(tmp_path):
    path, checkpoint = tmp_path / 's', tmp_path / 'state'
    path.write_bytes(raw(item('function_call', 'c', arguments='SECRET-' * 200000)))
    audit(path, checkpoint)
    before = frozen(path), frozen(checkpoint)
    run = verify(path, checkpoint)
    assert run.returncode == 0 and json.loads(run.stdout)['offset'] == path.stat().st_size
    assert 'SECRET' not in run.stdout and 'SECRET' not in run.stderr
    assert before == (frozen(path), frozen(checkpoint))
