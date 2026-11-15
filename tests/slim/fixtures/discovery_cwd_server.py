#!/usr/bin/env python3
"""Synthetic local cwd/relative-file probe, followed by the paginated fixture."""
import json
import os
import runpy
import sys

with open('cwd-marker.json', encoding='utf-8') as handle:
    marker = json.load(handle)
assert os.getcwd() == marker['cwd'], 'fixture cwd mismatch'
if 'env' in marker:
    assert os.environ.get('MCP_SLIM_CWD_MARKER') == marker['env'], 'fixture environment mismatch'
if len(sys.argv) > 2 and sys.argv[2]:
    trace = {'cwd': os.getcwd(), 'marker': marker, 'env': os.environ.get('MCP_SLIM_CWD_MARKER'),
             'path': os.environ.get('PATH'), 'literal': sys.argv[3] if len(sys.argv) > 3 else None}
    with open(sys.argv[2], 'w', encoding='utf-8') as handle:
        json.dump({key: value for key, value in trace.items() if value is not None}, handle, ensure_ascii=False)
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'discovery_server.py'), run_name='__main__')
