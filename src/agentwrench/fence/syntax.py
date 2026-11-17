"""Tree-sitter TypeScript parsing, symbol selectors and CST event streams.

Sources are parsed as UTF-16 so that every index, column and text slice is in
UTF-16 code units, exactly as the original Node.js binding reported them.
"""
from __future__ import annotations

import bisect
import hashlib
from json.encoder import encode_basestring
import re
import sys
import time
import warnings

from .errors import JS_WHITESPACE, require_that, utf16_slice
from .patch import is_well_formed, utf8_length

DECLARATIONS = frozenset({
    "function_declaration", "generator_function_declaration", "function_signature",
    "function_expression", "generator_function", "class_declaration",
    "abstract_class_declaration", "class", "interface_declaration",
    "type_alias_declaration", "enum_declaration", "internal_module", "module",
    "variable_declarator", "method_definition", "method_signature",
    "abstract_method_signature", "public_field_definition", "property_signature",
    "enum_assignment", "import_alias",
})
SIMPLE_NAMES = frozenset({
    "identifier", "type_identifier", "property_identifier",
    "private_property_identifier", "nested_identifier", "nested_type_identifier",
})
MAX_SOURCE_BYTES = 1024 * 1024
MAX_NODES = 200_000
MAX_DEPTH = 512
PARSE_TIMEOUT_SECONDS = 1.0
COMMENT_KINDS = frozenset({"comment", "hash_bang_line"})
_NON_WHITESPACE = re.compile(f"[^{JS_WHITESPACE}]")
_LINE_BREAK = re.compile("[\r\n  ]")

_LANGUAGES: dict[bool, object] = {}


def _language(tsx: bool):
    language = _LANGUAGES.get(tsx)
    if language is None:
        import tree_sitter
        import tree_sitter_typescript

        capsule = tree_sitter_typescript.language_tsx() if tsx else tree_sitter_typescript.language_typescript()
        language = _LANGUAGES[tsx] = tree_sitter.Language(capsule)
    return language


def _parse_tree(language, data: bytes, encoding: str):
    """Parse with a one-second budget; None when the budget is exhausted.

    py-tree-sitter 0.23-0.25 offer ``Parser.timeout_micros`` (deprecated in
    0.25); 0.26 removed it in favour of a progress callback, which only applies
    when the source is supplied through a read callback.
    """
    import tree_sitter

    parser = tree_sitter.Parser(language)  # a fresh parser: no state from a cancelled parse
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy_timeout = hasattr(parser, "timeout_micros")
        if legacy_timeout:
            parser.timeout_micros = int(PARSE_TIMEOUT_SECONDS * 1_000_000)
    if legacy_timeout:
        try:
            return parser.parse(data, encoding=encoding)
        except ValueError:
            return None
    deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS

    def read(offset, _point):
        return data[offset:offset + 64 * 1024]

    def progress(*_state):
        return time.monotonic() > deadline

    try:
        return parser.parse(read, encoding=encoding, progress_callback=progress)
    except ValueError:
        return None


_EXPLICIT_LE: list[bool] = []


def _supports_explicit_byte_order(language) -> bool:
    """py-tree-sitter >= 0.25 accepts 'utf16le'; older versions only native-order 'utf16'."""
    if not _EXPLICIT_LE:
        import tree_sitter

        try:
            tree_sitter.Parser(language).parse(b"", encoding="utf16le")
            _EXPLICIT_LE.append(True)
        except ValueError:
            _EXPLICIT_LE.append(False)
    return _EXPLICIT_LE[0]


def _parse_utf16(language, source: str):
    if sys.byteorder == "little":
        return _parse_tree(language, source.encode("utf-16-le"), "utf16")
    if _supports_explicit_byte_order(language):
        return _parse_tree(language, source.encode("utf-16-le"), "utf16le")
    # Byte offsets are identical in either byte order; slices use the LE copy.
    return _parse_tree(language, source.encode("utf-16-be"), "utf16")


class ParsedSource:
    """A parsed baseline or candidate: source text, tree, symbols and UTF-16 line starts."""

    def __init__(self, source: str, file: str, tree, data: bytes):
        self.source = source
        self.file = file
        self.tree = tree
        self.data = data  # UTF-16-LE bytes of ``source``; byte offset = 2 * index
        self.symbols: list[dict] = []
        self.groups: dict[str, list[dict]] = {}
        self.line_starts: list[int] = [0]
        self.length = len(data) // 2

    def slice(self, start: int, end: int) -> str:
        return self.data[2 * start:2 * end].decode("utf-16-le", "surrogatepass")

    def text(self, node) -> str:
        return self.data[node.start_byte:node.end_byte].decode("utf-16-le", "surrogatepass")


def start_index(node) -> int:
    return node.start_byte // 2


def end_index(node) -> int:
    return node.end_byte // 2


def node_range(node) -> dict:
    # Tree-sitter columns are byte offsets; with UTF-16 input, two bytes per unit.
    start, end = node.start_point, node.end_point
    return {
        "start": {"line": start.row + 1, "column": start.column // 2 + 1, "index": node.start_byte // 2},
        "end": {"line": end.row + 1, "column": end.column // 2 + 1, "index": node.end_byte // 2},
    }


def _is_default_value(node, parent) -> bool:
    if parent is None or parent.type != "export_statement":
        return False
    value = parent.child_by_field_name("value")
    return value is not None and value.id == node.id and any(child.type == "default" for child in parent.children)


def _local_name(parsed: ParsedSource, node, parent):
    name = node.child_by_field_name("key" if node.type == "pair" else "name")
    if name is None and node.type == "import_alias":
        name = node.named_children[0] if node.named_child_count else None
    if name is None:
        return "default" if _is_default_value(node, parent) else None
    if name.type in SIMPLE_NAMES or name.type == "computed_property_name":
        return parsed.text(name)
    if name.type in ("string", "number"):
        return f"[{parsed.text(name)}]"
    return None  # Destructuring requires selecting a named enclosing declaration.


def _qualify(parent: str, name: str) -> str:
    return f"{parent}{'' if name.startswith('[') else '.'}{name}" if parent else name


def parse_source(source, file: str = "input.ts") -> ParsedSource:
    require_that(isinstance(source, str), "invalid_source", f"Baseline reader must return text for {file}")
    require_that(is_well_formed(source), "invalid_encoding", f"{file} contains an unpaired UTF-16 surrogate")
    require_that(utf8_length(source) <= MAX_SOURCE_BYTES, "source_limit", f"{file} exceeds the 1 MiB source limit")
    require_that("\0" not in source, "invalid_source", f"{file} contains a NUL byte")
    tree = _parse_utf16(_language(file.endswith(".tsx")), source)
    require_that(tree is not None, "parse_timeout", f"Tree-sitter timed out parsing {file}")
    parsed = ParsedSource(source, file, tree, source.encode("utf-16-le"))
    symbols = parsed.symbols
    errors = []
    stack = [(tree.root_node, None, "", 0)]
    count = 0
    while stack:
        node, parent, scope, depth = stack.pop()
        count += 1
        require_that(count <= MAX_NODES and depth <= MAX_DEPTH, "syntax_limit", f"{file} exceeds the syntax complexity limit")
        if node.is_error or node.is_missing:
            errors.append({"kind": node.type, "missing": node.is_missing, "range": node_range(node)})
        child_scope = scope
        kind = node.type
        if kind in DECLARATIONS or kind == "pair" or _is_default_value(node, parent):
            name = _local_name(parsed, node, parent)
            if name:
                child_scope = _qualify(scope, name)
                symbols.append({"name": child_scope, "kind": kind, "range": node_range(node), "node": node})
        children = node.named_children
        for child in reversed(children):
            stack.append((child, node, child_scope, depth + 1))
    require_that(not tree.root_node.has_error and not errors, "syntax_error",
                 f"Cannot establish a boundary for invalid syntax in {file}", {"file": file, "errors": errors[:20]})
    groups = parsed.groups
    for symbol in symbols:
        groups.setdefault(symbol["name"], []).append(symbol)
    for group in groups.values():
        for i, symbol in enumerate(group):
            symbol["selector"] = symbol["name"] if len(group) == 1 else f"{symbol['name']}#{i + 1}"
    starts = parsed.line_starts
    data = parsed.data
    offset = data.find(b"\n\x00")
    while offset != -1:
        if offset % 2:  # straddles two code units; not a line feed
            offset = data.find(b"\n\x00", offset + 1)
            continue
        starts.append(offset // 2 + 1)
        offset = data.find(b"\n\x00", offset + 2)
    return parsed


def source_range(parsed: ParsedSource, start: int, end: int) -> dict:
    def point(index: int) -> dict:
        line = bisect.bisect_right(parsed.line_starts, index) - 1
        line = max(line, 0)
        return {"line": line + 1, "column": index - parsed.line_starts[line] + 1, "index": index}

    return {"start": point(start), "end": point(end)}


def list_symbols(parsed: ParsedSource) -> list[dict]:
    return [{key: value for key, value in symbol.items() if key != "node"} for symbol in parsed.symbols]


def resolve_symbol(parsed: ParsedSource, selector: str) -> dict:
    exact = [symbol for symbol in parsed.symbols if symbol["selector"] == selector]
    require_that(len(exact) == 1, "unknown_symbol",
                 f"Symbol {parsed.file}::{selector} is missing or ambiguous; run symbols to list exact selectors", {
                     "file": parsed.file, "selector": selector,
                     "candidates": [symbol["selector"] for symbol in parsed.symbols
                                    if symbol["name"] == selector or symbol["selector"].startswith(f"{selector}#")],
                 })
    return exact[0]


class Event:
    """One CST event. ``key`` is the JSON-encoded identity compared across versions."""

    __slots__ = ("key", "kind", "start", "end", "category", "text", "gap_index")

    def __init__(self, key, kind, start, end, category, text, gap_index=None):
        self.key = key
        self.kind = kind
        self.start = start
        self.end = end
        self.category = category
        self.text = text
        self.gap_index = gap_index

    def __repr__(self):
        return f"Event({self.key}, {self.kind}, {self.start}-{self.end}, {self.category})"


# Keys are the compact JSON arrays JSON.stringify produced in the original
# (for example ``["leaf","identifier","x"]``), built directly for speed.
_string = encode_basestring
_BOUNDARY_KEYS = {True: '["comment-line-boundary",true]', False: '["comment-line-boundary",false]'}


# A CST event stream retains grammar structure, punctuation, comments and exact
# leaf spelling. Whitespace is emitted only from gaps between parser children;
# whitespace inside strings, templates and comments always remains significant.
# Placeholders preserve the selected node's position in its parent structure.
def syntax_events(parsed: ParsedSource, selected=(), include_whitespace: bool = False, sink=None,
                  trivia_only: bool = False) -> list[Event]:
    by_id = {symbol["node"].id: symbol for symbol in selected}
    events: list[Event] = []
    gap_counter = 0

    def emit(encoded, node, category="syntax", text="", start=None, end=None, gap_index=None):
        if sink is not None:
            sink(encoded)
            return
        events.append(Event(encoded, node.type, node.start_byte // 2 if start is None else start,
                            node.end_byte // 2 if end is None else end, category,
                            text if len(text) <= 80 else utf16_slice(text, 0, 160), gap_index))

    def gap(start, end, node, previous=None, following=None):
        nonlocal gap_counter
        ordinal = gap_counter
        gap_counter += 1
        text = parsed.slice(start, end)
        comment = (previous if previous is not None and previous.type in COMMENT_KINDS
                   else following if following is not None and following.type in COMMENT_KINDS else None)
        # Compiler/linter directives and JSDoc can change meaning when an inline
        # comment becomes a standalone line even though Tree-sitter's CST is equal.
        if comment is not None and not trivia_only:
            emit(_BOUNDARY_KEYS[_LINE_BREAK.search(text) is not None], comment, "comment_boundary", text, start, end)
        if start == end:
            return
        # Retain any unexpected non-whitespace gap even in whitespace mode.
        if include_whitespace or _NON_WHITESPACE.search(text):
            key = f'["gap",{ordinal},{_string(text)}]' if trivia_only else f'["gap",{_string(text)}]'
            emit(key, node, "whitespace", text, start, end, ordinal if trivia_only else None)

    def enter(node):
        symbol = by_id.get(node.id)
        if symbol is not None:
            if not trivia_only:
                emit(f'["allowed",{_string(symbol["selector"])}]', node, "allowed")
            return None
        children = node.children
        if not children:
            if not trivia_only:
                text = parsed.text(node)
                emit(f'["leaf",{_string(node.type)},{_string(text)}]', node, "syntax", text)
            return None
        if not trivia_only:
            emit(f'["open",{_string(node.type)}]', node)
        return [node, children, 0, node.start_byte // 2]

    root = parsed.tree.root_node
    gap(0, root.start_byte // 2, root)
    # Iterative pre-order walk (the original recursed; depth is bounded by MAX_DEPTH).
    stack = []
    frame = enter(root)
    if frame is not None:
        stack.append(frame)
    while stack:
        frame = stack[-1]
        node, children, i, position = frame
        if i < len(children):
            child = children[i]
            gap(position, child.start_byte // 2, node, children[i - 1] if i > 0 else None, child)
            if not trivia_only:
                emit(f'["field",{_string(node.field_name_for_child(i) or "")}]', child)
            frame[2] = i + 1
            frame[3] = child.end_byte // 2
            child_frame = enter(child)
            if child_frame is not None:
                stack.append(child_frame)
            continue
        gap(position, node.end_byte // 2, node, children[-1])
        if not trivia_only:
            emit(f'["close",{_string(node.type)}]', node)
        stack.pop()
    gap(root.end_byte // 2, parsed.length, root)
    return events


def syntax_fingerprint(parsed: ParsedSource, selected=()) -> str:
    digest = hashlib.sha256()

    def sink(key: str) -> None:
        digest.update(key.encode("utf-8", "surrogatepass"))
        digest.update(b"\n")

    syntax_events(parsed, selected, False, sink)
    return digest.hexdigest()


def trivia_events(parsed: ParsedSource, selected=()) -> list[Event]:
    return syntax_events(parsed, selected, True, None, True)


def same_events(left: list[Event], right: list[Event]) -> bool:
    return len(left) == len(right) and all(a.key == b.key for a, b in zip(left, right))


# Compare source partitions instead of allocating a second pair of complete
# CST event streams just to establish that outside whitespace is unchanged.
# Alternating text/selector parts avoid sentinel collisions with source text.
def same_outside_text(before: ParsedSource, after: ParsedSource, old_selected, new_selected) -> bool:
    def parts(parsed: ParsedSource, selected) -> list:
        ordered = sorted(selected, key=lambda symbol: symbol["node"].start_byte)
        result = []
        start = 0
        for symbol in ordered:
            result.append(parsed.data[start:symbol["node"].start_byte])
            result.append(symbol["selector"])
            start = symbol["node"].end_byte
        result.append(parsed.data[start:])
        return result

    return parts(before, old_selected) == parts(after, new_selected)
