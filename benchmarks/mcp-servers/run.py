#!/usr/bin/env python3
"""Reproduce benchmarks/mcp-servers/results.json from scratch.

Run from the repository root:

    uv run --with tiktoken==0.14.0 python benchmarks/mcp-servers/run.py --workdir /some/scratch/dir

What it does, for every server in SERVERS:

1. installs the pinned package into <workdir>/servers/<id> (npm with --ignore-scripts,
   a uv virtualenv, or a checksum-verified GitHub release binary);
2. runs `agentwrench slim discover` against it with a minimal environment
   (temporary HOME, PATH limited to node/npm/uv and /usr/bin:/bin, dummy tokens
   only) and saves the catalogue to <workdir>/catalogues/<id>.json;
3. runs `agentwrench slim measure` (before), `measure --config` for a
   description-trim-only config, a strip-annotations-only config and the
   no-descriptions floor, and `plan --budget` + `measure --config` for each
   budget in BUDGETS;
4. counts the same JSON text with tiktoken (o200k_base and cl100k_base) to check
   the ceil(bytes/4) estimate against a real BPE tokenizer.

Third-party catalogues stay in the workdir; only aggregate numbers are written to
results.json. No credentials are read or needed; nothing calls a paid API.
Network access is needed for npm, PyPI, GitHub releases and the tiktoken BPE files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# Pinned versions (looked up 2026-11-15).
SERVERS = [
    {'id': 'filesystem', 'name': 'Filesystem (MCP reference)', 'kind': 'npm',
     'package': '@modelcontextprotocol/server-filesystem', 'version': '2.3.0',
     'bin': 'mcp-server-filesystem', 'args': ['{sandbox}']},
    {'id': 'memory', 'name': 'Memory (MCP reference)', 'kind': 'npm',
     'package': '@modelcontextprotocol/server-memory', 'version': '2.1.1',
     'bin': 'mcp-server-memory', 'args': [], 'env': {'MEMORY_FILE_PATH': '{sandbox}/memory.jsonl'}},
    {'id': 'sequential-thinking', 'name': 'Sequential Thinking (MCP reference)', 'kind': 'npm',
     'package': '@modelcontextprotocol/server-sequential-thinking', 'version': '2.1.1',
     'bin': 'mcp-server-sequential-thinking', 'args': []},
    {'id': 'everything', 'name': 'Everything (MCP reference test server)', 'kind': 'npm',
     'package': '@modelcontextprotocol/server-everything', 'version': '2.3.0',
     'bin': 'mcp-server-everything', 'args': ['stdio']},
    {'id': 'fetch', 'name': 'Fetch (MCP reference, Python)', 'kind': 'pypi',
     'package': 'mcp-server-fetch', 'version': '2026.11.11', 'bin': 'mcp-server-fetch', 'args': []},
    {'id': 'git', 'name': 'Git (MCP reference, Python)', 'kind': 'pypi',
     'package': 'mcp-server-git', 'version': '2026.11.11', 'bin': 'mcp-server-git', 'args': []},
    {'id': 'context7', 'name': 'Context7 (Upstash)', 'kind': 'npm',
     'package': '@upstash/context7-mcp', 'version': '4.7.0', 'bin': 'context7-mcp', 'args': []},
    {'id': 'playwright', 'name': 'Playwright MCP (Microsoft)', 'kind': 'npm',
     'package': '@playwright/mcp', 'version': '0.0.85', 'bin': 'playwright-mcp', 'args': []},
    {'id': 'chrome-devtools', 'name': 'Chrome DevTools MCP (Google)', 'kind': 'npm',
     'package': 'chrome-devtools-mcp', 'version': '1.13.0', 'bin': 'chrome-devtools-mcp',
     'args': ['--no-usage-statistics']},
    {'id': 'notion', 'name': 'Notion MCP (Notion)', 'kind': 'npm',
     'package': '@notionhq/notion-mcp-server', 'version': '2.6.1', 'bin': 'notion-mcp-server', 'args': [],
     'env': {'NOTION_TOKEN': 'dummy-not-a-real-token'}},
    {'id': 'github', 'name': 'GitHub MCP (GitHub, default toolsets)', 'kind': 'github-release',
     'package': 'github/github-mcp-server', 'version': '2.1.0', 'bin': 'github-mcp-server', 'args': ['stdio'],
     'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'dummy-not-a-real-token'}},
    {'id': 'github-all', 'name': 'GitHub MCP (GitHub, --toolsets all)', 'kind': 'github-release',
     'package': 'github/github-mcp-server', 'version': '2.1.0', 'bin': 'github-mcp-server',
     'args': ['stdio', '--toolsets', 'all'], 'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'dummy-not-a-real-token'},
     'install_as': 'github'},
]

GITHUB_SHA256 = {  # from github-mcp-server_2.1.0_checksums.txt
    'Linux_x86_64': 'a442f9db287cea01008bcb55fe6bc0292270301046c3edbd1a8b22d6e4157b2b',
    'Linux_arm64': '34d5a268f65cd1e9b7107fa976f3571ccd575f9df00cec489fec79a039923a22',
    'Darwin_x86_64': '3c4b023b0d90bdc8f3a2ab63307ff55d293e98c27e733008eed0fa35aca55499',
    'Darwin_arm64': '3fb2936c33416693a8b268442ab9b1e838d4fbb8c2a6813d8a77f87c3009ced6',
}

# "Typical stack" used for the headline total.
STACK = ['filesystem', 'github', 'playwright', 'context7']

# Description-trim-only config: one-sentence tool descriptions, schemas untouched.
TRIM_ONLY = {'maxDescriptionChars': 160}
# Strip-only config: what `plan` does first (schema annotations, keeps `default`).
STRIP_ONLY = {'stripSchemaAnnotations': ['description', 'title', 'examples', '$comment']}
# Lower bound without hiding any tool: strip annotations and drop every tool
# description. This is what `plan` writes when a budget cannot be met.
FLOOR = {**STRIP_ONLY, 'maxDescriptionChars': 0}
# Plan budget as a fraction of each server's own "before" estimate, so one
# setting is comparable across servers of very different sizes.
BUDGETS = {'plan50': 0.50}

TIKTOKEN_ENCODINGS = ['o200k_base', 'cl100k_base']


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def minimal_env(work: Path) -> dict[str, str]:
    path = []
    for tool in ('node', 'npm', 'uv'):
        found = shutil.which(tool)
        if not found:
            raise SystemExit(f'{tool} not found on PATH')
        directory = str(Path(found).parent)
        if directory not in path:
            path.append(directory)
    path += ['/usr/bin', '/bin']
    home = work / 'home'
    home.mkdir(parents=True, exist_ok=True)
    return {
        'PATH': os.pathsep.join(path), 'HOME': str(home), 'LANG': 'C.UTF-8', 'TMPDIR': str(work / 'tmp'),
        'npm_config_cache': str(work / 'cache' / 'npm'), 'npm_config_registry': 'https://registry.npmjs.org/',
        'npm_config_update_notifier': 'false', 'npm_config_fund': 'false', 'npm_config_audit': 'false',
        'UV_CACHE_DIR': str(work / 'cache' / 'uv'), 'UV_NO_CONFIG': '1', 'DO_NOT_TRACK': '1',
    }


def run(cmd: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, check=True, text=True, **kwargs)


def install(server: dict, work: Path, env: dict[str, str]) -> Path:
    """Install the pinned server; return the absolute path of its executable."""
    target = work / 'servers' / server.get('install_as', server['id'])
    kind = server['kind']
    if kind == 'npm':
        exe = target / 'node_modules' / '.bin' / server['bin']
        if not exe.exists():
            target.mkdir(parents=True, exist_ok=True)
            log(f"npm install {server['package']}@{server['version']}")
            run(['npm', 'install', '--prefix', str(target), '--ignore-scripts', '--no-save', '--loglevel=error',
                 f"{server['package']}@{server['version']}"], env, stdout=subprocess.DEVNULL)
        return exe
    if kind == 'pypi':
        exe = target / 'bin' / server['bin']
        if not exe.exists():
            log(f"uv pip install {server['package']}=={server['version']}")
            run(['uv', 'venv', '--quiet', str(target)], env)
            run(['uv', 'pip', 'install', '--quiet', '--python', str(target / 'bin' / 'python'),
                 f"{server['package']}=={server['version']}"], env)
        return exe
    if kind == 'github-release':
        exe = target / server['bin']
        if not exe.exists():
            system = {'Linux': 'Linux', 'Darwin': 'Darwin'}[platform.system()]
            machine = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'arm64': 'arm64', 'aarch64': 'arm64'}[platform.machine().lower()]
            asset = f'github-mcp-server_{system}_{machine}.tar.gz'
            url = f"https://github.com/{server['package']}/releases/download/v{server['version']}/{asset}"
            log(f'download {url}')
            data = urllib.request.urlopen(url, timeout=120).read()  # noqa: S310 - pinned https URL
            digest = hashlib.sha256(data).hexdigest()
            if digest != GITHUB_SHA256[f'{system}_{machine}']:
                raise SystemExit(f'checksum mismatch for {asset}: {digest}')
            target.mkdir(parents=True, exist_ok=True)
            archive = target / asset
            archive.write_bytes(data)
            with tarfile.open(archive) as tar:
                tar.extract(server['bin'], target, filter='data')
            exe.chmod(0o755)
        return exe
    raise ValueError(kind)


def aw(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    """Run the agentwrench CLI from this checkout (the interpreter `uv run` gave us)."""
    return run([sys.executable, '-m', 'agentwrench', *args], env, capture_output=True, **kwargs)


def substitute(value: str, sandbox: Path) -> str:
    return value.replace('{sandbox}', str(sandbox))


def discover(server: dict, exe: Path, work: Path, env: dict[str, str]) -> tuple[Path, list[str]]:
    sandbox = work / 'sandbox' / server['id']
    sandbox.mkdir(parents=True, exist_ok=True)
    catalogue = work / 'catalogues' / f"{server['id']}.json"
    catalogue.parent.mkdir(parents=True, exist_ok=True)
    env_args: list[str] = []
    for name, value in server.get('env', {}).items():
        env_args += ['--server-env', f'{name}={substitute(value, sandbox)}']
    server_cmd = [str(exe), *(substitute(a, sandbox) for a in server['args'])]
    args = ['slim', 'discover', '--output', str(catalogue), '--server-cwd', str(sandbox),
            '--discovery-timeout-ms', '120000', *env_args, '--', *server_cmd]
    log(f"discover {server['id']}")
    aw(args, env)
    # A portable rendering of the command for results.json (no local paths).
    shown = ['agentwrench', *args]
    shown = [a.replace(str(work), '$WORK') for a in shown]
    return catalogue, shown


def cli_measure(catalogue: Path, env: dict[str, str], config: Path | None = None) -> dict:
    args = ['slim', 'measure', str(catalogue), '--json']
    if config is not None:
        args += ['--config', str(config)]
    return json.loads(aw(args, env).stdout)


def cli_plan(catalogue: Path, budget: int, env: dict[str, str]) -> tuple[dict, str]:
    result = aw(['slim', 'plan', str(catalogue), '--budget', str(budget)], env)
    return json.loads(result.stdout), result.stderr


def tokenizer_counts(tools: list[dict], encodings: dict) -> dict[str, int]:
    """Count the exact JSON text `slim measure` sizes: compact {name,description?,inputSchema} per tool."""
    from agentwrench.slim.jsjson import stringify

    texts = []
    for tool in tools:
        core = {'name': tool['name']}
        if 'description' in tool:
            core['description'] = tool['description']
        core['inputSchema'] = tool['inputSchema']
        texts.append(stringify(core))
    counts = {'bytes': sum(len(t.encode('utf-8')) for t in texts)}
    for name, enc in encodings.items():
        counts[name] = sum(len(enc.encode(t, disallowed_special=())) for t in texts)
    return counts


def variant(name: str, config: dict, tools: list[dict], catalogue: Path, work: Path, env: dict[str, str],
            encodings: dict, before: dict) -> dict:
    from agentwrench.slim import apply_config

    path = work / 'configs' / f'{catalogue.stem}.{name}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + '\n')
    measured = cli_measure(catalogue, env, path)
    counts = tokenizer_counts(apply_config(tools, config), encodings)
    assert counts['bytes'] == measured['bytes'], (name, counts['bytes'], measured['bytes'])
    return {
        'config': config, 'tools': measured['tools'], 'bytes': measured['bytes'],
        'estimatedTokens': measured['estimatedTokens'],
        'savedPercent': pct(before['bytes'], measured['bytes']),
        **{k: v for k, v in counts.items() if k != 'bytes'},
        **{f'savedPercent_{k}': pct(before[k], v) for k, v in counts.items() if k != 'bytes'},
    }


def pct(before: int, after: int) -> float:
    return round(100 * (before - after) / before, 1) if before else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--workdir', type=Path, help='scratch directory (default: a new temporary directory)')
    parser.add_argument('--output', type=Path, default=HERE / 'results.json')
    parser.add_argument('--only', nargs='*', help='server ids to run (default: all)')
    ns = parser.parse_args()

    import tiktoken

    work = (ns.workdir or Path(tempfile.mkdtemp(prefix='agentwrench-bench-'))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    (work / 'tmp').mkdir(exist_ok=True)
    log(f'workdir: {work}')
    env = minimal_env(work)
    encodings = {name: tiktoken.get_encoding(name) for name in TIKTOKEN_ENCODINGS}

    from agentwrench.slim import extract_tools

    results = []
    for server in SERVERS:
        if ns.only and server['id'] not in ns.only:
            continue
        exe = install(server, work, env)
        catalogue, command = discover(server, exe, work, env)
        tools = extract_tools(json.loads(catalogue.read_text()))
        before = cli_measure(catalogue, env)
        counts = tokenizer_counts(tools, encodings)
        assert counts['bytes'] == before['bytes']
        full_bytes = sum(len(json.dumps(t, separators=(',', ':'), ensure_ascii=False).encode()) for t in tools)
        entry = {
            'id': server['id'], 'name': server['name'], 'kind': server['kind'], 'package': server['package'],
            'version': server['version'], 'command': command,
            'tools': before['tools'],
            'before': {'bytes': before['bytes'], 'estimatedTokens': before['estimatedTokens'],
                       **{k: v for k, v in counts.items() if k != 'bytes'}},
            'fullToolObjectBytes': full_bytes,
            'largestTools': [{'name': t['name'], 'estimatedTokens': t['estimatedTokens']} for t in before['top'][:3]],
            'variants': {},
        }
        args = (tools, catalogue, work, env, encodings, {**entry['before'], 'bytes': before['bytes']})
        entry['variants']['trim160'] = variant('trim160', TRIM_ONLY, *args)
        entry['variants']['strip'] = variant('strip', STRIP_ONLY, *args)
        entry['variants']['floor'] = variant('floor', FLOOR, *args)
        for name, fraction in BUDGETS.items():
            budget = max(1, math.floor(before['estimatedTokens'] * fraction))
            config, analysis = cli_plan(catalogue, budget, env)
            v = variant(name, config, *args)
            v['budget'] = budget
            v['meetsBudget'] = v['estimatedTokens'] <= budget
            v['planOutput'] = analysis.strip().splitlines()
            entry['variants'][name] = v
        results.append(entry)
        log(f"  {server['id']}: {entry['tools']} tools, ~{before['estimatedTokens']} est. tokens")

    def total(ids: list[str], key: str | None) -> dict:
        rows = [r for r in results if r['id'] in ids]
        pick = (lambda r: r['before']) if key is None else (lambda r: r['variants'][key])
        out = {'tools': sum(r['tools'] for r in rows), 'bytes': sum(pick(r)['bytes'] for r in rows)}
        out['estimatedTokens'] = sum(pick(r)['estimatedTokens'] for r in rows)
        for enc in TIKTOKEN_ENCODINGS:
            out[enc] = sum(pick(r)[enc] for r in rows)
        return out

    stack = None
    if all(any(r['id'] == s for r in results) for s in STACK):
        stack = {'servers': STACK, 'before': total(STACK, None),
                 **{k: total(STACK, k) for k in ['trim160', 'strip', 'floor', *BUDGETS]}}
        for key in ['trim160', 'strip', 'floor', *BUDGETS]:
            stack[key]['savedPercent'] = pct(stack['before']['bytes'], stack[key]['bytes'])
            for enc in TIKTOKEN_ENCODINGS:
                stack[key][f'savedPercent_{enc}'] = pct(stack['before'][enc], stack[key][enc])

    uv_version = subprocess.run(['uv', '--version'], capture_output=True, text=True).stdout.strip()
    document = {
        'generatedBy': 'benchmarks/mcp-servers/run.py',
        'date': dt.date.today().isoformat(),
        'agentwrench': aw(['slim', '--version'], env).stdout.strip(),
        'environment': {
            'platform': f'{platform.system()} {platform.machine()}',
            'node': subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip(),
            'uv': uv_version, 'python': platform.python_version(), 'tiktoken': tiktoken.__version__,
        },
        'estimator': 'ceil(UTF-8 bytes / 4) of compact JSON {name, description?, inputSchema} per tool',
        'tokenizers': TIKTOKEN_ENCODINGS,
        'configs': {'trim160': TRIM_ONLY, 'strip': STRIP_ONLY, 'floor': FLOOR,
                    **{k: f'agentwrench slim plan --budget floor({v} * before estimate)' for k, v in BUDGETS.items()}},
        'servers': results,
        'stack': stack,
    }
    ns.output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n')
    log(f'wrote {ns.output}')
    print(table(document))
    return 0


def table(doc: dict) -> str:
    """Markdown summary table. Each cell: slim estimate / tiktoken o200k_base (percent saved by each)."""
    lines = [
        '| Server | Version | Tools | Before | Trim-only (160 chars) | Strip-only | `plan` @50% budget | Floor (no descriptions) |',
        '|---|---|--:|--:|--:|--:|--:|--:|',
    ]

    def cell(v: dict) -> str:
        return f"{v['estimatedTokens']:,} / {v['o200k_base']:,} (−{v['savedPercent']:.0f}% / −{v['savedPercent_o200k_base']:.0f}%)"

    for s in doc['servers']:
        b, v = s['before'], s['variants']
        cap = v['plan50']['config'].get('maxDescriptionChars', 'none')
        met = '' if v['plan50']['meetsBudget'] else ', budget not met'
        lines.append(
            f"| {s['name']} | `{s['package']}@{s['version']}` | {s['tools']} | {b['estimatedTokens']:,} / {b['o200k_base']:,} "
            f"| {cell(v['trim160'])} | {cell(v['strip'])} | {cell(v['plan50'])} cap {cap}{met} | {cell(v['floor'])} |")
    if doc['stack']:
        st = doc['stack']
        lines.append(
            f"| **Typical stack: {' + '.join(st['servers'])}** | | {st['before']['tools']} "
            f"| **{st['before']['estimatedTokens']:,} / {st['before']['o200k_base']:,}** | {cell(st['trim160'])} "
            f"| {cell(st['strip'])} | **{cell(st['plan50'])}** | {cell(st['floor'])} |")
    return '\n'.join(lines)


if __name__ == '__main__':
    raise SystemExit(main())
