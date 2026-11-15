"""Whole-file admission includes real pointer metadata and failure finalization."""
import copy
import json

import pytest

from agentwrench.cassette.format import CassetteError
from agentwrench.cassette.record import Recorder


def test_actual_pointer_metadata_rejects_event_before_commit(monkeypatch):
    import agentwrench.cassette.record as module
    monkeypatch.setattr(module, 'MAX_CASSETTE', 100_000)
    recorder = Recorder(['/result/' + 'x' * 80_000])
    recorder.append('client', {'jsonrpc': '2.0', 'id': 7, 'method': 'work'})
    recorder.append('client', {'jsonrpc': '2.0', 'id': '7', 'method': 'work'})
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size))
    with pytest.raises(CassetteError, match='recording limit'):
        recorder.append('server', {'jsonrpc': '2.0', 'id': 7, 'result': {'pad': 'x' * 30_000}})
    assert (recorder.events, recorder.pending, recorder.size) == before
    assert len((json.dumps(recorder.document(-15, ['recording limit reached', 'unanswered requests remain']),
                           ensure_ascii=False, indent=2) + '\n').encode()) <= 100_000


# Independent serialization matches the public file contract, not size estimates.
def encoded(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def largest_reasons():
    return ['x' * 254] * 8


@pytest.mark.parametrize('paths', [[], ['/result/' + 'x' * 80_000],
    ['/result/' + '秘密\n"\\~0~1' * 20] * 100])
@pytest.mark.parametrize('event_count', [1, 2])
@pytest.mark.parametrize('over', [0, 1], ids=['exact', 'one-over'])
def test_file_admission_exact_boundary(monkeypatch, tmp_path, paths, event_count, over):
    import agentwrench.cassette.record as module
    import agentwrench.cassette.format as fmt
    monkeypatch.setattr(module.time, 'monotonic', lambda: 10.0)
    msg = {'jsonrpc': '2.0', 'method': 'note', 'params': {'秘密\n"\\': ['é', {}, 1]}}
    probe = Recorder(paths)
    for _ in range(event_count):
        probe.append('server', msg)
    # The maximum final failure envelope, including LF, is the admission limit.
    limit = len(encoded(probe.document(-2147483648, largest_reasons()))) - over
    monkeypatch.setattr(module, 'MAX_CASSETTE', limit)
    monkeypatch.setattr(fmt, 'MAX_CASSETTE', limit)
    recorder = Recorder(paths)
    for _ in range(event_count - 1):
        recorder.append('server', msg)
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size))
    if over:
        with pytest.raises(CassetteError, match='recording limit'):
            recorder.append('server', msg)
        assert (recorder.events, recorder.pending, recorder.size) == before
    else:
        recorder.append('server', msg)
    for code, reasons in [(0, []), (None, ['server could not be started']),
                          (-2147483648, largest_reasons()),
                          (2147483647, ['recording limit reached', 'unanswered requests remain'])]:
        data = recorder.document(code, reasons)
        fmt.validate_cassette(data)
        assert data['redact_paths'] == paths
        path = tmp_path / 'capture.json'
        fmt.save_cassette(path, data)
        assert path.read_bytes() == encoded(data)
        assert path.stat().st_size <= limit
        if not over and reasons == largest_reasons():
            assert path.stat().st_size == limit


@pytest.mark.parametrize('direction', ['client', 'server'])
@pytest.mark.parametrize('kind', ['request', 'response'])
def test_file_rejection_keeps_typed_pending_lifetimes(monkeypatch, direction, kind):
    import agentwrench.cassette.record as module
    monkeypatch.setattr(module.time, 'monotonic', lambda: 10.0)
    recorder = Recorder(['/result/absent'])
    for owner in ('client', 'server'):
        for value in (7, '7'):
            recorder.append(owner, {'jsonrpc': '2.0', 'method': 'work', 'id': value})
    msg = {'jsonrpc': '2.0', 'id': 8, 'method': 'work'} if kind == 'request' else {
        'jsonrpc': '2.0', 'id': 7, 'result': {}}
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size))
    candidate = recorder.document(-2147483648, largest_reasons())
    candidate['events'] = [*recorder.events, {'seq': 4, 'ms': 0.0, 'direction': direction, 'message': msg}]
    monkeypatch.setattr(module, 'MAX_CASSETTE', len(encoded(candidate)) - 1)
    with pytest.raises(CassetteError, match='recording limit'):
        recorder.append(direction, msg)
    assert (recorder.events, recorder.pending, recorder.size) == before


@pytest.mark.parametrize('over', [0, 1], ids=['exact', 'one-over'])
def test_metadata_preflight_before_server_or_destination(monkeypatch, tmp_path, over, capsys):
    import agentwrench.cassette.record as module
    from agentwrench.cassette.cli import main
    paths = ['/result/' + '秘密\n"\\~0~1' * 2000] * 3
    limit = len(encoded(Recorder(paths).document(-2147483648, largest_reasons()))) - over
    monkeypatch.setattr(module, 'MAX_CASSETTE', limit)
    if not over:
        recorder = Recorder(paths)
        assert recorder.paths == paths
        assert len(encoded(recorder.document(-2147483648, largest_reasons()))) == limit
        return
    async def must_not_launch(*args, **kwargs):
        pytest.fail('metadata rejection must precede server launch')
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', must_not_launch)
    path, report = tmp_path / 'capture.json', tmp_path / 'report.json'
    sentinel = b'previous destination\n'
    path.write_bytes(sentinel)
    args = ['record', '--cassette', str(path), '--report', str(report)]
    for pointer in paths:
        args += ['--redact-path', pointer]
    assert main([*args, '--', 'must-not-launch']) == 2
    assert path.read_bytes() == sentinel
    assert json.loads(report.read_bytes()) == {'exit_code': 2, 'unmatched': 0, 'unused': 0}
    out = capsys.readouterr()
    assert out.out == '' and 'recording metadata exceeds' in out.err
    assert len(out.err.encode()) < 100
    assert not list(tmp_path.glob('.mcp-cassette-*'))


def test_final_reasons_are_bounded_without_truncating_data():
    from agentwrench.cassette.record import MAX_REASON_JSON_BYTES, MAX_REASONS
    paths = ['/result/absent']
    recorder = Recorder(paths)
    paths.append('/result/later')  # caller mutation must not defeat preflight
    recorder.append('server', {'jsonrpc': '2.0', 'method': 'note', 'params': {'text': '秘密\n"\\'}})
    originals = copy.deepcopy(recorder.events)
    for reasons in [[], ['x' * 254], ['x' * 255], ['秘密' * 100], ['\n' * 128],
                    ['same failure'] * 100, ['first'] + ['x' * 10000] * 20]:
        data = recorder.document(-15, reasons)
        actual = data['metadata']['reasons']
        assert len(actual) <= MAX_REASONS == 8
        assert all(len(json.dumps(reason, ensure_ascii=False).encode()) <= MAX_REASON_JSON_BYTES == 256
                   for reason in actual)
        assert len(encoded(data)) <= recorder.metadata_size + recorder.size + 2
        assert data['incomplete'] is bool(reasons)
        assert data['events'] == originals
        assert data['redact_paths'] == ['/result/absent']
        if len(reasons) > 8:
            assert actual[-1] == 'additional recording failures omitted'
        if reasons and len(json.dumps(reasons[0]).encode()) > 256:
            assert actual[0] == 'recording failure (diagnostic omitted)'
