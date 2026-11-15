#!/usr/bin/env python3
"""Synthetic nonsecret environment/cwd probe; fail without printing values."""
import json
import os
import runpy
import sys


def fail(message):
    sys.stderr.write(f'{message}\n')
    sys.exit(1)


with open('env-marker.json', encoding='utf-8') as handle:
    marker = json.load(handle)
if os.getcwd() != marker['cwd']:
    fail('fixture cwd mismatch')
for name, value in marker['expected'].items():
    if os.environ.get(name) != value:
        fail('fixture environment mismatch')
for name in marker.get('absent') or []:
    if name in os.environ:
        fail('fixture environment key must be absent')
if len(sys.argv) > 2 and sys.argv[2]:
    trace = {'cwd': os.getcwd(), 'expected': marker['expected'], 'absent': marker.get('absent'),
             'path': os.environ.get('PATH'), 'literal': sys.argv[3] if len(sys.argv) > 3 else None}
    with open(sys.argv[2], 'w', encoding='utf-8') as handle:
        json.dump({key: value for key, value in trace.items() if value is not None}, handle, ensure_ascii=False)
# This fixture rejects every exchange except initialize, initialized and tools/list.
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'discovery_server.py'), run_name='__main__')
