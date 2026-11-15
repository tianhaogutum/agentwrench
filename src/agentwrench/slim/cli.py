"""``agentwrench slim`` command line: measure, plan, discover and proxy."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from typing import Any

from .config import PROGRAM, VERSION, InputError, validate_config
from .discover import (
    PROTOCOL_VERSION,
    DiscoveryError,
    run_discover,
    validate_discovery_timeout,
    validate_max_output_bytes,
    validate_max_pages,
    validate_max_tools,
    validate_max_wire_bytes,
    validate_protocol_versions,
    validate_server_env,
    validate_server_unset_env,
    validate_shutdown_timeout,
)
from .jsjson import stringify, to_fixed
from .jsonio import read_json
from .output import CatalogueOutputError, write_catalogue_file
from .plan import plan
from .proxy import run_proxy
from .requests import validate_request_timeout
from .slim import apply_config, extract_tools, measure
from .stdio import write_stderr, write_stdout

DOCS_URL = 'https://github.com/tianhaogutum/agentwrench/blob/main/docs/slim.md'

USAGE = f"""{PROGRAM} {VERSION} — offline MCP context planner and stdio proxy
Usage:
  {PROGRAM} measure <tools.json> [--config slim.json] [--json]
  {PROGRAM} plan <tools.json> --budget <estimated-tokens>
  {PROGRAM} discover [--output <catalogue.json>] [--server-cwd <directory>] [--server-env <NAME=VALUE>]... [--server-unset-env <NAME>]... [--accept-protocol-version <YYYY-MM-DD>]... [--config slim.json] [--request-timeout-ms <ms>] [--discovery-timeout-ms <ms>] [--shutdown-timeout-ms <ms>] [--max-pages <n>] [--max-tools <n>] [--max-output-bytes <n>] [--max-wire-bytes <n>] -- <server> [args...]
  {PROGRAM} proxy --config slim.json [--request-timeout-ms <ms>] -- <server> [args...]

Token estimates are ceil(UTF-8 bytes / 4), not model token counts.
Plan writes a ready config to stdout and analysis to stderr; it never starts a server.
Discover requires MCP protocol {PROTOCOL_VERSION} unless older revisions are accepted
with --accept-protocol-version (e.g. 2025-06-18).
Globs support only * and ?; deny wins. Limits and details: {DOCS_URL}"""

MODES = ('measure', 'plan', 'proxy', 'discover')
_DIGITS = re.compile('[0-9]+')

# Value options accepted by each mode (beyond the repeatable environment options).
_VALUE_OPTIONS: dict[str, set[str]] = {
    'measure': {'--config'},
    'plan': {'--budget'},
    'proxy': {'--config', '--request-timeout-ms'},
    'discover': {'--config', '--output', '--server-cwd', '--request-timeout-ms', '--discovery-timeout-ms',
                 '--shutdown-timeout-ms', '--max-pages', '--max-tools', '--max-output-bytes', '--max-wire-bytes'},
}

# Repeatable discover-only options and their option keys.
_REPEATABLE = {'--server-env': 'serverEnv', '--server-unset-env': 'serverUnsetEnv',
               '--accept-protocol-version': 'protocolVersions'}

# (option, error message for non-digit values, validator)
_NUMERIC_OPTIONS: list[tuple[str, str, str, Any]] = [
    ('request-timeout-ms', 'request_timeout_ms', '--request-timeout-ms: expected an integer from 1 to 2147483647', validate_request_timeout),
    ('discovery-timeout-ms', 'discovery_timeout_ms', '--discovery-timeout-ms: expected an integer from 1 to 2147483647', validate_discovery_timeout),
    ('shutdown-timeout-ms', 'shutdown_timeout_ms', '--shutdown-timeout-ms: expected an integer from 1 to 30000', validate_shutdown_timeout),
    ('max-pages', 'max_pages', '--max-pages: expected an integer from 1 to 10000', validate_max_pages),
    ('max-tools', 'max_tools', '--max-tools: expected an integer from 1 to 10000', validate_max_tools),
    ('max-output-bytes', 'max_output_bytes', '--max-output-bytes: expected an integer from 13 to 16777216', validate_max_output_bytes),
    ('max-wire-bytes', 'max_wire_bytes', '--max-wire-bytes: expected an integer from 1 to 2147483647', validate_max_wire_bytes),
]


def _missing(value: str | None) -> bool:
    return not value or value.startswith('--')


def parse(args: list[str]) -> tuple[str, dict[str, Any], str | None, list[str]]:
    mode = args[0] if args else None
    rest = args[1:]
    if mode not in MODES:
        raise InputError('command: expected measure, plan, proxy or discover (see --help)')
    options: dict[str, Any] = {}
    positional: list[str] = []
    command: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        following = rest[i + 1] if i + 1 < len(rest) else None
        if arg == '--' and mode in ('proxy', 'discover'):
            command = rest[i + 1:]
            break
        if arg in _REPEATABLE:
            if mode != 'discover':
                raise InputError(f'{arg}: unsupported option')
            if _missing(following):
                raise InputError(f'{arg}: missing value')
            options.setdefault(_REPEATABLE[arg], []).append(following)
            i += 1
        elif arg.startswith('--server-env='):
            # Never reflect a supplied assignment in wrapper diagnostics.
            raise InputError('--server-env: use a separate NAME=VALUE argument (discover only)')
        elif arg.startswith('--server-unset-env='):
            raise InputError('--server-unset-env: use a separate NAME argument (discover only)')
        elif arg == '--json' and mode == 'measure':
            if options.get('json'):
                raise InputError('--json: duplicate option')
            options['json'] = True
        elif arg in _VALUE_OPTIONS[mode]:
            key = arg[2:]
            if key in options:
                raise InputError(f'{arg}: duplicate option')
            if _missing(following):
                raise InputError(f'{arg}: missing value')
            options[key] = following
            i += 1
        elif arg.startswith('-'):
            raise InputError(f'{arg}: unsupported option')
        else:
            positional.append(arg)
        i += 1
    if mode == 'proxy':
        if 'config' not in options or positional or not command:
            raise InputError('proxy: requires --config <file> -- <server> [args...]')
    elif mode == 'discover':
        if positional or not command:
            raise InputError('discover: requires [--config <file>] -- <server> [args...]')
    elif len(positional) != 1:
        raise InputError(f'{mode}: requires one tools.json file')
    if mode == 'plan' and not _DIGITS.fullmatch(options.get('budget', '')):
        raise InputError('--budget: expected a positive integer')
    for option, key, message, validate in _NUMERIC_OPTIONS:
        if option in options:
            if not _DIGITS.fullmatch(options[option]):
                raise InputError(message)
            options[key] = validate(int(options[option]))
    validate_server_unset_env(options.get('serverUnsetEnv'), validate_server_env(options.get('serverEnv')))
    validate_protocol_versions(options.get('protocolVersions'))
    return mode, options, positional[0] if positional else None, command


def _accepted_versions(extra: list[str] | None) -> tuple[str, ...] | None:
    """The default revision plus any explicitly accepted older ones."""
    return None if not extra else tuple(dict.fromkeys((PROTOCOL_VERSION, *extra)))


MIN_USEFUL_DESCRIPTION_CHARS = 40


def _plan_warnings(tools: list[dict[str, Any]], p: dict[str, Any]) -> list[str]:
    """Explain a plan that only fits by gutting descriptions, or does not fit at all."""
    cap = p['config'].get('maxDescriptionChars')
    short = cap is not None and cap < MIN_USEFUL_DESCRIPTION_CHARS
    if not short and p['meetsBudget']:
        return []
    lines = []
    if short:
        lines.append(f'description cap of {cap} code point{"" if cap == 1 else "s"} is below '
                     f'{MIN_USEFUL_DESCRIPTION_CHARS}; tool descriptions lose most of their usage guidance.')
    if not p['meetsBudget']:
        lines.append(f"budget {p['budget']} is not met even by this config (~{p['after']['estimatedTokens']} tokens).")
    strip = {key: value for key, value in p['config'].items() if key != 'maxDescriptionChars'}
    strip_only = measure(apply_config(tools, strip))['estimatedTokens']
    useful = measure(apply_config(tools, {**strip, 'maxDescriptionChars': MIN_USEFUL_DESCRIPTION_CHARS}))['estimatedTokens']
    lines.append(f'consider strip-only (remove maxDescriptionChars from the config: ~{strip_only} tokens) '
                 f'or a higher budget (--budget {useful} keeps up to {MIN_USEFUL_DESCRIPTION_CHARS} characters).')
    return lines


def _report(m: dict[str, Any]) -> str:
    return f"tools: {m['tools']}  bytes: {m['bytes']:,}  est.tokens: ~{m['estimatedTokens']:,}\n"


def _out(text: str) -> None:
    write_stdout(text.encode('utf-8', 'surrogatepass'))


def _err(text: str) -> None:
    write_stderr(text)


async def _run(args: list[str]) -> int:
    mode, options, file, command = parse(args)
    config = validate_config(read_json(options['config'])) if 'config' in options else {}
    if mode == 'proxy':
        return await run_proxy(command, config, request_timeout_ms=options.get('request_timeout_ms'))
    if mode == 'discover':
        snapshot = await run_discover(
            command, config, server_env=options.get('serverEnv'), server_unset_env=options.get('serverUnsetEnv'),
            server_cwd=options.get('server-cwd'), request_timeout_ms=options.get('request_timeout_ms'),
            discovery_timeout_ms=options.get('discovery_timeout_ms'), shutdown_timeout_ms=options.get('shutdown_timeout_ms'),
            max_pages=options.get('max_pages'), max_tools=options.get('max_tools'),
            max_output_bytes=options.get('max_output_bytes'), max_wire_bytes=options.get('max_wire_bytes'),
            protocol_versions=_accepted_versions(options.get('protocolVersions')))
        if 'output' in options:
            write_catalogue_file(options['output'], snapshot)
        else:
            _out(snapshot)
        return 0
    tools = extract_tools(read_json(file))
    if mode == 'plan':
        p = plan(tools, int(options['budget']))
        _out(f"{stringify(p['config'], 2)}\n")
        _err(f"Estimator: {p['before']['estimator']}\nBudget: {p['budget']}\n")
        for stage in p['stages']:
            _err(f"{stage['stage']}: {stage['bytes']} B, ~{stage['estimatedTokens']} tokens\n")
        _err('Budget met. No tools denied.\n' if p['meetsBudget'] else 'Budget NOT met without removing tools. Config denies nothing.\n')
        for t in p['denySuggestions']:
            _err(f"Deny suggestion (review manually): {stringify(t['name'])} ({t['bytes']} B); remaining ~{t['estimatedTokensIfDenied']}\n")
        for line in _plan_warnings(tools, p):
            _err(f'Warning: {line}\n')
        return 0
    has_config = 'config' in options
    before = measure(tools)
    after = measure(apply_config(tools, config, lambda message: _err(f'{PROGRAM}: {message}\n')))
    if options.get('json'):
        report = dict(after)
        if has_config:
            report.update(before=before, savedBytes=before['bytes'] - after['bytes'],
                          savedEstimatedTokens=before['estimatedTokens'] - after['estimatedTokens'])
        _out(f'{stringify(report, 2)}\n')
        return 0
    if has_config:
        saved = before['bytes'] - after['bytes']
        percent = to_fixed(saved / before['bytes'] * 100, 1) if before['bytes'] else '0.0'
        _out(f'before  {_report(before)}after   {_report(after)}saved: {saved} B ({percent}%)\n')
    else:
        _out(_report(after))
    _out(f"estimator: {after['estimator']}\nscope: {after['scope']}\ntop:\n")
    for t in after['top']:
        _out(f"  {stringify(t['name'])}  {t['bytes']} B  ~{t['estimatedTokens']} tokens\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run ``agentwrench slim`` with ``argv`` (defaults to ``sys.argv[1:]``)."""
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        if len(args) == 1 and args[0] in ('--help', '-h'):
            _out(f'{USAGE}\n')
            return 0
        if len(args) == 1 and args[0] == '--version':
            _out(f'{VERSION}\n')
            return 0
        return asyncio.run(_run(args))
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        # Die from SIGINT like an unhandled Node.js signal (no traceback).
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
        return 130
    except (InputError, DiscoveryError, CatalogueOutputError) as error:
        _err(f'{PROGRAM}: {error}\n')
        return 2 if isinstance(error, InputError) else 1
    except Exception:
        _err(f'{PROGRAM}: unexpected failure\n')
        return 1
