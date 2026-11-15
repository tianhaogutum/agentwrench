"""Tool extraction, transformation (filter/truncate/strip) and byte measurement."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import collation
from .bounds import validate_json_tree
from .config import InputError, glob_match, is_visible, validate_config
from .jsjson import (
    JS_WHITESPACE,
    JS_WHITESPACE_CLASS,
    MISSING,
    js_keys,
    stringify,
    utf16_length,
)

__all__ = [
    'apply_config', 'extract_tools', 'glob_match', 'is_visible', 'measure',
    'truncate_description', 'validate_config',
]

MAP_SCHEMAS = frozenset({'properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'})
ARRAY_SCHEMAS = frozenset({'allOf', 'anyOf', 'oneOf', 'prefixItems'})
SINGLE_SCHEMAS = frozenset({
    'additionalProperties', 'additionalItems', 'unevaluatedProperties', 'unevaluatedItems', 'propertyNames',
    'contains', 'not', 'if', 'then', 'else', 'contentSchema',
})
REFERENCE_KEYS = ('$ref', '$dynamicRef', '$recursiveRef')
ESTIMATOR = 'ceil(UTF-8 bytes / 4); not a model tokenizer'
SCOPE = 'sum of compact JSON {name,description?,inputSchema}; excludes envelope and other tool metadata'

_SENTENCE_END = re.compile(f'[.!?。！？](?={JS_WHITESPACE_CLASS}|\\Z)')
_SPACES = re.compile(f'{JS_WHITESPACE_CLASS}+')
_DIGITS = re.compile('[0-9]+')
_PERCENT = re.compile('(?:%[0-9A-Fa-f]{2})+')
_BAD_PERCENT = re.compile('%(?![0-9A-Fa-f]{2})')

Warn = Callable[[str], Any]


def extract_tools(value: Any) -> list[dict[str, Any]]:
    validate_json_tree(value)
    tools = value if isinstance(value, list) else (value.get('tools', MISSING) if isinstance(value, dict) else MISSING)
    if isinstance(tools, list) and len(tools) > 10000:
        raise InputError('tools: exceeds 10000 tool limit')
    if not isinstance(tools, list):
        raise InputError('tools: expected a tools/list result object or a bare array')
    names: set[str] = set()
    for i, tool in enumerate(tools):
        name = tool.get('name') if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name or utf16_length(name) > 1024:
            raise InputError(f'tools[{i}].name: expected a nonempty string of at most 1024 characters')
        if name in names:
            raise InputError(f'tools[{i}].name: duplicate tool name')
        names.add(name)
        description = tool.get('description', MISSING)
        if isinstance(description, str) and len(description) > 1_000_000:
            raise InputError(f'tools[{i}].description: exceeds 1000000 code points')
        if description is not MISSING and not isinstance(description, str):
            raise InputError(f'tools[{i}].description: expected a string')
        if not isinstance(tool.get('inputSchema'), dict):
            raise InputError(f'tools[{i}].inputSchema: expected a JSON Schema object')
    return tools


def truncate_description(text: str, max_chars: int | float | None) -> str:
    """Cap at ``max_chars`` code points, preferring sentence then word boundaries."""
    if max_chars is None or len(text) <= max_chars:
        return text
    max_chars = int(max_chars)
    prefix = text[:max_chars]
    # Prefer the latest sentence, then a word boundary. A single long word is cut
    # at a Unicode code point; no suffix is added beyond the configured budget.
    sentences = [m for m in _SENTENCE_END.finditer(text) if m.start() < len(prefix)]
    if sentences:
        return prefix[:sentences[-1].start() + 1].rstrip(JS_WHITESPACE)
    if text[max_chars] in JS_WHITESPACE or (prefix and prefix[-1] in JS_WHITESPACE):
        return prefix.rstrip(JS_WHITESPACE)
    spaces = list(_SPACES.finditer(prefix))
    return (prefix[:spaces[-1].start()] if spaces else prefix).rstrip(JS_WHITESPACE)


def _decode_uri_component(text: str) -> str:
    """``decodeURIComponent``; raises ``ValueError`` like its ``URIError``."""
    if '%' not in text:
        return text
    if _BAD_PERCENT.search(text):
        raise ValueError('malformed URI sequence')
    return _PERCENT.sub(lambda m: bytes.fromhex(m.group(0).replace('%', '')).decode('utf-8'), text)


def _effective_strip(schema: Any, requested: list[str]) -> set[str]:
    """Conservatively retain an annotation keyword throughout a schema if a JSON
    Pointer reference can target that annotation (including a nested literal).
    Pointer positions are classified so a property named "default" is not
    mistaken for the default annotation. No URI is fetched."""
    strip = set(requested)
    if not strip:
        return strip

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            entries = [(key, value[key]) for key in js_keys(value)]
        elif isinstance(value, list):
            entries = [(str(i), item) for i, item in enumerate(value)]
        else:
            return
        for key, child in entries:
            if key in REFERENCE_KEYS and isinstance(child, str):
                hash_index = child.find('#')
                if hash_index != -1:
                    try:
                        fragment = _decode_uri_component(child[hash_index + 1:])
                    except ValueError:
                        fragment = ''
                    if fragment.startswith('/'):
                        _release(fragment, strip)
            inspect(child)

    inspect(schema)
    return strip


def _release(fragment: str, strip: set[str]) -> None:
    tokens = [token.replace('~1', '/').replace('~0', '~') for token in fragment[1:].split('/')]
    kind = 'schema'
    for i, token in enumerate(tokens):
        if kind in ('map', 'array'):
            kind = 'schema'
            continue
        if token in strip:
            strip.discard(token)
            break
        if token in MAP_SCHEMAS or token == 'dependencies':
            kind = 'map'
        elif token in ARRAY_SCHEMAS:
            kind = 'array'
        elif token == 'items':
            following = tokens[i + 1] if i + 1 < len(tokens) else ''
            kind = 'array' if _DIGITS.fullmatch(following) else 'schema'
        elif token in SINGLE_SCHEMAS:
            kind = 'schema'
        else:
            break  # data/unknown keywords are not recursively stripped


def _strip_schema(schema: Any, strip: set[str], depth: int = 0) -> Any:
    if depth > 128:
        raise InputError('inputSchema: schema nesting exceeds 128')
    if not isinstance(schema, dict):
        return schema
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in strip:
            continue
        if key in MAP_SCHEMAS and isinstance(value, dict):
            value = {name: _strip_schema(node, strip, depth + 1) for name, node in value.items()}
        elif key in ARRAY_SCHEMAS and isinstance(value, list):
            value = [_strip_schema(node, strip, depth + 1) for node in value]
        elif key == 'items':
            if isinstance(value, list):
                value = [_strip_schema(node, strip, depth + 1) for node in value]
            else:
                value = _strip_schema(value, strip, depth + 1)
        elif key == 'dependencies' and isinstance(value, dict):
            value = {name: _strip_schema(node, strip, depth + 1) if isinstance(node, dict) else node for name, node in value.items()}
        elif key in SINGLE_SCHEMAS:
            value = _strip_schema(value, strip, depth + 1)
        result[key] = value
    return result


def apply_config(tools: list[dict[str, Any]], config: dict[str, Any] | None = None, warn: Warn | None = None) -> list[dict[str, Any]]:
    config = {} if config is None else config
    validate_config(config)
    validate_json_tree(tools)
    selected = [tool for tool in tools if is_visible(tool['name'], config)]
    allow = config.get('allow', MISSING)
    if allow is not MISSING and not any(glob_match(pattern, tool['name']) for tool in tools for pattern in allow):
        if warn is not None:
            warn('allow list matched zero tools in this page/snapshot')
    requested = config.get('stripSchemaAnnotations') or []
    cap = config.get('maxDescriptionChars')
    result = []
    for tool in selected:
        slimmed = dict(tool)
        if 'description' in tool:
            slimmed['description'] = truncate_description(tool['description'], cap)
        slimmed['inputSchema'] = _strip_schema(tool['inputSchema'], _effective_strip(tool['inputSchema'], requested))
        result.append(slimmed)
    return result


def tool_bytes(tool: dict[str, Any]) -> int:
    core: dict[str, Any] = {'name': tool['name']}
    if 'description' in tool:
        core['description'] = tool['description']
    core['inputSchema'] = tool['inputSchema']
    return len(stringify(core).encode('utf-8'))


def ceil_div4(value: int) -> int:
    return -(-value // 4)


def measure(tools: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for tool in tools:
        size = tool_bytes(tool)
        entries.append({'name': tool['name'], 'bytes': size, 'estimatedTokens': ceil_div4(size)})
    total = sum(entry['bytes'] for entry in entries)
    return {
        'tools': len(tools), 'bytes': total, 'estimatedTokens': ceil_div4(total),
        'estimator': ESTIMATOR, 'scope': SCOPE, 'perTool': entries, 'top': largest_first(entries)[:10],
    }


def largest_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by bytes descending, then ``name.localeCompare`` (stable)."""
    return sorted(entries, key=lambda entry: (-entry['bytes'], collation.sort_key(entry['name'])))
