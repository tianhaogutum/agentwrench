"""Explicit native fields -> intermediate representation -> native fields."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from .formats import ConfigError, Document

# Fields here have a verified equivalent. Everything else is native passthrough.
MAPPINGS = {
    'claude': {'command': 'command', 'args': 'args', 'env': 'env', 'url': 'url', 'headers': 'headers'},
    'codex': {'command': 'command', 'args': 'args', 'env': 'env', 'url': 'url',
              'http_headers': 'headers', 'enabled': 'enabled', 'cwd': 'cwd',
              'startup_timeout_sec': 'startup_timeout_sec', 'tool_timeout_sec': 'tool_timeout_sec',
              'bearer_token_env_var': 'bearer_token_env_var', 'env_http_headers': 'env_http_headers',
              'enabled_tools': 'enabled_tools', 'disabled_tools': 'disabled_tools'},
    'cline': {'command': 'command', 'args': 'args', 'env': 'env', 'url': 'url',
              'headers': 'headers', 'disabled': 'enabled'},
    'continue': {'command': 'command', 'args': 'args', 'env': 'env', 'url': 'url', 'cwd': 'cwd'},
    'vscode': {'command': 'command', 'args': 'args', 'env': 'env', 'url': 'url',
               'headers': 'headers', 'cwd': 'cwd'},
}
TRANSPORTS = {
    'claude': {'stdio': 'stdio', 'http': 'http', 'sse': 'sse', 'ws': 'ws'},
    'cline': {'stdio': 'stdio', 'streamableHttp': 'http', 'sse': 'sse'},
    'continue': {'stdio': 'stdio', 'streamable-http': 'http', 'sse': 'sse'},
    'vscode': {'stdio': 'stdio', 'http': 'http', 'sse': 'sse'},
    'codex': {'stdio': 'stdio', 'http': 'http'},
}


@dataclass
class Server:
    name: str
    origin: str
    raw: dict
    fields: dict
    transport: str
    passthrough: dict[str, dict] = field(default_factory=dict)


@dataclass
class Finding:
    server: str
    field: str
    kind: str
    message: str
    path: tuple[str | int, ...]
    scope: str = 'server'

    def as_dict(self):
        # Retain the established report schema independently of versioned records.
        return {k: getattr(self, k) for k in ('server', 'field', 'kind', 'message')}


def validate(raw, fmt):
    known = set(MAPPINGS[fmt])
    if fmt != 'codex':
        known.add('type')
    known.update({'cline': {'autoApprove', 'timeout'},
                  'continue': {'connectionTimeout', 'requestOptions'}}.get(fmt, set()))
    for key in ('type', 'command', 'url', 'cwd', 'bearer_token_env_var'):
        if key in known and key in raw and (not isinstance(raw[key], str) or not raw[key]):
            raise ConfigError(f'{key} must be a nonempty string (value masked)')
    for key in ('args', 'enabled_tools', 'disabled_tools', 'autoApprove'):
        if key in known and key in raw and (not isinstance(raw[key], list) or
                           not all(isinstance(v, str) for v in raw[key])):
            raise ConfigError(f'{key} must be a list of strings (value masked)')
    for key in ('enabled', 'disabled'):
        if key in known and key in raw and not isinstance(raw[key], bool):
            raise ConfigError(f'{key} must be a boolean')
    for key in ('startup_timeout_sec', 'tool_timeout_sec', 'timeout', 'connectionTimeout'):
        if key in known and key in raw and (type(raw[key]) not in (int, float) or raw[key] <= 0):
            raise ConfigError(f'{key} must be a positive number')
    maps = [('env', fmt == 'vscode'), ('headers', False), ('http_headers', False),
            ('env_http_headers', False)]
    for key, relaxed in maps:
        if key in known and key in raw:
            v = raw[key]
            if not isinstance(v, dict) or not all(isinstance(x, str) or
                    (relaxed and (x is None or type(x) in (int, float))) for x in v.values()):
                raise ConfigError(f'{key} must be a string map (VS Code env also allows numbers/null)')
    if fmt == 'continue' and 'requestOptions' in raw and not isinstance(raw['requestOptions'], dict):
        raise ConfigError('requestOptions must be an object')
    if fmt == 'continue' and 'headers' in raw.get('requestOptions', {}):
        h = raw['requestOptions']['headers']
        if not isinstance(h, dict) or not all(isinstance(v, str) for v in h.values()):
            raise ConfigError('requestOptions.headers must be a string map')
    if bool(raw.get('command')) == bool(raw.get('url')):
        raise ConfigError('each server requires exactly one of command or url')
    if 'url' in raw and any(k in known and k in raw for k in ('args', 'env', 'cwd')):
        raise ConfigError('remote servers cannot contain stdio args/env/cwd')
    if 'command' in raw and any(k in known and k in raw for k in ('headers', 'http_headers', 'env_http_headers',
                                                   'bearer_token_env_var')):
        raise ConfigError('stdio servers cannot contain remote authentication fields')


def to_ir(document: Document) -> list[Server]:
    result = []
    fmt = document.format
    for name, raw in document.servers.items():
        validate(raw, fmt)
        mapping = MAPPINGS[fmt]
        fields = {canonical: copy.deepcopy(raw[key]) for key, canonical in mapping.items() if key in raw}
        if fmt == 'cline' and 'disabled' in raw:
            fields['enabled'] = not raw['disabled']
        unknown = {k: copy.deepcopy(v) for k, v in raw.items() if k not in mapping and k != 'type'}
        if fmt == 'codex':
            transport = 'stdio' if 'command' in raw else 'http'
            if 'type' in raw:
                unknown['type'] = copy.deepcopy(raw['type'])
        else:
            default = 'stdio' if 'command' in raw else ('sse' if fmt == 'cline' else
                       'ambiguous-remote' if fmt == 'continue' else 'http')
            native = raw.get('type', default)
            transport = TRANSPORTS[fmt].get(native, 'unsupported:' + native)
        if (transport == 'stdio') != ('command' in raw) and not transport.startswith('unsupported:'):
            raise ConfigError('transport type conflicts with command/url')
        if fmt == 'continue' and 'requestOptions' in unknown:
            options = unknown['requestOptions']
            if 'headers' in options:
                fields['headers'] = options.pop('headers')
            if not options:
                del unknown['requestOptions']
        result.append(Server(name, fmt, copy.deepcopy(raw), fields, transport, {fmt: unknown}))
    return result


def native_paths(value, prefix=(), label_prefix=''):
    """Traverse native keys/indices alongside their legacy display labels."""
    if isinstance(value, (dict, list)) and not value:
        yield prefix, label_prefix, value
    elif isinstance(value, dict):
        for key, v in value.items():
            label = f'{label_prefix}.{key}' if label_prefix else key
            yield from native_paths(v, (*prefix, key), label)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from native_paths(v, (*prefix, i), f'{label_prefix}[{i}]')
    else:
        yield prefix, label_prefix, value


def paths(value, prefix=''):
    for _, label, v in native_paths(value, label_prefix=prefix):
        yield label, v


SECRET_KEY = re.compile(r'(token|secret|password|passwd|authorization|api[-_]?key|credential|cookie|private[-_]?key)', re.I)
RAW_TOKEN = re.compile(r'(?:\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)', re.I)
TOKEN = re.compile(r'(?:\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}|\bBearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|[?&](?:token|key|secret|password)=\S+|://[^/\s:@]+:[^/\s@]+@|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)', re.I)
REF = re.compile(r'\$\{[^{}]+\}|\$\{\{[^{}]+\}\}')


def secret_locations(value):
    for segments, path, v in native_paths(value):
        if not isinstance(v, str) or not v or v == '[REDACTED]':
            continue
        stripped = REF.sub(lambda m: m.group(0).split(':-', 1)[1].rstrip('}')
                           if ':-' in m.group(0) else '', v)
        # env-var names are references by schema, not secret values.
        reference_field = path.split('.')[-1] == 'bearer_token_env_var' or '.env_http_headers.' in '.' + path
        if reference_field:
            continue
        if TOKEN.search(stripped) or (SECRET_KEY.search(path) and stripped.strip() not in ('', 'Bearer')):
            yield segments, path


def secret_paths(value):
    for _, path in secret_locations(value):
        yield path


def redacted(value):
    """Return a copy with entire sensitive leaves replaced; never interpolate."""
    def walk(v, prefix=''):
        if isinstance(v, dict):
            return {k: walk(x, f'{prefix}.{k}' if prefix else k) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f'{prefix}[{i}]') for i, x in enumerate(v)]
        return '[REDACTED]' if prefix in sensitive else v
    sensitive = set(secret_paths(value))
    return walk(value)


def project(servers: list[Server], target: str) -> tuple[dict, list[Finding]]:
    output, findings = {}, []
    for server in servers:
        name, source = server.name, server.origin
        def add(path, field, kind, message):
            findings.append(Finding(name, field, kind, message, path))
        for segments, path in secret_locations(server.raw):
            add(segments, path, 'secret', 'literal [REDACTED]; replace with a client-supported environment reference')
        for segments, path, v in native_paths(server.raw):
            if isinstance(v, str) and ('${' in v or '$env:' in v) and source != target:
                add(segments, path, 'interpolation', 'expression kept literally; client interpolation differs; no expansion or rewriting performed')
        if source == target:
            output[name] = copy.deepcopy(server.raw)
            continue
        if server.fields.get('enabled') is False and target not in ('codex', 'cline'):
            key = 'enabled' if source == 'codex' else 'disabled'
            add((key,), key, 'error',
                'disabled server cannot be represented by target; server omitted to prevent accidental enablement')
            continue
        if server.transport not in TRANSPORTS[target].values():
            add(('type',), 'type', 'error', 'transport unsupported by target; server omitted')
            continue
        if server.transport != 'stdio':
            url = server.fields.get('url', '')
            if not url.startswith(('https://', 'http://', '${')):
                add(('url',), 'url', 'error', 'non-HTTP endpoint scheme cannot be mapped to target; server omitted')
                continue
        out = {}
        reverse = {v: k for k, v in MAPPINGS[target].items()}
        if target == 'continue':
            reverse['headers'] = 'requestOptions.headers'
        for canonical, value in server.fields.items():
            if canonical not in reverse:
                source_key = next((k for k, v in MAPPINGS[source].items() if v == canonical), canonical)
                add((source_key,), source_key, 'loss', 'no verified equivalent in target; omitted')
                continue
            if canonical == 'env' and target != 'vscode' and any(not isinstance(v, str) for v in value.values()):
                add(('env',), 'env', 'error', 'non-string VS Code env values unsupported by target; server omitted')
                out = None
                break
            key = reverse[canonical]
            if key == 'requestOptions.headers':
                out['requestOptions'] = {'headers': copy.deepcopy(value)}
            else:
                out[key] = not value if key == 'disabled' else copy.deepcopy(value)
        if out is None:
            continue
        if target != 'codex':
            out['type'] = next(k for k, v in TRANSPORTS[target].items() if v == server.transport)
        for key, value in server.passthrough[source].items():
            if isinstance(value, dict) and value:
                for segments, path, _ in native_paths(value, (key,), key):
                    add(segments, path, 'loss', 'native passthrough has no verified equivalent; omitted')
            else:
                message = ('Cline timeout is seconds for all requests; Codex tool/startup lifecycles differ; omitted'
                           if source == 'cline' and key == 'timeout' else
                           'native connection timeout units/lifecycle not mapped; omitted'
                           if key == 'connectionTimeout' else
                           'native passthrough has no verified equivalent; omitted')
                add((key,), key, 'loss', message)
        output[name] = out
    return output, findings


def semantic(server: Server):
    result = copy.deepcopy(server.fields)
    result['transport'] = server.transport
    result.setdefault('enabled', True)
    if server.transport == 'stdio':
        result.setdefault('args', [])
        result.setdefault('env', {})
    else:
        result.setdefault('headers', {})
    return result


def diff_paths(expected: Any, actual: Any, prefix=''):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys()):
            p = f'{prefix}.{key}' if prefix else key
            if key not in actual or key not in expected:
                yield p
            else:
                yield from diff_paths(expected[key], actual[key], p)
    elif expected != actual or (isinstance(expected, bool) != isinstance(actual, bool)):
        yield prefix


def document_findings(doc: Document, target: str):
    if doc.format == target:
        return []
    from .formats import ROOTS
    return [Finding('(document)', k, 'loss', 'document-level setting is not transferred; retained in source',
                    (k,), 'document')
            for k in doc.data if k != ROOTS[doc.format]]
