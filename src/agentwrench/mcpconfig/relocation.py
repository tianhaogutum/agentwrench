"""Lexical project relocation of explicitly selected native paths, without I/O."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath
import re

from .core import MAPPINGS, TOKEN, to_ir
from .formats import ConfigError, Document, FORMATS, JsonParser, MAX_BYTES, loads, same_value


def create_manifest(document: Document, source_root: str, target_root: str,
                    selections: list[list]) -> dict:
    """Validate a complete relocation before emitting reusable snapshot selectors."""
    _, provenance = relocate(document, source_root, target_root, selections)
    manifest = {
        'schemaVersion': 1, 'operation': 'relocation-selection', 'scope': 'project',
        'format': document.format,
        'sourceSha256': hashlib.sha256(document.text.encode('utf-8')).hexdigest(),
        'sourceRoot': source_root, 'targetRoot': target_root,
        'paths': [record['path'] for record in provenance['relocations']],
    }
    # Include the CLI's formatting and terminal newline: never emit a plan our
    # bounded reader would refuse, even for many selectors with long names.
    if len((json.dumps(manifest, indent=2, ensure_ascii=True) + '\n').encode('utf-8')) > MAX_BYTES:
        raise ConfigError('relocation manifest exceeds 1 MiB limit; reduce selections')
    return manifest


def read_manifest(text: str, source_text: str) -> dict:
    """Read a strict bounded envelope and refuse stale source bytes before editing.

    The digest identifies the loaded UTF-8 snapshot, not a filename, semantic
    config, signature or authorization. No manifest values enter diagnostics.
    Native selection/containment checks still run against that snapshot.
    """
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ConfigError('relocation manifest exceeds 1 MiB limit')
    try:
        value = JsonParser(text).parse()
    except (ConfigError, RecursionError):
        raise ConfigError('invalid relocation manifest JSON (details masked)') from None
    keys = {'schemaVersion', 'operation', 'scope', 'format', 'sourceSha256',
            'sourceRoot', 'targetRoot', 'paths'}
    if (not isinstance(value, dict) or value.keys() != keys
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['operation'] != 'relocation-selection' or value['scope'] != 'project'
            or value['format'] not in FORMATS
            or not isinstance(value['sourceSha256'], str)
            or re.fullmatch(r'[0-9a-f]{64}', value['sourceSha256']) is None
            or not isinstance(value['sourceRoot'], str)
            or not isinstance(value['targetRoot'], str)
            or not isinstance(value['paths'], list) or not value['paths']):
        raise ConfigError('invalid or unsupported relocation selection manifest (values masked)')
    if hashlib.sha256(source_text.encode('utf-8')).hexdigest() != value['sourceSha256']:
        raise ConfigError('relocation source digest mismatch; regenerate and review the manifest')
    return value


def selection(text: str) -> list:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise ConfigError('relocation --path must be a JSON array (value masked)') from None
    if not (isinstance(value, list) and len(value) in (2, 3)
            and isinstance(value[0], str) and value[0]
            and ((len(value) == 2 and value[1] == 'cwd')
                 or (len(value) == 3 and value[1] == 'args'
                     and type(value[2]) is int and value[2] >= 0))):
        raise ConfigError('relocation selects only [server,"cwd"] or [server,"args",index]')
    return value


def absolute_path(value: str) -> PurePosixPath:
    # Never normalize away evidence of ambiguity, expand variables, resolve links,
    # or infer whether an arbitrary argument/string represents a path.
    if (not isinstance(value, str) or not value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/')[1:])
            or any(c in value for c in '\\$%`*?[]{}~:')
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ConfigError('relocation requires normalized absolute POSIX paths (value masked)')
    return PurePosixPath(value)


def relocate(document: Document, source_root: str, target_root: str,
             selections: list[list]) -> tuple[str, dict]:
    """Return edited native text and explicit provenance, or refuse the whole plan.

    Selectors are server names followed by native field segments, never dotted
    strings. Provenance intentionally contains selected path values; ordinary
    errors remain value-masked. This does not move files or discover client scope.
    """
    old_root, new_root = absolute_path(source_root), absolute_path(target_root)
    if old_root == new_root:
        raise ConfigError('relocation requires two different project roots')
    if TOKEN.search(source_root) or TOKEN.search(target_root):
        raise ConfigError('relocation root contains a credential signature (value masked)')
    servers = {s.name: s for s in to_ir(document)}
    if not selections:
        raise ConfigError('relocation requires at least one explicit --path selection')
    updates, records, seen = {}, [], set()
    for supplied in selections:
        # Validate library callers too, without letting bool alias an args index.
        path = selection(json.dumps(supplied))
        if tuple(path) in seen:
            raise ConfigError('relocation has a duplicate path selection (path masked)')
        seen.add(tuple(path))
        name, field = path[:2]
        server = servers.get(name)
        if (server is None or server.transport != 'stdio' or field not in MAPPINGS[document.format]
                or field not in server.raw):
            raise ConfigError('relocation selection is not an existing native stdio path (path masked)')
        value = server.raw[field]
        if field == 'args':
            if path[2] >= len(value):
                raise ConfigError('relocation argument index is out of range (path masked)')
            value = value[path[2]]
        old = absolute_path(value)
        try:
            relative = old.relative_to(old_root)
        except ValueError:
            raise ConfigError('relocation selected path is outside the source project root (value masked)') from None
        relocated = str(new_root / relative)
        update = updates.setdefault(name, {})
        if field == 'args':
            update.setdefault(field, copy.deepcopy(server.raw[field]))[path[2]] = relocated
        else:
            update[field] = relocated
        records.append({'path': path, 'basis': 'explicit-selection', 'relativePath': str(relative),
                        'before': value, 'after': relocated})
    result = document.render(updates)
    edited = loads(result, document.format)
    to_ir(edited)
    expected = copy.deepcopy(document.servers)
    for name, fields in updates.items():
        expected[name].update(fields)
    if not same_value(edited.servers, expected):
        raise ConfigError('relocation could not preserve the complete native server configuration')
    return result, {
        'schemaVersion': 1, 'operation': 'relocate', 'scope': 'project', 'format': document.format,
        'sourceRoot': source_root, 'targetRoot': target_root, 'relocations': records,
    }
