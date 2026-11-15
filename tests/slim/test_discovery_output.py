"""Port of test/discovery-output.test.js (--max-output-bytes)."""

import asyncio
import math
import os
import re

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.discover import DiscoveryError, collect_catalogue, run_discover
from agentwrench.slim.jsjson import stringify

from .conftest import EXAMPLE_CONFIG, PYTHON, TimerSpy, fixture, run, signal_handlers, spawn_marker


def tool(name):
    return {'name': name, 'inputSchema': {'type': 'object'}}


def server(mode):
    return [PYTHON, fixture('discovery_deadline_server.py'), mode]


def utf8(text):
    return len(text.encode('utf-8'))


def collect(*args, **kwargs):
    return asyncio.run(collect_catalogue(*args, **kwargs))


def test_discovery_output_byte_limit_one_extra_byte_utf8_metadata_and_cross_page_commas_stop_before_next_page():
    for names in (['read_a', 'read_b'], ['read_🦊', 'read_é']):
        tools = [tool(name) for name in names]
        tools[0]['title'] = 'é🦊\n"'
        expected = f"{stringify({'tools': tools})}\n"
        size = utf8(expected)
        assert size > len(expected)  # Deliberately distinguish UTF-8 from code-point length.
        for one_page in (False, True):
            batches = [tools] if one_page else [[t] for t in tools]
            calls = [0]

            async def crossing(params):
                assert calls[0] < len(batches), 'must not request beyond crossing page'
                calls[0] += 1
                return {'tools': batches[calls[0] - 1], 'nextCursor': str(calls[0])}

            with pytest.raises(DiscoveryError, match='discovery catalogue exceeds .*output bytes'):
                collect(crossing, {}, max_output_bytes=size - 1)
            assert calls[0] == len(batches)
            calls[0] = 0

            async def exact(params):
                calls[0] += 1
                return {'tools': batches[calls[0] - 1], **({'nextCursor': str(calls[0])} if calls[0] < len(batches) else {})}

            assert collect(exact, {}, max_output_bytes=size) == expected
    only = tool('a')
    size = 13 + utf8(stringify(only))

    async def single(params):
        return {'tools': [only]}

    assert collect(single, {}, max_output_bytes=size) == f"{stringify({'tools': [only]})}\n"
    with pytest.raises(DiscoveryError, match='output bytes'):
        collect(single, {}, max_output_bytes=size - 1)


def test_discovery_output_byte_limit_transformed_exact_cap_permits_later_empty_and_hidden_terminal_pages():
    source = {'name': 'read_🦊', 'description': 'é🦊 extra', 'inputSchema': {'type': 'object', 'description': 'strip'}, 'annotations': {'readOnlyHint': True}}
    expected = '{"tools":[{"name":"read_🦊","description":"é🦊","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":true}}]}\n'
    pages = [[tool('hidden_start')], [source], [], [tool('hidden_tail')], []]
    cursors = []

    async def list_page(params):
        index = len(cursors)
        cursors.append((params or {}).get('cursor'))
        result = {'tools': pages[index], '_meta': {'uncounted': 'x' * 500}, 'resultType': 'complete', 'ttlMs': 1000}
        if index < len(pages) - 1:
            result['nextCursor'] = ['', 'tail🦊', 'empty', 'last'][index]
        return result

    snapshot = collect(list_page, {'allow': ['read*'], 'maxDescriptionChars': 2, 'stripSchemaAnnotations': ['description']},
                       max_output_bytes=utf8(expected), max_pages=5, max_tools=3)
    assert snapshot == expected
    assert cursors == [None, '', 'tail🦊', 'empty', 'last']


def test_discovery_output_byte_limit_13_byte_empty_floor_hidden_source_page_bounds_and_duplicate_guard():
    assert utf8('{"tools":[]}\n') == 13

    async def empty(params):
        return {'tools': []}

    assert collect(empty, {}, max_output_bytes=13) == '{"tools":[]}\n'
    calls = [0]

    async def hidden_terminal(params):
        calls[0] += 1
        return {'tools': [tool(f'hidden_{calls[0]}')], **({'nextCursor': str(calls[0])} if calls[0] < 3 else {})}

    assert collect(hidden_terminal, {'allow': []}, max_output_bytes=13) == '{"tools":[]}\n'
    assert calls[0] == 3
    calls[0] = 0

    async def hidden_endless(params):
        calls[0] += 1
        return {'tools': [tool(f'hidden_{calls[0]}')], 'nextCursor': str(calls[0])}

    with pytest.raises(DiscoveryError, match='exceeds 1 source tools'):
        collect(hidden_endless, {'allow': []}, max_output_bytes=13, max_tools=1)
    assert calls[0] == 2
    calls[0] = 0

    async def empty_endless(params):
        calls[0] += 1
        return {'tools': [], 'nextCursor': str(calls[0])}

    with pytest.raises(DiscoveryError, match='exceeds 2 page limit'):
        collect(empty_endless, {}, max_output_bytes=13, max_pages=2)
    assert calls[0] == 2
    calls[0] = 0

    async def duplicate(params):
        calls[0] += 1
        return {'tools': [tool('hidden')], 'nextCursor': str(calls[0])}

    with pytest.raises(DiscoveryError, match='duplicate tool names'):
        collect(duplicate, {'allow': []}, max_output_bytes=13)
    assert calls[0] == 2


def test_discovery_output_byte_limit_cli_crossing_page_preserves_stdout_and_existing_absent_files(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    max_output_bytes = utf8(f"{stringify({'tools': [tool('hidden_0')]})}\n")
    direct_log = tmp_dir / 'direct.log'
    with pytest.raises(DiscoveryError, match='output bytes'):
        asyncio.run(run_discover([*server('many'), str(direct_log)], {}, max_output_bytes=max_output_bytes))
    expected_trace = 'initialize\nnotifications/initialized\ntools/list\ntools/list\n'
    assert direct_log.read_text() == expected_trace
    for destination in ('stdout', 'existing', 'absent'):
        log = tmp_dir / f'{destination}.log'
        if destination == 'existing':
            output.write_text('sentinel')
        if destination == 'absent' and output.exists():
            output.unlink()
        result = run('discover', '--max-output-bytes', str(max_output_bytes), '--max-pages', '10', '--max-tools', '10',
                     '--discovery-timeout-ms', '5000', '--request-timeout-ms', '2000',
                     *([] if destination == 'stdout' else ['--output', str(output)]), '--', *server('many'), str(log))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search('discovery catalogue exceeds .*output bytes', result.stderr)
        assert log.read_text() == expected_trace
        if destination == 'existing':
            assert output.read_text() == 'sentinel'
        if destination == 'absent':
            assert not output.exists()
        assert all(not name.startswith('.agentwrench-slim-') for name in os.listdir(tmp_dir))


def test_discovery_output_byte_limit_exact_finite_stdout_file_bytes_match_omission_and_hard_maximum(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    command = ['--', PYTHON, fixture('discovery_server.py')]
    expected = f"{stringify({'tools': [{'name': name, 'description': 'Read. Extra instructions.', 'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'integer'}}}, 'annotations': {'readOnlyHint': True}} for name in ['read_z', 'read_a', 'read_tail']]})}\n"
    assert utf8(expected) == 516
    baseline = run('discover', '--config', str(EXAMPLE_CONFIG), *command)
    assert baseline.returncode == 0, baseline.stderr
    assert baseline.stdout == expected
    for max_output_bytes in ('516', '16777216'):
        for file in (False, True):
            if file:
                output.write_text('sentinel')
            result = run('discover', '--max-output-bytes', max_output_bytes, '--max-tools', '5', '--max-pages', '3',
                         '--discovery-timeout-ms', '5000', '--request-timeout-ms', '1000', '--config', str(EXAMPLE_CONFIG),
                         *(['--output', str(output)] if file else []), *command)
            assert result.returncode == 0, result.stderr
            assert result.stderr == ''
            assert result.stdout == ('' if file else expected)
            if file:
                assert output.read_text(encoding='utf-8') == expected


def test_discovery_output_byte_limit_malformed_or_disallowed_cli_values_fail_exit_2_before_spawn(tmp_dir):
    command = ['--', *spawn_marker(tmp_dir / 'spawned')]
    for values in [[], [''], ['0'], ['12'], ['-1'], ['13.5'], ['1e3'], ['+13'], [' 13'], ['13 '], ['NaN'], ['Infinity'],
                   ['16777217'], ['999999999999999999999'], ['13', '--max-output-bytes', '14']]:
        result = run('discover', '--max-output-bytes', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-output-bytes: (missing value|duplicate option|expected an integer from 13 to 16777216)', result.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--max-output-bytes', '13', *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-output-bytes: unsupported option', result.stderr)
    assert os.listdir(tmp_dir) == []
    assert re.search('discover .*--max-output-bytes', run('--help').stdout)


def test_discovery_output_byte_limit_invalid_runtime_values_reject_before_list_invocation_or_spawn(tmp_dir):
    for max_output_bytes in [0, 12, -1, 13.5, math.nan, math.inf, '13', 16777217, True, {}, []]:
        called = []

        async def list_page(params):
            called.append(True)
            return {'tools': []}

        with pytest.raises(InputError, match='--max-output-bytes'):
            collect(list_page, {}, max_output_bytes=max_output_bytes)
        assert called == []
        with pytest.raises(InputError, match='--max-output-bytes'):
            asyncio.run(run_discover(spawn_marker(tmp_dir / 'spawned'), {}, max_output_bytes=max_output_bytes))
    assert os.listdir(tmp_dir) == []


def test_discovery_output_byte_limit_empty_floor_leaves_both_deadlines_independent(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    for mode, overall, request, diagnostic in [('empty', '500', '2000', 'discovery duration limit exceeded'),
                                                ('initialize', '5000', '100', 'discovery request timed out')]:
        output.write_text('sentinel')
        result = run('discover', '--max-output-bytes', '13', '--discovery-timeout-ms', overall,
                     '--request-timeout-ms', request, '--output', str(output), '--', *server(mode))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search(diagnostic, result.stderr)
        assert output.read_text() == 'sentinel'


def test_discovery_output_byte_limit_crossing_cap_clears_overall_timer_and_signal_handlers(monkeypatch):
    spy = TimerSpy(monkeypatch)
    signals = signal_handlers()
    with pytest.raises(DiscoveryError, match='output bytes'):
        asyncio.run(run_discover(server('many'), {}, max_output_bytes=13, discovery_timeout_ms=5000))
    overall = spy.with_ms(5000)
    assert len(overall) == 1
    assert overall[0] in spy.cleared
    assert overall[0].has_ref() is False
    assert signal_handlers() == signals
