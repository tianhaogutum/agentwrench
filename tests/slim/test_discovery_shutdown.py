"""Port of test/discovery-shutdown.test.js (--shutdown-timeout-ms)."""

import asyncio
import json
import math
import os
import re

import pytest

from agentwrench.slim import timers
from agentwrench.slim.config import InputError
from agentwrench.slim.discover import DiscoveryError, run_discover

from .conftest import PYTHON, TimerSpy, fixture, run, signal_handlers, spawn_marker


def server(mode):
    return [PYTHON, fixture('discovery_shutdown_server.py'), mode]


SNAPSHOT = json.dumps({'tools': [{'name': 'read_🦊', 'inputSchema': {'type': 'object'}}]}, ensure_ascii=False, separators=(',', ':')) + '\n'


def discover(*args, **kwargs):
    return asyncio.run(run_discover(*args, **kwargs))


def test_shutdown_grace_runtime_permits_a_longer_clean_eof_flush():
    assert discover(server('delay-1500'), {}, shutdown_timeout_ms=2500) == SNAPSHOT


def test_shutdown_grace_runtime_enforces_a_shorter_eof_grace():
    with pytest.raises(DiscoveryError, match='shutdown exceeded grace period'):
        discover(server('delay-250'), {}, shutdown_timeout_ms=100)


def test_shutdown_grace_invalid_runtime_options_reject_before_spawn(tmp_dir):
    for shutdown_timeout_ms in (0, -1, 1.5, math.nan, math.inf, '100', 30001, True):
        with pytest.raises(InputError, match='--shutdown-timeout-ms: expected an integer from 1 to 30000'):
            discover(spawn_marker(tmp_dir / 'spawned'), {}, shutdown_timeout_ms=shutdown_timeout_ms)
    assert os.listdir(tmp_dir) == []


def test_shutdown_grace_cli_bounds_duplicates_and_disallowed_modes_reject_before_spawn(tmp_dir):
    command = ['--', *spawn_marker(tmp_dir / 'spawned')]
    for values in [[], [''], ['0'], ['-1'], ['1.5'], ['1e3'], ['+1'], ['NaN'], ['Infinity'], ['30001'], ['99999999999999999'], ['1', '--shutdown-timeout-ms', '2']]:
        result = run('discover', '--shutdown-timeout-ms', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--shutdown-timeout-ms: (missing value|duplicate option|expected an integer from 1 to 30000)', result.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--shutdown-timeout-ms', '100', *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--shutdown-timeout-ms: unsupported option', result.stderr)
    assert os.listdir(tmp_dir) == []
    assert re.search('discover .*--shutdown-timeout-ms', run('--help').stdout)


def test_shutdown_grace_success_starts_only_after_collection_default_and_accepted_endpoints_retain_bytes(monkeypatch):
    # List collection takes longer than the chosen EOF grace.
    assert discover(server('slow-list'), {}, shutdown_timeout_ms=100) == SNAPSHOT
    with pytest.raises(DiscoveryError, match='shutdown exceeded grace period'):
        discover(server('delay-250'), {}, shutdown_timeout_ms=1)
    # Observe the default without relying on a narrow wall-clock window.
    spy = TimerSpy(monkeypatch)
    assert discover(server('immediate'), {}) == SNAPSHOT
    assert 1000 in spy.durations
    for ms in ('30000', '5000'):
        result = run('discover', '--shutdown-timeout-ms', ms, '--', *server('immediate'))
        assert result.returncode == 0, result.stderr
        assert result.stdout == SNAPSHOT


def test_shutdown_grace_deterministic_close_before_equal_after_deadline_and_overdue_callback(monkeypatch):
    clock = {'now': 0, 'close_at': 4320, 'grace': None}
    monkeypatch.setattr(timers, 'now', lambda: clock['now'])

    def on_set(timer, ms):
        if ms == 4321:
            clock['grace'] = timer
            clock['now'] = clock['close_at']

    spy = TimerSpy(monkeypatch, delay=lambda ms: 20000 if ms == 4321 else ms, on_set=on_set)
    listeners = signal_handlers()
    for close_at in (4320, 4321, 4322):
        clock['close_at'] = close_at
        clock['now'] = 0
        if close_at < 4321:
            assert discover(server('immediate'), {}, shutdown_timeout_ms=4321) == SNAPSHOT
        else:
            with pytest.raises(DiscoveryError, match='shutdown exceeded grace period'):
                discover(server('immediate'), {}, shutdown_timeout_ms=4321)
        assert clock['grace'] in spy.cleared
    assert signal_handlers() == listeners


def test_shutdown_grace_timer_expiry_and_unsuccessful_exit_preserve_existing_absent_file_destinations(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    for exists in (True, False):
        if exists:
            output.write_text('sentinel')
        else:
            output.unlink()
        for mode in ('delay-250', 'exit'):
            result = run('discover', '--shutdown-timeout-ms', '100', '--output', str(output), '--', *server(mode))
            assert result.returncode == 1, result.stderr
            assert result.stdout == ''
            assert re.search('exited unsuccessfully' if mode == 'exit' else 'shutdown exceeded grace period', result.stderr)
            if exists:
                assert output.read_text() == 'sentinel'
            assert os.listdir(tmp_dir) == (['catalogue.json'] if exists else [])
    result = run('discover', '--shutdown-timeout-ms', '2500', '--output', str(output), '--', *server('delay-1500'))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert output.read_text(encoding='utf-8') == SNAPSHOT


def test_shutdown_grace_overall_deadline_protocol_wire_failure_and_sigterm_ignoring_do_not_await_long_success_grace(tmp_dir):
    for mode, options, diagnostic in [
        ('hang', ['--discovery-timeout-ms', '500'], 'discovery duration limit exceeded'),
        ('protocol', [], 'invalid JSON-RPC stdout'),
        ('overflow', ['--max-wire-bytes', '500'], 'incoming wire bytes'),
        ('hang', ['--shutdown-timeout-ms', '100'], 'shutdown exceeded grace period'),
    ]:
        trace = tmp_dir / f"{mode}-{options[0] if options else 'none'}.log"
        result = run('discover', *([] if options and options[0] == '--shutdown-timeout-ms' else ['--shutdown-timeout-ms', '30000']), *options,
                     '--', *server(mode), str(trace))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search(diagnostic, result.stderr)
        assert trace.read_text() == 'initialize\nnotifications/initialized\ntools/list\nEOF\nSIGTERM\n'


def test_shutdown_grace_failures_before_eof_do_not_start_grace_shutdown_failure_clears_all_timers_and_signal_handlers(monkeypatch):
    spy = TimerSpy(monkeypatch)
    listeners = signal_handlers()
    with pytest.raises(DiscoveryError, match='pagination loop'):
        discover([PYTHON, fixture('discovery_server.py'), 'cycle'], {}, shutdown_timeout_ms=4321, discovery_timeout_ms=5000)
    assert len(spy.with_ms(4321)) == 0
    with pytest.raises(DiscoveryError, match='invalid JSON-RPC stdout'):
        discover(server('protocol'), {}, shutdown_timeout_ms=4321, discovery_timeout_ms=5000)
    assert len(spy.with_ms(4321)) == 1
    assert all(timer in spy.cleared for timer, _ in spy.timers)
    assert all(not timer.has_ref() for timer in spy.with_ms(4321, 5000))
    assert signal_handlers() == listeners
