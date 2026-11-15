"""Port of test/discovery-tools.test.js (--max-tools)."""

import asyncio
import json
import math
import os
import re

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.discover import DiscoveryError, collect_catalogue, run_discover
from agentwrench.slim.jsjson import stringify

from .conftest import PYTHON, SLIM_EXPECTED, TimerSpy, fixture, run, signal_handlers, spawn_marker


def tool(name):
    return {'name': name, 'inputSchema': {'type': 'object'}}


def server(mode):
    return [PYTHON, fixture('discovery_deadline_server.py'), mode]


def collect(*args, **kwargs):
    return asyncio.run(collect_catalogue(*args, **kwargs))


def test_discovery_source_tool_limit_hidden_definitions_aggregate_before_filtering_and_crossing_page_stops_the_walk():
    for max_tools in (1, 2, 3):
        for one_page in (False, True):
            reads = [tool(f'read_{i}') for i in range(max_tools)]
            pages = [[tool('hidden'), *reads]] if one_page else [[tool('hidden')], [], reads]
            cursors = []

            async def list_page(params):
                index = len(cursors)
                cursors.append((params or {}).get('cursor'))
                return {'tools': pages[index] if index < len(pages) else [],
                        'nextCursor': ['', 'tail🦊', 'end'][index] if index < 3 else str(index)}

            with pytest.raises(DiscoveryError, match=f'discovery catalogue exceeds {max_tools} source tools'):
                collect(list_page, {'allow': ['read*']}, max_tools=max_tools)
            assert cursors == ([None] if one_page else [None, '', 'tail🦊'])


def test_discovery_source_tool_limit_exact_cap_permits_later_empty_pages_through_terminal_and_keeps_source_order():
    for max_tools in (1, 2, 3, 10000):
        tools = [tool(f'read_{max_tools - i}') for i in range(max_tools)]
        split = math.ceil(max_tools / 2)
        pages = [[], tools[:split], tools[split:], [], []]
        calls = [0]

        async def list_page(params):
            assert (params or {}).get('cursor') == (str(calls[0]) if calls[0] else None)
            batch = pages[calls[0]]
            calls[0] += 1
            return {'tools': batch, **({'nextCursor': str(calls[0])} if calls[0] < len(pages) else {})}

        snapshot = collect(list_page, {}, max_tools=max_tools)
        assert calls[0] == len(pages)
        assert snapshot == f"{stringify({'tools': tools})}\n"

    async def empty(params):
        return {'tools': []}

    assert collect(empty, {}, max_tools=1) == '{"tools":[]}\n'


def test_discovery_source_tool_limit_global_hidden_duplicate_guard_remains_active_at_and_below_cap():
    for max_tools in (1, 2, 3):
        calls = [0]

        async def list_page(params):
            calls[0] += 1
            return {'tools': [tool('hidden')], **({'nextCursor': ''} if calls[0] == 1 else {})}

        with pytest.raises(DiscoveryError, match='duplicate tool names'):
            collect(list_page, {'allow': []}, max_tools=max_tools)
        assert calls[0] == 2


def test_discovery_source_tool_limit_cli_failures_preserve_stdout_and_existing_absent_files_without_another_request(tmp_dir):
    config, output = tmp_dir / 'slim.json', tmp_dir / 'catalogue.json'
    config.write_text('{"allow":[]}')
    direct_log = tmp_dir / 'direct.log'
    with pytest.raises(DiscoveryError, match='discovery catalogue exceeds 2 source tools'):
        asyncio.run(run_discover([*server('many'), str(direct_log)], {}, max_tools=2))
    expected_trace = 'initialize\nnotifications/initialized\ntools/list\ntools/list\ntools/list\n'
    assert direct_log.read_text() == expected_trace
    for destination in ('stdout', 'existing', 'absent'):
        log = tmp_dir / f'{destination}.log'
        if destination == 'existing':
            output.write_text('sentinel')
        if destination == 'absent' and output.exists():
            output.unlink()
        result = run('discover', '--max-tools', '2', '--max-pages', '10', '--discovery-timeout-ms', '5000',
                     '--request-timeout-ms', '2000', '--config', str(config), *([] if destination == 'stdout' else ['--output', str(output)]),
                     '--', *server('many'), str(log))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search('discovery catalogue exceeds 2 source tools', result.stderr)
        assert log.read_text() == expected_trace
        if destination == 'existing':
            assert output.read_text() == 'sentinel'
        if destination == 'absent':
            assert not output.exists()
        assert all(not name.startswith('.agentwrench-slim-') for name in os.listdir(tmp_dir))


def test_discovery_source_tool_limit_exact_finite_stdout_file_bytes_match_omission_with_transformations(tmp_dir):
    config, output = tmp_dir / 'slim.json', tmp_dir / 'catalogue.json'
    config.write_text(json.dumps({'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']}))
    command = ['--', PYTHON, fixture('discovery_server.py')]
    baseline = run('discover', '--config', str(config), *command)
    assert baseline.returncode == 0, baseline.stderr
    assert baseline.stdout == SLIM_EXPECTED
    for max_tools in ('5', '10000'):
        for file in (False, True):
            if file:
                output.write_text('sentinel')
            result = run('discover', '--max-tools', max_tools, '--max-pages', '3', '--discovery-timeout-ms', '5000',
                         '--request-timeout-ms', '1000', '--config', str(config), *(['--output', str(output)] if file else []), *command)
            assert result.returncode == 0, result.stderr
            assert result.stderr == ''
            assert result.stdout == ('' if file else SLIM_EXPECTED)
            if file:
                assert output.read_text(encoding='utf-8') == SLIM_EXPECTED


def test_discovery_source_tool_limit_malformed_or_disallowed_cli_values_fail_exit_2_before_spawn(tmp_dir):
    command = ['--', *spawn_marker(tmp_dir / 'spawned')]
    for values in [[], [''], ['0'], ['-1'], ['1.5'], ['1e3'], ['+1'], ['NaN'], ['Infinity'], ['10001'],
                   ['2147483647'], ['999999999999999999999'], ['1', '--max-tools', '2']]:
        result = run('discover', '--max-tools', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-tools: (missing value|duplicate option|expected an integer from 1 to 10000)', result.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--max-tools', '1', *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-tools: unsupported option', result.stderr)
    assert os.listdir(tmp_dir) == []
    assert re.search('discover .*--max-tools', run('--help').stdout)


def test_discovery_source_tool_limit_invalid_runtime_values_reject_before_list_invocation_or_spawn(tmp_dir):
    for max_tools in [0, -1, 1.5, math.nan, math.inf, '2', 10001, True, {}, []]:
        called = []

        async def list_page(params):
            called.append(True)
            return {'tools': []}

        with pytest.raises(InputError, match='--max-tools'):
            collect(list_page, {}, max_tools=max_tools)
        assert called == []
        with pytest.raises(InputError, match='--max-tools'):
            asyncio.run(run_discover(spawn_marker(tmp_dir / 'spawned'), {}, max_tools=max_tools))
    assert os.listdir(tmp_dir) == []


def test_discovery_source_tool_limit_empty_pages_still_hit_page_overall_bounds_and_request_deadline_stays_independent(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    for mode, pages, overall, request, diagnostic in [
        ('empty', '2', '5000', '2000', 'discovery exceeds 2 page limit'),
        ('empty', '100', '500', '2000', 'discovery duration limit exceeded'),
        ('initialize', '100', '5000', '100', 'discovery request timed out'),
    ]:
        output.write_text('sentinel')
        result = run('discover', '--max-tools', '1', '--max-pages', pages, '--discovery-timeout-ms', overall,
                     '--request-timeout-ms', request, '--output', str(output), '--', *server(mode))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search(diagnostic, result.stderr)
        assert output.read_text() == 'sentinel'
    calls = [0]

    async def list_page(params):
        calls[0] += 1
        return {'tools': [tool('read')] if calls[0] == 1 else [], 'nextCursor': str(calls[0])}

    with pytest.raises(DiscoveryError, match='discovery exceeds 3 page limit'):
        collect(list_page, {}, max_tools=1, max_pages=3)
    assert calls[0] == 3


def test_discovery_source_tool_limit_crossing_cap_clears_overall_timer_and_signal_handlers(monkeypatch):
    spy = TimerSpy(monkeypatch)
    signals = signal_handlers()
    with pytest.raises(DiscoveryError, match='discovery catalogue exceeds 1 source tools'):
        asyncio.run(run_discover(server('many'), {}, max_tools=1, discovery_timeout_ms=5000))
    overall = spy.with_ms(5000)
    assert len(overall) == 1
    assert overall[0] in spy.cleared
    assert overall[0].has_ref() is False
    assert signal_handlers() == signals
