"""Port of test/discovery-wire.test.js (--max-wire-bytes)."""

import asyncio
import json
import math
import os
import re

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.discover import DiscoveryError, run_discover

from .conftest import PYTHON, TimerSpy, fixture, load_fixture_module, run, signal_handlers, spawn_marker

wire_scenario = load_fixture_module('discovery_wire_server').wire_scenario
CONFIG = {'allow': ['read*'], 'maxDescriptionChars': 2, 'stripSchemaAnnotations': ['description']}
EXPECTED = '{"tools":[{"name":"read_🦊","description":"é🦊","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":true}}]}\n'
DIAGNOSTIC = r'discovery exceeds \d+ incoming wire bytes'


def server(mode='normal', trace=None):
    return [PYTHON, fixture('discovery_wire_server.py'), mode, *([str(trace)] if trace else [])]


def discover(*args, **kwargs):
    return asyncio.run(run_discover(*args, **kwargs))


def utf8(text):
    return len(text.encode('utf-8'))


def test_discovery_wire_budget_exact_raw_total_matches_omission_including_split_utf8_and_shutdown_notifications():
    size = wire_scenario()['bytes']
    assert size > utf8(EXPECTED)
    assert discover(server(), CONFIG) == EXPECTED
    for mode in ('normal', 'split'):
        assert discover(server(mode), CONFIG, max_wire_bytes=size) == EXPECTED
        with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
            discover(server(mode), CONFIG, max_wire_bytes=size - 1)
    assert discover(server(), {'allow': []}, max_wire_bytes=size, max_output_bytes=13) == '{"tools":[]}\n'


def test_discovery_wire_budget_one_extra_byte_after_terminal_response_rejects_through_clean_shutdown():
    with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
        discover(server('extra'), CONFIG, max_wire_bytes=wire_scenario()['bytes'])


def test_discovery_wire_budget_overflow_within_a_split_multibyte_sequence_stops_before_page_reply(tmp_dir):
    trace = tmp_dir / 'split.log'
    scenario = wire_scenario()
    cap = len(scenario['init']) + scenario['pages'][0].index('🦊'.encode()) + 1
    with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
        discover(server('split', trace), {}, max_wire_bytes=cap)
    assert trace.read_text() == 'initialize\nnotifications/initialized\ntools/list\n'


def test_discovery_wire_budget_hidden_page_and_ignored_notification_overflow_prevents_next_page(tmp_dir):
    trace = tmp_dir / 'wire.log'
    scenario = wire_scenario()
    with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
        discover(server('normal', trace), {'allow': []}, max_wire_bytes=len(scenario['init']) + len(scenario['pages'][0]) - 1)
    assert trace.read_text() == 'initialize\nnotifications/initialized\ntools/list\n'


def test_discovery_wire_budget_partial_chunk_overflow_fails_before_newline_or_request_deadline(tmp_dir):
    trace = tmp_dir / 'partial.log'
    with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
        discover(server('partial', trace), {}, max_wire_bytes=len(wire_scenario()['init']) + 64,
                 request_timeout_ms=2000, discovery_timeout_ms=5000)
    assert trace.read_text() == 'initialize\nnotifications/initialized\ntools/list\npartial-without-newline\n'


def test_discovery_wire_budget_initialize_bytes_can_overflow_before_initialized_or_list_requests(tmp_dir):
    trace = tmp_dir / 'init.log'
    with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
        discover(server('normal', trace), {}, max_wire_bytes=1)
    assert trace.read_text() == 'initialize\n'


def test_discovery_wire_budget_exact_cli_stdout_file_output_and_overflow_preservation(tmp_dir):
    output, config_path = tmp_dir / 'catalogue.json', tmp_dir / 'config.json'
    config_path.write_text(json.dumps(CONFIG))
    size = wire_scenario()['bytes']
    for file in (False, True):
        result = run('discover', '--max-wire-bytes', str(size), '--max-output-bytes', str(utf8(EXPECTED)),
                     '--max-pages', '3', '--max-tools', '2', '--discovery-timeout-ms', '5000', '--request-timeout-ms', '2000',
                     '--config', str(config_path), *(['--output', str(output)] if file else []), '--', *server())
        assert result.returncode == 0, result.stderr
        assert result.stdout == ('' if file else EXPECTED)
        if file:
            assert output.read_text(encoding='utf-8') == EXPECTED
    for mode in ('normal', 'extra'):
        for destination in ('stdout', 'existing', 'absent'):
            if destination == 'existing':
                output.write_text('sentinel')
            if destination == 'absent' and output.exists():
                output.unlink()
            result = run('discover', '--max-wire-bytes', str(size - (1 if mode == 'normal' else 0)),
                         '--config', str(config_path), *([] if destination == 'stdout' else ['--output', str(output)]), '--', *server(mode))
            assert result.returncode == 1, result.stderr
            assert result.stdout == ''
            assert re.search(DIAGNOSTIC, result.stderr)
            if destination == 'existing':
                assert output.read_text() == 'sentinel'
            if destination == 'absent':
                assert not output.exists()
            assert all(not name.startswith('.agentwrench-slim-') for name in os.listdir(tmp_dir))


def test_discovery_wire_budget_inherited_stderr_and_outgoing_requests_excluded_maximum_accepted():
    size = wire_scenario('empty')['bytes']
    for limit in (size, 2147483647):
        result = run('discover', '--max-wire-bytes', str(limit), '--', *server('empty'))
        assert result.returncode == 0, result.stderr
        assert result.stdout == '{"tools":[]}\n'
        assert utf8(result.stderr) > size


def test_discovery_wire_budget_invalid_cli_values_are_discover_only_and_reject_before_spawn(tmp_dir):
    command = ['--', *spawn_marker(tmp_dir / 'spawned')]
    for values in [[], [''], ['0'], ['-1'], ['1.5'], ['1e3'], ['+1'], [' 1'], ['1 '], ['NaN'], ['Infinity'],
                   ['2147483648'], ['999999999999999999999'], ['1', '--max-wire-bytes', '2']]:
        result = run('discover', '--max-wire-bytes', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-wire-bytes: (missing value|duplicate option|expected an integer from 1 to 2147483647)', result.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--max-wire-bytes', '1', *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-wire-bytes: unsupported option', result.stderr)
    assert os.listdir(tmp_dir) == []
    assert re.search('discover .*--max-wire-bytes', run('--help').stdout)


def test_discovery_wire_budget_invalid_runtime_values_reject_before_spawn(tmp_dir):
    for max_wire_bytes in (0, -1, 1.5, math.nan, math.inf, '1', 2147483648, True, {}, []):
        with pytest.raises(InputError, match='--max-wire-bytes'):
            discover(spawn_marker(tmp_dir / 'spawned'), {}, max_wire_bytes=max_wire_bytes)
    assert os.listdir(tmp_dir) == []


def test_discovery_wire_budget_omission_and_configured_budget_retain_the_8_mib_per_line_hard_bound():
    for max_wire_bytes in (None, 2147483647):
        with pytest.raises(DiscoveryError, match='stdio line exceeds 8 MiB'):
            discover(server('line-hard'), {}, max_wire_bytes=max_wire_bytes)


def test_discovery_wire_budget_output_source_tool_page_and_both_deadline_bounds_remain_independent():
    max_wire_bytes = 2147483647
    for options, error in [({'max_output_bytes': 13}, 'output bytes'), ({'max_tools': 1}, 'exceeds 1 source tools'),
                           ({'max_pages': 1}, 'exceeds 1 page limit')]:
        with pytest.raises(DiscoveryError, match=error):
            discover(server(), CONFIG, max_wire_bytes=max_wire_bytes, **options)

    def command(mode):
        return [PYTHON, fixture('discovery_deadline_server.py'), mode]

    with pytest.raises(DiscoveryError, match='request timed out'):
        discover(command('initialize'), {}, max_wire_bytes=max_wire_bytes, request_timeout_ms=100, discovery_timeout_ms=5000)
    with pytest.raises(DiscoveryError, match='duration limit exceeded'):
        discover(command('empty'), {}, max_wire_bytes=max_wire_bytes, request_timeout_ms=2000, discovery_timeout_ms=300)


def test_discovery_wire_budget_overflow_bounds_sigterm_ignoring_cleanup_and_clears_timer_signal_handlers(monkeypatch):
    spy = TimerSpy(monkeypatch)
    signals = signal_handlers()
    with pytest.raises(DiscoveryError, match=DIAGNOSTIC):
        discover(server('stubborn'), {}, max_wire_bytes=len(wire_scenario()['init']) + 64, request_timeout_ms=2000, discovery_timeout_ms=5000)
    overall = spy.with_ms(5000)
    assert len(overall) == 1
    assert overall[0] in spy.cleared
    assert overall[0].has_ref() is False
    assert signal_handlers() == signals
