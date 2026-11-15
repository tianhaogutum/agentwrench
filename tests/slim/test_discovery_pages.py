"""Port of test/discovery-pages.test.js (--max-pages)."""

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


def server(mode):
    return [PYTHON, fixture('discovery_deadline_server.py'), mode]


def collect(*args, **kwargs):
    return asyncio.run(collect_catalogue(*args, **kwargs))


def test_discovery_page_limit_count_hidden_and_empty_pages_and_never_request_a_continuation_beyond_the_bound():
    for empty in (False, True):
        for max_pages in (1, 2, 3):
            cursors = []

            async def list_page(params):
                cursors.append((params or {}).get('cursor'))
                result = {'tools': [] if empty else [{'name': f'hidden_{len(cursors)}', 'inputSchema': {}}]}
                if len(cursors) < 4:
                    result['nextCursor'] = ['', 'tail🦊', 'end'][len(cursors) - 1]
                return result

            with pytest.raises(DiscoveryError, match=f'discovery exceeds {max_pages} page limit'):
                collect(list_page, {'allow': []}, max_pages=max_pages)
            assert cursors == [None, '', 'tail🦊'][:max_pages]


def test_discovery_page_limit_terminal_page_at_exactly_the_selected_bound_succeeds_with_unchanged_ordering():
    for max_pages in (1, 2, 3, 10000):
        calls = [0]

        async def list_page(params):
            assert (params or {}).get('cursor') == (str(calls[0]) if calls[0] else None)
            calls[0] += 1
            return {'tools': [{'name': f'read_{calls[0]}', 'inputSchema': {'type': 'object'}}],
                    **({'nextCursor': str(calls[0])} if calls[0] < max_pages else {})}

        snapshot = collect(list_page, {}, max_pages=max_pages)
        assert calls[0] == max_pages
        assert snapshot == f"{stringify({'tools': [{'name': f'read_{i + 1}', 'inputSchema': {'type': 'object'}} for i in range(max_pages)]})}\n"


def test_discovery_page_limit_runtime_failure_and_cli_stdout_file_failures_preserve_destinations_and_stop_at_two_pages(tmp_dir):
    config, output = tmp_dir / 'slim.json', tmp_dir / 'catalogue.json'
    config.write_text('{"allow":[]}')
    direct_log = tmp_dir / 'direct.log'
    with pytest.raises(DiscoveryError, match='discovery exceeds 2 page limit'):
        asyncio.run(run_discover([*server('many'), str(direct_log)], {}, max_pages=2))
    assert direct_log.read_text() == 'initialize\nnotifications/initialized\ntools/list\ntools/list\n'
    for mode in ('many', 'empty'):
        for destination in ('stdout', 'existing', 'absent'):
            log = tmp_dir / f'{mode}-{destination}.log'
            if destination == 'existing':
                output.write_text('sentinel')
            if destination == 'absent' and output.exists():
                output.unlink()
            result = run('discover', '--max-pages', '2', '--discovery-timeout-ms', '5000', '--request-timeout-ms', '2000',
                         '--config', str(config), *([] if destination == 'stdout' else ['--output', str(output)]), '--', *server(mode), str(log))
            assert result.returncode == 1, result.stderr
            assert result.stdout == ''
            assert re.search('discovery exceeds 2 page limit', result.stderr)
            assert log.read_text() == 'initialize\nnotifications/initialized\ntools/list\ntools/list\n'
            if destination == 'existing':
                assert output.read_text() == 'sentinel'
            if destination == 'absent':
                assert not output.exists()
            assert all(not name.startswith('.agentwrench-slim-') for name in os.listdir(tmp_dir))


def test_discovery_page_limit_finite_transformed_stdout_and_atomic_file_bytes_match_the_omitted_limit(tmp_dir):
    config, output = tmp_dir / 'slim.json', tmp_dir / 'catalogue.json'
    config.write_text(json.dumps({'allow': ['read*'], 'deny': ['*_admin'], 'maxDescriptionChars': 5, 'stripSchemaAnnotations': ['description']}))
    command = ['--', PYTHON, fixture('discovery_server.py')]
    baseline = run('discover', '--config', str(config), *command)
    assert baseline.returncode == 0, baseline.stderr
    assert baseline.stdout == SLIM_EXPECTED
    for max_pages in ('3', '10000'):
        for file in (False, True):
            if file:
                output.write_text('sentinel')
            result = run('discover', '--max-pages', max_pages, '--discovery-timeout-ms', '5000', '--request-timeout-ms', '1000',
                         '--config', str(config), *(['--output', str(output)] if file else []), *command)
            assert result.returncode == 0, result.stderr
            assert result.stderr == ''
            assert result.stdout == ('' if file else SLIM_EXPECTED)
            if file:
                assert output.read_text(encoding='utf-8') == SLIM_EXPECTED


def test_discovery_page_limit_malformed_or_disallowed_cli_values_fail_exit_2_before_spawn(tmp_dir):
    command = ['--', *spawn_marker(tmp_dir / 'spawned')]
    for values in [[], [''], ['0'], ['-1'], ['1.5'], ['1e3'], ['+1'], ['NaN'], ['Infinity'], ['10001'],
                   ['2147483647'], ['999999999999999999999'], ['1', '--max-pages', '2']]:
        result = run('discover', '--max-pages', *values, *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-pages: (missing value|duplicate option|expected an integer from 1 to 10000)', result.stderr)
    for mode in ('proxy', 'measure', 'plan'):
        result = run(mode, '--max-pages', '1', *command)
        assert result.returncode == 2, result.stderr
        assert result.stdout == ''
        assert re.search('--max-pages: unsupported option', result.stderr)
    assert os.listdir(tmp_dir) == []
    assert re.search('discover .*--max-pages', run('--help').stdout)


def test_discovery_page_limit_invalid_runtime_values_reject_before_list_invocation_or_spawn(tmp_dir):
    for max_pages in [0, -1, 1.5, math.nan, math.inf, '2', 10001, True]:
        called = []

        async def list_page(params):
            called.append(True)
            return {'tools': []}

        with pytest.raises(InputError, match='--max-pages'):
            collect(list_page, {}, max_pages=max_pages)
        assert called == []
        with pytest.raises(InputError, match='--max-pages'):
            asyncio.run(run_discover(spawn_marker(tmp_dir / 'spawned'), {}, max_pages=max_pages))
    assert os.listdir(tmp_dir) == []


def test_discovery_page_limit_existing_overall_and_per_request_deadlines_can_still_fail_before_the_page_cap(tmp_dir):
    output = tmp_dir / 'catalogue.json'
    for mode, overall, request, diagnostic in [('many', '500', '2000', 'discovery duration limit exceeded'),
                                                ('initialize', '5000', '100', 'discovery request timed out')]:
        output.write_text('sentinel')
        result = run('discover', '--max-pages', '100', '--discovery-timeout-ms', overall,
                     '--request-timeout-ms', request, '--output', str(output), '--', *server(mode))
        assert result.returncode == 1, result.stderr
        assert result.stdout == ''
        assert re.search(diagnostic, result.stderr)
        assert output.read_text() == 'sentinel'
    assert os.listdir(tmp_dir) == ['catalogue.json']


def test_discovery_page_limit_hitting_the_cap_clears_overall_timer_and_signal_handlers(monkeypatch):
    spy = TimerSpy(monkeypatch)
    signals = signal_handlers()
    with pytest.raises(DiscoveryError, match='discovery exceeds 1 page limit'):
        asyncio.run(run_discover(server('many'), {}, max_pages=1, discovery_timeout_ms=5000))
    overall = spy.with_ms(5000)
    assert len(overall) == 1
    assert overall[0] in spy.cleared
    assert overall[0].has_ref() is False
    assert signal_handlers() == signals
