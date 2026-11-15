"""Bounded parsers and comment-preserving document edits."""
from __future__ import annotations

import copy
import io
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import tomlkit
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tokens import AliasToken, AnchorToken, DirectiveToken, TagToken

FORMATS = ('claude', 'codex', 'cline', 'continue', 'vscode')
ROOTS = {'claude': 'mcpServers', 'codex': 'mcp_servers', 'cline': 'mcpServers',
         'continue': 'mcpServers', 'vscode': 'servers'}
MAX_BYTES = 1024 * 1024


class ConfigError(ValueError):
    """A safe diagnostic; never include raw input values in messages."""


def plain(value: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise ConfigError('configuration exceeds maximum nesting depth (64)')
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ConfigError('mapping keys must be strings')
        return {str(k): plain(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v, depth + 1) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    raise ConfigError('only finite JSON-compatible data values are supported')


class JsonParser:
    """JSON/JSONC parser recording value spans for surgical edits (no eval)."""
    def __init__(self, text: str, comments: bool = False):
        self.text, self.i, self.comments = text, 0, comments
        self.spans: dict[tuple, tuple[int, int]] = {}
        self.members: dict[tuple, tuple[int, int]] = {}

    def error(self):
        line = self.text.count('\n', 0, self.i) + 1
        col = self.i - self.text.rfind('\n', 0, self.i)
        raise ConfigError(f'invalid JSON at line {line}, column {col}')

    def skip(self):
        while self.i < len(self.text):
            if self.text[self.i].isspace():
                self.i += 1
            elif self.comments and self.text.startswith('//', self.i):
                end = self.text.find('\n', self.i)
                self.i = len(self.text) if end < 0 else end
            elif self.comments and self.text.startswith('/*', self.i):
                end = self.text.find('*/', self.i + 2)
                if end < 0:
                    self.error()
                self.i = end + 2
            else:
                break

    def value(self, path=(), depth=0):
        if depth > 64:
            raise ConfigError('configuration exceeds maximum nesting depth (64)')
        self.skip()
        start = self.i
        if self.i >= len(self.text):
            self.error()
        ch = self.text[self.i]
        if ch in '{[':
            self.i += 1
            obj = {} if ch == '{' else []
            close = '}' if ch == '{' else ']'
            self.skip()
            if self.i < len(self.text) and self.text[self.i] == close:
                self.i += 1
            else:
                while True:
                    if ch == '{':
                        self.skip()
                        if self.i >= len(self.text) or self.text[self.i] != '"':
                            self.error()
                        key_start = self.i
                        try:
                            key, self.i = json.JSONDecoder().raw_decode(self.text, self.i)
                        except ValueError:
                            self.error()
                        if key in obj:
                            raise ConfigError('duplicate JSON key')
                        self.skip()
                        if self.i >= len(self.text) or self.text[self.i] != ':':
                            self.error()
                        self.i += 1
                        obj[key] = self.value(path + (key,), depth + 1)
                        self.members[path + (key,)] = (key_start, self.i)
                    else:
                        obj.append(self.value(path + (len(obj),), depth + 1))
                    self.skip()
                    if self.i >= len(self.text):
                        self.error()
                    token = self.text[self.i]
                    self.i += 1
                    if token == close:
                        break
                    if token != ',':
                        self.error()
                    self.skip()
                    if self.comments and self.i < len(self.text) and self.text[self.i] == close:
                        self.i += 1
                        break
        else:
            try:
                obj, self.i = json.JSONDecoder().raw_decode(self.text, self.i)
            except ValueError:
                self.error()
        self.spans[path] = (start, self.i)
        return obj

    def parse(self):
        value = self.value()
        self.skip()
        if self.i != len(self.text):
            self.error()
        return plain(value)


def yaml_engine():
    y = YAML(typ='rt')
    y.preserve_quotes = True
    y.allow_duplicate_keys = False
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def dump_yaml(data):
    out = io.StringIO()
    yaml_engine().dump(data, out)
    return out.getvalue()


def overlay(target, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            overlay(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def same_value(left, right):
    """JSON-compatible equality, keeping booleans distinct from numbers."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_value(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def reconcile_mapping(target, desired):
    """Apply a complete resolved map, retaining native trivia on surviving keys."""
    for key in list(target):
        if key not in desired:
            del target[key]
    for key, value in desired.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            reconcile_mapping(target[key], value)
        elif key not in target or not same_value(plain(target[key]), value):
            target[key] = copy.deepcopy(value)


def limit_yaml_comments(value, start, stop):
    """Detach comments that ruamel attributes to this item but live outside its slice."""
    def prune(comment):
        if isinstance(comment, list):
            return [prune(c) for c in comment]
        if comment is not None and hasattr(comment, 'start_mark'):
            line = comment.start_mark.line
            if line < start or line >= stop:
                return None
            comment.value = ''.join(comment.value.splitlines(keepends=True)[:stop - line])
        return comment
    ca = getattr(value, 'ca', None)
    if ca is not None:
        ca.comment = prune(ca.comment)
        ca.end[:] = [c for c in prune(ca.end) if c is not None]
        for key, comments in ca.items.items():
            ca.items[key] = prune(comments)
    if isinstance(value, dict):
        for child in value.values():
            limit_yaml_comments(child, start, stop)
    elif isinstance(value, list):
        for child in value:
            limit_yaml_comments(child, start, stop)


@dataclass
class Document:
    format: str
    text: str
    data: Any
    servers: dict[str, dict]
    spans: dict = field(default_factory=dict)
    members: dict = field(default_factory=dict)

    def render_merged(self, servers: dict[str, dict]) -> str:
        """Apply an exact three-way result, preserving unrelated target content."""
        if same_value(self.servers, servers):
            return self.text
        root = ROOTS[self.format]
        if self.format == 'codex':
            data = copy.deepcopy(self.data)
            if root not in data:
                data[root] = tomlkit.table()
            reconcile_mapping(data[root], servers)
            return tomlkit.dumps(data)
        if self.format == 'continue':
            updates = {n: v for n, v in servers.items()
                       if n not in self.servers or not same_value(v, self.servers[n])}
            return self._yaml_patch(updates, self.servers.keys() - servers.keys(), exact=True)
        # Append new servers with the existing surgical insertion, then edit only
        # changed value spans and removed members. Untouched servers stay verbatim.
        additions = {n: v for n, v in servers.items() if n not in self.servers}
        doc = loads(self.render(additions), self.format) if additions else self
        edits = []
        for name, current in doc.servers.items():
            if name not in servers:
                a, b = doc.members[(root, name)]
                edits.append((a, b, ''))
            elif not same_value(current, servers[name]):
                a, b = doc.spans[(root, name)]
                edits.append((a, b, json.dumps(servers[name], ensure_ascii=False, indent=2)))
        if doc.servers.keys() - servers.keys():
            kept = [n for n in doc.servers if n in servers]
            scanner = JsonParser(doc.text, comments=(self.format == 'vscode'))
            for name in doc.servers:
                scanner.i = doc.spans[(root, name)][1]
                scanner.skip()
                if scanner.i < len(doc.text) and doc.text[scanner.i] == ',':
                    if name not in servers or name == kept[-1]:
                        edits.append((scanner.i, scanner.i + 1, ''))
        result = doc.text
        for a, b, value in sorted(edits, reverse=True):
            result = result[:a] + value + result[b:]
        return result

    def render(self, updates: dict[str, dict]) -> str:
        """Overlay named fields; preserve all unrelated content and native comments."""
        updates = {name: value for name, value in updates.items()
                   if name not in self.servers or any(self.servers[name].get(k) != v
                                                      for k, v in value.items())}
        if not updates:
            return self.text
        root = ROOTS[self.format]
        if self.format == 'codex':
            data = copy.deepcopy(self.data)
            if root not in data:
                data[root] = tomlkit.table()
            for name, value in updates.items():
                if name not in data[root]:
                    data[root][name] = tomlkit.table()
                overlay(data[root][name], value)
            return tomlkit.dumps(data)
        if self.format == 'continue':
            return self._yaml_patch(updates)
        edits = []

        def patch(path, current, update):
            if not isinstance(current, dict) or not isinstance(update, dict):
                a, b = self.spans[path]
                edits.append((a, b, json.dumps(update, ensure_ascii=False, indent=2)))
                return
            additions = {}
            for key, value in update.items():
                if key in current:
                    if current[key] != value:
                        patch(path + (key,), current[key], value)
                else:
                    additions[key] = value
            if additions:
                if current:
                    at = self.spans[path + (next(reversed(current)),)][1]
                    prefix = ','
                else:
                    at = self.spans[path][0] + 1
                    prefix = ''
                body = ',\n'.join(json.dumps(k, ensure_ascii=False) + ': ' +
                                  json.dumps(v, ensure_ascii=False, indent=2)
                                  for k, v in additions.items())
                edits.append((at, at, prefix + '\n' + body + '\n'))
        patch((), self.data, {root: updates})
        result = self.text
        for a, b, text in sorted(edits, reverse=True):
            result = result[:a] + text + result[b:]
        return result

    def _yaml_patch(self, updates, removals=(), *, exact=False):
        # Slice only touched block sequence items. Never normalize unrelated YAML.
        seq = self.data.get('mcpServers')
        if seq is None:
            return self.text + ('' if self.text.endswith('\n') else '\n') + dump_yaml({'mcpServers': [
                {'name': n, **v} for n, v in updates.items()]})
        if not seq:
            line, col = self.data.lc.value('mcpServers')
            lines = self.text.splitlines(keepends=True)
            empty = re.match(r'\[\s*\]', lines[line][col:])
            if not empty:
                raise ConfigError('empty YAML mcpServers must use [] for merging')
            suffix = lines[line][col + empty.end():].rstrip('\r\n')
            block = dump_yaml({'mcpServers': [{'name': n, **v} for n, v in updates.items()]})
            lines[line] = lines[line][:col].rstrip() + suffix + '\n' + block.split('\n', 1)[1]
            return ''.join(lines)
        if seq.fa.flow_style() or any(item.fa.flow_style() for item in seq):
            raise ConfigError('YAML merge requires block-style mcpServers and entries')
        lines = self.text.splitlines(keepends=True)
        scalar_lines = set()
        for token in yaml_engine().scan(self.text):
            if getattr(token, 'style', None) in ('|', '>'):
                scalar_lines.update(range(token.start_mark.line + 1, token.end_mark.line))
        def boundary_line(index):
            return index not in scalar_lines and (not lines[index].strip() or
                                                  lines[index].lstrip().startswith('#'))
        starts = [seq.lc.item(i)[0] for i in range(len(seq))]
        # Locate end using the next root-level key; comments before it stay untouched.
        next_lines = [self.data.lc.key(k)[0] for k in self.data
                      if self.data.lc.key(k)[0] > starts[-1]]
        end = min(next_lines, default=len(lines))
        while end > starts[-1] and boundary_line(end - 1):
            end -= 1
        edits = []
        pending = dict(updates)
        indent = len(lines[starts[0]]) - len(lines[starts[0]].lstrip())
        empty_indent = max(indent, self.data.lc.key('mcpServers')[1] + 2)
        for i, item in enumerate(seq):
            name = item['name']
            if name not in pending and name not in removals:
                continue
            stop = starts[i+1] if i+1 < len(seq) else end
            # Keep trailing comment/blank lines attached to the following entry.
            while stop > starts[i] and boundary_line(stop - 1):
                stop -= 1
            if name in removals:
                emitted = (' ' * empty_indent + '[]\n'
                           if len(removals) == len(seq) and not updates and i == 0 else '')
            else:
                updated = copy.deepcopy(item)
                if exact:
                    reconcile_mapping(updated, {'name': name, **pending.pop(name)})
                else:
                    overlay(updated, pending.pop(name))
                limit_yaml_comments(updated, starts[i], stop)
                emitted = dump_yaml([updated])
                emitted = ''.join(' ' * indent + line.removeprefix('  ') for line in emitted.splitlines(keepends=True))
            edits.append((starts[i], stop, emitted))
        if pending:
            emitted = dump_yaml([{'name': n, **v} for n, v in pending.items()])
            emitted = ''.join(' ' * indent + line.removeprefix('  ') for line in emitted.splitlines(keepends=True))
            if end and not lines[end - 1].endswith(('\n', '\r')):
                emitted = '\n' + emitted
            edits.append((end, end, emitted))
        for a, b, value in sorted(edits, reverse=True):
            lines[a:b] = [value]
        return ''.join(lines)


def loads(text: str, fmt: str) -> Document:
    if fmt not in FORMATS:
        raise ConfigError('unknown format')
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ConfigError('configuration exceeds 1 MiB limit')
    spans = {}
    members = {}
    try:
        if fmt == 'codex':
            data = tomlkit.parse(text)
        elif fmt == 'continue':
            y = yaml_engine()
            for token in y.scan(text):
                if isinstance(token, (AliasToken, AnchorToken, DirectiveToken, TagToken)):
                    raise ConfigError('YAML aliases, anchors, directives and explicit tags are unsupported')
            data = y.load(text)
        else:
            parser = JsonParser(text, comments=(fmt == 'vscode'))
            data = parser.parse()
            spans = parser.spans
            members = parser.members
    except (YAMLError, tomlkit.exceptions.ParseError) as exc:
        mark = getattr(exc, 'problem_mark', None)
        line = mark.line + 1 if mark else getattr(exc, 'line', 1)
        col = mark.column + 1 if mark else getattr(exc, 'col', 1)
        raise ConfigError(f'invalid {fmt} at line {line}, column {col}') from None
    except RecursionError:
        raise ConfigError('configuration exceeds maximum nesting depth (64)') from None
    normalized = plain(data)
    if not isinstance(normalized, dict):
        raise ConfigError('configuration root must be an object')
    root = ROOTS[fmt]
    entries = normalized.get(root, [] if fmt == 'continue' else {})
    servers = {}
    if fmt == 'continue':
        if not isinstance(entries, list):
            raise ConfigError('mcpServers must be a list')
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
                raise ConfigError('Continue requires native named servers; hub uses/override entries unsupported')
            name = entry['name']
            if name in servers:
                raise ConfigError('duplicate Continue server name')
            servers[name] = {k: v for k, v in entry.items() if k != 'name'}
    else:
        if not isinstance(entries, dict):
            raise ConfigError('server collection must be an object')
        servers = entries
    for name, server in servers.items():
        if not name or len(name) > 256 or any(ord(c) < 32 for c in name):
            raise ConfigError('server names must contain 1–256 printable characters')
        if not isinstance(server, dict):
            raise ConfigError('each server must be an object')
    return Document(fmt, text, data, servers, spans, members)


def new_document(fmt: str, servers: dict[str, dict]) -> str:
    root = ROOTS[fmt]
    if fmt == 'codex':
        d = tomlkit.document()
        d[root] = tomlkit.table()
        for name, server in servers.items():
            d[root][name] = server
        return tomlkit.dumps(d)
    if fmt == 'continue':
        return dump_yaml({'name': 'MCP bridge', 'version': '1.0.0', 'schema': 'v1',
                          root: [{'name': n, **v} for n, v in servers.items()]})
    return json.dumps({root: servers}, indent=2, ensure_ascii=False) + '\n'
