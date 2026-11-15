"""Port of test/discovery-timeout.test.js (--discovery-timeout-ms)."""

import asyncio
import math
import os
import re
import signal

import pytest

from agentwrench.slim import timers
from agentwrench.slim.config import InputError
from agentwrench.slim.discover import DiscoveryDeadline, DiscoveryError, run_discover, validate_discovery_timeout

from .conftest import EXAMPLE_CONFIG, PYTHON, TimerSpy, fixture, run, signal_handlers, spawn_marker


def server(mode):
    return [PYTHON, fixture('discovery_deadline_server.py'), mode]


def test_overall_discovery_deadline_runtime_stops_quick_sequential_pages_despite_independent_request_deadlines():
    with pytest.raises(DiscoveryError, match='discovery duration limit exceeded'):
        asyncio.run(run_discover(server('many'), {}, discovery_timeout_ms=500, request_timeout_ms=2000))


def test_overall_discovery_deadline_filtered_and_truly_empty_pages_cannot_reset_the_budget_or_publish_output(tmp_dir):
    output, config = tmp_dir / 'catalogue.json', tmp_dir / 'slim.json'
    config.write_text('{"allow":[]}')
    for mode in ('many', 'empty'):
        log = tmp_dir / f'{mode}.log'
        output.write_text('sentinel')
        for destination in ([], ['--output', str(output)]):
            log.write_text('')
            result = run('discover', '--discovery-timeout-ms', '500', '--request-timeout-ms', '2000',
                         '--config', str(config), *destination, '--', *server(mode), str(log))
            assert result.returncode == 1, result.stderr
            assert result.stdout == ''
            assert re.search('discovery duration limit exceeded', result.stderr)
            assert output.read_text() == 'sentinel'
            methods = log.read_text().strip().split('\n')
            assert methods[:2] == ['initialize', 'notifications/initialized']
            assert methods.count('tools/list') >= 2, ','.join(methods)
            assert methods.count('tools/list') < 100
            assert all(m in ('initialize', 'notifications/initialized', 'tools/list') for m in methods)
    assert sorted(os.listdir(tmp_dir)) == ['catalogue.json', 'empty.log', 'many.log', 'slim.json']


def test_overall_discovery_deadline_finite_stdout_and_file_catalogues_are_byte_identical_with_the_limit_omitted_or_enabled(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    for config in ([], ['--config', str(EXAMPLE_CONFIG)]):
        command = ['--', PYTHON, fixture('discovery_server.py')]
        baseline = run('discover', *config, *command)
        assert baseline.returncode == 0, baseline.stderr
        for ms in ('5000', '2147483647'):
            bounded = run('discover', '--discovery-timeout-ms', ms, *config, *command)
            assert bounded.returncode == 0, bounded.stderr
            assert bounded.stdout == baseline.stdout
            assert bounded.stderr == baseline.stderr
            output.write_text('sentinel')
            file = run('discover', '--discovery-timeout-ms', ms, '--output', str(output), *config, *command)
            assert file.returncode == 0, file.stderr
            assert file.stdout == ''
            assert output.read_text(encoding='utf-8') == baseline.stdout


def test_overall_discovery_deadline_initialization_clean_shutdown_and_sigterm_ignoring_child_remain_bounded():
    for mode in ('initialize', 'shutdown', 'stubborn'):
        result = run('discover', '--discovery-timeout-ms', '500', '--request-timeout-ms', '2000', '--', *server(mode))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search('discovery duration limit exceeded', result.stderr)
    request_first = run('discover', '--discovery-timeout-ms', '5000', '--request-timeout-ms', '100', '--', *server('initialize'))
    assert request_first.returncode == 1, request_first.stderr
    assert request_first.stdout == ''
    assert re.search('discovery request timed out', request_first.stderr)
    # Smallest accepted timer must be a runtime failure, not an input error.
    minimum = run('discover', '--discovery-timeout-ms', '1', '--', *server('initialize'))
    assert minimum.returncode == 1, minimum.stderr
    assert minimum.stdout == ''
    assert re.search('discovery duration limit exceeded', minimum.stderr)


def test_overall_discovery_deadline_invalid_values_and_other_modes_fail_before_spawn(tmp_dir):
    command = ['--', *spawn_marker(tmp_dir / 'spawned')]
    for values in [[], [''], ['0'], ['-1'], ['1.5'], ['1e3'], ['+1'], ['NaN'], ['Infinity'], ['2147483648'], ['999999999999999999999'], ['1', '--discovery-timeout-ms', '2']]:
        result = run('discover', '--discovery-timeout-ms', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--discovery-timeout-ms: (missing value|duplicate option|expected an integer from 1 to 2147483647)', result.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--discovery-timeout-ms', '100', *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--discovery-timeout-ms: unsupported option', result.stderr)
    assert os.listdir(tmp_dir) == []
    assert re.search('discover .*--discovery-timeout-ms', run('--help').stdout)


def test_overall_discovery_deadline_monotonic_equality_expires_even_before_timer_delivery_once_only(mock_timers, monkeypatch):
    clock = {'now': 4000}
    monkeypatch.setattr(timers, 'now', lambda: clock['now'])
    expired = []
    deadline = DiscoveryDeadline(100, lambda: expired.append(1))
    clock['now'] += 99
    deadline.check()
    assert len(expired) == 0
    clock['now'] += 1
    deadline.check()
    assert len(expired) == 1
    mock_timers.tick(1000)
    deadline.check()
    assert len(expired) == 1


def test_overall_discovery_deadline_timer_delivery_disabling_bounds_and_explicit_cleanup(mock_timers):
    expired = []
    callback = lambda: expired.append(1)  # noqa: E731
    disabled = DiscoveryDeadline(None, callback)
    stopped = DiscoveryDeadline(1, callback)
    stopped.stop()
    DiscoveryDeadline(100, callback)
    mock_timers.tick(99)
    assert len(expired) == 0
    mock_timers.tick(1)
    assert len(expired) == 1
    mock_timers.tick(1000)
    disabled.check()
    assert len(expired) == 1
    assert validate_discovery_timeout(None) is None
    for ms in (1, 2147483647):
        assert validate_discovery_timeout(ms) == ms
    for ms in (0, -1, 1.5, math.nan, math.inf, '100', 2147483648, True):
        with pytest.raises(InputError, match='--discovery-timeout-ms'):
            validate_discovery_timeout(ms)


def test_overall_discovery_deadline_runtime_disarms_timer_and_signal_handlers_on_success_protocol_failure_and_interruption(monkeypatch):
    spy = TimerSpy(monkeypatch)
    signals = signal_handlers()

    def command(mode):
        return [PYTHON, fixture('discovery_server.py'), mode]

    assert re.match(r'^\{"tools":', asyncio.run(run_discover(command('normal'), {}, discovery_timeout_ms=5000)))
    with pytest.raises(DiscoveryError, match='pagination loop'):
        asyncio.run(run_discover(command('cycle'), {}, discovery_timeout_ms=5000))
    for name, signum in (('SIGTERM', signal.SIGTERM), ('SIGINT', signal.SIGINT)):
        async def interrupted():
            original = signal.getsignal(signum)
            task = asyncio.ensure_future(run_discover(server('many'), {}, discovery_timeout_ms=5000))
            while signal.getsignal(signum) is original and not task.done():
                await asyncio.sleep(0.005)
            os.kill(os.getpid(), signum)
            return await task

        with pytest.raises(DiscoveryError, match=f'discovery interrupted by {name}'):
            asyncio.run(interrupted())
    with pytest.raises(DiscoveryError, match='cannot start server'):
        asyncio.run(run_discover(['/does/not/exist'], {}, discovery_timeout_ms=5000))
    overall = spy.with_ms(5000)
    assert len(overall) == 5
    assert all(timer in spy.cleared for timer in overall)
    assert all(not timer.has_ref() for timer in overall)
    assert signal_handlers() == signals


def test_overall_discovery_deadline_overdue_continuations_cannot_send_another_message_or_return_a_snapshot(tmp_dir, monkeypatch):
    log = tmp_dir / 'requests.log'
    clock = {'now': 0, 'advance_on_ms': None}
    durations = {}
    monkeypatch.setattr(timers, 'now', lambda: clock['now'])

    def on_set(timer, ms):
        durations[timer] = ms

    def on_clear(timer):
        if timer is not None and durations.get(timer) == clock['advance_on_ms']:
            clock['now'] = 5000

    TimerSpy(monkeypatch, on_set=on_set, on_clear=on_clear)
    # Expire on initialize completion without delivering the overall timer.
    clock['advance_on_ms'] = 2000
    with pytest.raises(DiscoveryError, match='discovery duration limit exceeded'):
        asyncio.run(run_discover([*server('many'), str(log)], {}, discovery_timeout_ms=5000, request_timeout_ms=2000))
    assert log.read_text() == 'initialize\n'
    # Expire on clean shutdown completion, after the terminal page was buffered.
    clock['now'] = 0
    clock['advance_on_ms'] = 1000
    with pytest.raises(DiscoveryError, match='discovery duration limit exceeded'):
        asyncio.run(run_discover([PYTHON, fixture('discovery_server.py')], {}, discovery_timeout_ms=5000, request_timeout_ms=2000))


def test_overall_discovery_deadline_speculative_reply_cannot_bypass_budget_during_a_blocked_request_write():
    result = run('discover', '--discovery-timeout-ms', '500', '--request-timeout-ms', '5000',
                 '--', PYTHON, fixture('discovery_server.py'), 'backpressure')
    assert result.returncode == 1, result.stderr
    assert result.stdout == ''
    assert re.search('discovery duration limit exceeded', result.stderr)
