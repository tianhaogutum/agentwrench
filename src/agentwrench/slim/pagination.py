"""Cursor-graph tracking that rejects tools/list pagination loops."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .bounds import validate_json_tree
from .config import PROGRAM, InputError
from .jsjson import MISSING, stringify

MAX_PAGINATION_ENTRIES = 10000


class PaginationError(InputError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _cursor_key(value: Any) -> str | None:
    # JSON encoding distinguishes lone UTF-16 surrogates from replacement chars.
    return None if value is MISSING else _digest(stringify(value))


def _canonical(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    return value


def _without_progress(value: Any) -> Any:
    """A progress token identifies a request, not a different catalogue. All other
    opaque params/metadata participate in the scope, independent of key order."""
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key != 'progressToken'}


def _scope_key(message: dict[str, Any]) -> str:
    params = {key: value for key, value in message.get('params', {}).items() if key != 'cursor'}
    if '_meta' in params:
        params['_meta'] = _without_progress(params['_meta'])
        if isinstance(params['_meta'], dict) and not params['_meta']:
            del params['_meta']
    meta = _without_progress(message.get('_meta', MISSING))
    scope: dict[str, Any] = {'params': params}
    if not (meta is MISSING or (isinstance(meta, dict) and not meta)):
        scope['meta'] = meta
    return _digest(stringify(_canonical(scope)))


@dataclass
class PageDescriptor:
    """Descriptors retain only hashes and an epoch, never an obsolete graph."""

    scope: str
    generation: object
    cursor: str | None


@dataclass
class _Graph:
    generation: object = field(default_factory=object)
    edges: dict[str, str] = field(default_factory=dict)


class PaginationTracker:
    """Track accepted cursor -> nextCursor edges, not tool contents. Retrying an
    identical page is allowed; inserting an edge that closes a cycle is not."""

    def __init__(self) -> None:
        self._scopes: dict[str, _Graph] = {}
        self._edges = 0

    def clear(self) -> None:
        self._scopes.clear()
        self._edges = 0

    def begin(self, message: dict[str, Any]) -> PageDescriptor:
        params = message.get('params', MISSING)
        if params is not MISSING and not isinstance(params, dict):
            raise InputError('tools/list params: expected an object')
        if params is not MISSING and 'cursor' in params and not isinstance(params['cursor'], str):
            raise InputError('tools/list params.cursor: expected a string')
        validate_json_tree(None if params is MISSING else params)
        validate_json_tree(message.get('_meta'))
        scope = _scope_key(message)
        cursor = params.get('cursor', MISSING) if params is not MISSING else MISSING
        graph = self._scopes.get(scope)
        if graph is None or cursor is MISSING:
            if graph is None and len(self._scopes) >= MAX_PAGINATION_ENTRIES:
                raise PaginationError(-32000, f'{PROGRAM} pagination context limit reached')
            if graph is not None:
                self._edges -= len(graph.edges)
            graph = _Graph()
            self._scopes[scope] = graph
        return PageDescriptor(scope, graph.generation, _cursor_key(cursor))

    def observe(self, request: PageDescriptor, result: dict[str, Any]) -> None:
        if 'nextCursor' in result and not isinstance(result['nextCursor'], str):
            raise InputError('tools/list result.nextCursor: expected a string')
        cursor = request.cursor
        following = _cursor_key(result.get('nextCursor', MISSING))
        if cursor is None:
            return
        if following == cursor:
            raise PaginationError(-32603, f'{PROGRAM} rejected tools/list pagination loop')
        graph = self._scopes.get(request.scope)
        # Replies begun before a restart/list_changed still get filtered and checked
        # for self-loops, but cannot repopulate a new catalogue's cursor history.
        if graph is None or graph.generation is not request.generation:
            return
        if following is None:
            if graph.edges.pop(cursor, None) is not None:
                self._edges -= 1
            return
        if graph.edges.get(cursor) == following:
            return
        node: str | None = following
        while node is not None:
            if node == cursor:
                raise PaginationError(-32603, f'{PROGRAM} rejected tools/list pagination loop')
            node = graph.edges.get(node)
        if cursor not in graph.edges:
            if self._edges >= MAX_PAGINATION_ENTRIES:
                raise PaginationError(-32603, f'{PROGRAM} pagination page limit reached')
            self._edges += 1
        graph.edges[cursor] = following
