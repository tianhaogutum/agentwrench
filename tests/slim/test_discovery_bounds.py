"""Port of test/discovery-bounds.test.js."""

import asyncio
import json

import pytest

from agentwrench.slim.discover import DiscoveryError, collect_catalogue
from agentwrench.slim.jsjson import stringify
from agentwrench.slim.jsonio import MAX_FILE_BYTES


def tool(name):
    return {'name': name, 'inputSchema': {'type': 'object'}}


def collect(*args, **kwargs):
    return asyncio.run(collect_catalogue(*args, **kwargs))


def test_discovery_aggregate_tool_bound_counts_hidden_tools_across_pages():
    first = [tool(f'hidden_{i}') for i in range(5000)]
    second = [tool(f'hidden_{5000 + i}') for i in range(5000)]
    page = [0]

    async def list_page(params):
        page[0] += 1
        return {'tools': first, 'nextCursor': 'next'} if page[0] == 1 else {'tools': second}

    assert json.loads(collect(list_page, {'allow': []})) == {'tools': []}
    second.append(tool('hidden_10000'))
    page[0] = 0
    with pytest.raises(DiscoveryError, match='10000 source tools'):
        collect(list_page, {'allow': []})


def test_discovery_aggregate_file_size_bound_accepts_16_mib_exactly_and_rejects_one_extra_byte():
    tools = [tool(name) for name in 'abc']
    tools[0]['extra'] = 'x' * (6 * 1024 * 1024)
    tools[1]['extra'] = 'x' * (6 * 1024 * 1024)
    tools[2]['extra'] = ''
    tools[2]['extra'] = 'x' * (MAX_FILE_BYTES - len(f"{stringify({'tools': tools})}\n".encode()))
    page = [0]

    async def list_page(params):
        index = page[0]
        page[0] += 1
        return {'tools': [tools[index]], **({'nextCursor': str(page[0])} if page[0] < len(tools) else {})}

    result = collect(list_page, {})
    assert len(result.encode()) == MAX_FILE_BYTES
    assert len(json.loads(result)['tools']) == 3
    tools[2]['extra'] += 'x'
    page[0] = 0
    with pytest.raises(DiscoveryError, match='16 MiB'):
        collect(list_page, {})


def test_discovery_terminates_a_non_repeating_empty_page_walk_at_the_page_bound():
    calls = [0]

    async def endless(params):
        calls[0] += 1
        return {'tools': [], 'nextCursor': str(calls[0])}

    with pytest.raises(DiscoveryError, match='10000 page limit'):
        collect(endless, {})
    assert calls[0] == 10000
    calls[0] = 0

    async def terminal(params):
        calls[0] += 1
        return {'tools': [], **({'nextCursor': str(calls[0])} if calls[0] < 10000 else {})}

    assert json.loads(collect(terminal, {})) == {'tools': []}
    assert calls[0] == 10000
