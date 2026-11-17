"""The subset of jsdiff (npm ``diff`` 9.0.0) that the gate relies on, in pure Python.

Reimplemented behaviour, kept line-for-line close to jsdiff so that patch
interpretation and diagnostics match the original Node.js tool:

* ``parse_patch``: jsdiff ``parsePatch`` (Git extended headers, quoted names,
  hunk parsing and its line-count sanity checks). The gate adds its own strict
  envelope validation on top (see ``patch.py``).
* ``apply_patch``: jsdiff ``applyPatch`` restricted to ``fuzzFactor: 0`` and
  ``autoConvertLineEndings: false`` (the only options the gate uses).
* ``diff_arrays``: jsdiff ``diffArrays``, a Myers O(ND) diff with jsdiff's
  diagonal pruning, ``maxEditLength`` and ``timeout`` bounds.

jsdiff is Copyright (c) 2009-2015, Kevin Decker, BSD-3-Clause.
"""
from __future__ import annotations

import math
import re
import time

from .errors import JS_WHITESPACE, js_trim

_S = f"[{JS_WHITESPACE}]"
# JavaScript's ``.`` excludes all ECMAScript line terminators, not only ``\n``.
_DOT = "[^\n\r  ]"
_INDEX_HEADER = re.compile(rf"Index:{_S}")
_HG_HEADER = re.compile(rf"diff(?: -r [A-Za-z0-9_]+)+{_S}")
_HEADER_PREFIX = re.compile(rf"(?:Index:|diff(?: -r [A-Za-z0-9_]+)+){_S}+")
_FILE_HEADER = re.compile(rf"(---|\+\+\+){_S}")
_FILE_HEADER_SPACES = re.compile(rf"(---|\+\+\+){_S}+")
_HUNK_HEADER = re.compile(rf"@@{_S}")
_CHUNK_HEADER = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@")
_RENAME_FROM = re.compile(f"rename from ({_DOT}*)")
_RENAME_TO = re.compile(f"rename to ({_DOT}*)")
_COPY_FROM = re.compile(f"copy from ({_DOT}*)")
_COPY_TO = re.compile(f"copy to ({_DOT}*)")
_NEW_FILE_MODE = re.compile(r"new file mode ([0-9]+)")
_DELETED_FILE_MODE = re.compile(r"deleted file mode ([0-9]+)")
_OLD_MODE = re.compile(r"old mode ([0-9]+)")
_NEW_MODE = re.compile(r"new mode ([0-9]+)")
_UNDERLINE = "=" * 67


class JsdiffError(ValueError):
    """A patch that jsdiff's parser itself rejects."""


class Hunk:
    __slots__ = ("old_start", "old_lines", "new_start", "new_lines", "lines")

    def __init__(self, old_start, old_lines, new_start, new_lines, lines=None):
        # Starts are ints, or NaN when jsdiff could not read the hunk header.
        self.old_start = old_start
        self.old_lines = old_lines
        self.new_start = new_start
        self.new_lines = new_lines
        self.lines: list[str] = [] if lines is None else lines

    def __repr__(self):
        return (f"Hunk(-{self.old_start},{self.old_lines} +{self.new_start},{self.new_lines}, "
                f"{len(self.lines)} lines)")


class FilePatch:
    """One file section of a parsed patch (jsdiff's structured patch index)."""

    def __init__(self):
        self.index: str | None = None
        self.old_file_name: str | None = None
        self.new_file_name: str | None = None
        self.old_header: str | None = None
        self.new_header: str | None = None
        self.hunks: list[Hunk] = []
        self.is_git = False
        self.is_rename = False
        self.is_copy = False
        self.is_create = False
        self.is_delete = False
        self.is_binary = False
        self.old_mode: str | None = None
        self.new_mode: str | None = None
        self.path: str | None = None  # set by the gate after validation

    def __repr__(self):
        return f"FilePatch({self.old_file_name!r} -> {self.new_file_name!r}, {len(self.hunks)} hunks)"


def _is_git_diff_header(line: str) -> bool:
    return line.startswith("diff --git ")


def _is_diff_header(line: str) -> bool:
    return (_is_git_diff_header(line) or _INDEX_HEADER.match(line) is not None
            or _HG_HEADER.match(line) is not None)


def _is_file_header(line: str) -> bool:
    return _FILE_HEADER.match(line) is not None


def _is_hunk_header(line: str) -> bool:
    return _HUNK_HEADER.match(line) is not None


def _decode_octal_bytes(values: list[int]) -> str:
    # WHATWG TextDecoder('utf-8'): replacement characters, leading BOM removed.
    text = bytes(values).decode("utf-8", "replace")
    return text[1:] if text.startswith("﻿") else text


_ESCAPES = {"a": "\x07", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"'}


def parse_quoted_file_name(s: str):
    """C-style quoted name as written by Git: ``(name, raw_length)`` or None."""
    if not s.startswith('"'):
        return None

    def at(k: int) -> str:
        return s[k] if 0 <= k < len(s) else ""

    def octal(ch: str) -> bool:
        return ch != "" and "0" <= ch <= "7"

    result = []
    j = 1
    while j < len(s):
        if s[j] == '"':
            return "".join(result), j + 1
        if s[j] == "\\" and j + 1 < len(s):
            j += 1
            ch = s[j]
            if ch in _ESCAPES:
                result.append(_ESCAPES[ch])
            elif "0" <= ch <= "7":
                if j + 2 >= len(s) or not octal(at(j + 1)) or not octal(at(j + 2)):
                    return None
                values = [int(s[j:j + 3], 8)]
                j += 3
                while at(j) == "\\" and octal(at(j + 1)):
                    if j + 3 >= len(s) or not octal(at(j + 2)) or not octal(at(j + 3)):
                        return None
                    values.append(int(s[j + 1:j + 4], 8))
                    j += 4
                # Values above 0o377 wrap like a JavaScript Uint8Array.
                result.append(_decode_octal_bytes([value & 0xFF for value in values]))
                continue
            else:
                return None
        else:
            result.append(s[j])
        j += 1
    return None


def _unquote_if_quoted(s: str) -> str:
    if s.startswith('"'):
        parsed = parse_quoted_file_name(s)
        if parsed:
            return parsed[0]
    return s


def _parse_git_diff_header(line: str):
    rest = line[len("diff --git "):]
    if rest.startswith('"'):
        old_path = parse_quoted_file_name(rest)
        if old_path is None:
            return None
        after_old = rest[old_path[1] + 1:]
        if after_old.startswith('"'):
            new_path = parse_quoted_file_name(after_old)
            if new_path is None:
                return None
            new_name = new_path[0]
        else:
            new_name = after_old
        return old_path[0], new_name
    quote = rest.find('"')
    if quote > 0:
        old_name = rest[:quote - 1]
        new_path = parse_quoted_file_name(rest[quote:])
        if new_path is None:
            return None
        return old_name, new_path[0]
    if rest.startswith("a/"):
        splits = []
        idx = 0
        while True:
            idx = rest.find(" b/", idx + 1)
            if idx == -1:
                break
            splits.append(idx)
        if splits:
            mid = splits[len(splits) // 2]
            return rest[:mid], rest[mid + 1:]
    return None


def parse_patch(uni_diff: str) -> list[FilePatch]:
    """Port of jsdiff ``parsePatch``. Raises :class:`JsdiffError` like jsdiff throws."""
    diffstr = uni_diff.split("\n")
    result: list[FilePatch] = []
    i = 0

    def parse_file_header(index: FilePatch) -> None:
        nonlocal i
        if i >= len(diffstr):
            return
        match = _FILE_HEADER_SPACES.match(diffstr[i])
        if not match:
            return
        prefix = match.group(1)
        data = js_trim(diffstr[i][3:]).split("\t")[:2]
        header = js_trim(data[1] if len(data) > 1 and data[1] else "")
        file_name = data[0]
        if file_name.startswith('"'):
            file_name = _unquote_if_quoted(file_name)
        else:
            file_name = file_name.replace("\\\\", "\\")
        if prefix == "---":
            index.old_file_name = file_name
            index.old_header = header
        else:
            index.new_file_name = file_name
            index.new_header = header
        i += 1

    def parse_hunk() -> Hunk:
        nonlocal i
        header_index = i
        header_line = diffstr[i]
        i += 1
        match = _CHUNK_HEADER.search(header_line)
        if match:
            hunk = Hunk(int(match.group(1)), 1 if match.group(2) is None else int(match.group(2)),
                        int(match.group(3)), 1 if match.group(4) is None else int(match.group(4)))
        else:
            hunk = Hunk(math.nan, 1, math.nan, 1)
        if hunk.old_lines == 0:
            hunk.old_start += 1
        if hunk.new_lines == 0:
            hunk.new_start += 1
        add_count = remove_count = 0
        while i < len(diffstr) and (remove_count < hunk.old_lines or add_count < hunk.new_lines
                                    or diffstr[i].startswith("\\")):
            line = diffstr[i]
            operation = " " if (len(line) == 0 and i != len(diffstr) - 1) else line[:1]
            if operation in ("+", "-", " ", "\\"):
                hunk.lines.append(line)
                if operation == "+":
                    add_count += 1
                elif operation == "-":
                    remove_count += 1
                elif operation == " ":
                    add_count += 1
                    remove_count += 1
            else:
                raise JsdiffError(f"Hunk at line {header_index + 1} contained invalid line {line}")
            i += 1
        if not add_count and hunk.new_lines == 1:
            hunk.new_lines = 0
        if not remove_count and hunk.old_lines == 1:
            hunk.old_lines = 0
        if add_count != hunk.new_lines:
            raise JsdiffError(f"Added line count did not match for hunk at line {header_index + 1}")
        if remove_count != hunk.old_lines:
            raise JsdiffError(f"Removed line count did not match for hunk at line {header_index + 1}")
        if (i < len(diffstr) and diffstr[i] and diffstr[i][0] in "+ -"
                and not _is_file_header(diffstr[i])):
            raise JsdiffError(f"Hunk at line {header_index + 1} has more lines than expected "
                              f"(expected {hunk.old_lines} old lines and {hunk.new_lines} new lines)")
        return hunk

    def parse_index() -> None:
        nonlocal i
        index = FilePatch()
        result.append(index)
        seen_diff_header = False
        while i < len(diffstr):
            line = diffstr[i]
            if _is_file_header(line) or _is_hunk_header(line):
                break
            if _is_git_diff_header(line):
                if seen_diff_header:
                    return
                seen_diff_header = True
                index.is_git = True
                paths = _parse_git_diff_header(line)
                if paths:
                    index.old_file_name, index.new_file_name = paths
                i += 1
                while i < len(diffstr):
                    ext = diffstr[i]
                    if _is_file_header(ext) or _is_hunk_header(ext) or _is_diff_header(ext):
                        break
                    if m := _RENAME_FROM.match(ext):
                        index.old_file_name = "a/" + _unquote_if_quoted(m.group(1))
                        index.is_rename = True
                    if m := _RENAME_TO.match(ext):
                        index.new_file_name = "b/" + _unquote_if_quoted(m.group(1))
                        index.is_rename = True
                    if m := _COPY_FROM.match(ext):
                        index.old_file_name = "a/" + _unquote_if_quoted(m.group(1))
                        index.is_copy = True
                    if m := _COPY_TO.match(ext):
                        index.new_file_name = "b/" + _unquote_if_quoted(m.group(1))
                        index.is_copy = True
                    if m := _NEW_FILE_MODE.match(ext):
                        index.is_create = True
                        index.new_mode = m.group(1)
                    if m := _DELETED_FILE_MODE.match(ext):
                        index.is_delete = True
                        index.old_mode = m.group(1)
                    if m := _OLD_MODE.match(ext):
                        index.old_mode = m.group(1)
                    if m := _NEW_MODE.match(ext):
                        index.new_mode = m.group(1)
                    if ext.startswith("Binary files "):
                        index.is_binary = True
                    i += 1
                continue
            elif _is_diff_header(line):
                if seen_diff_header:
                    return
                seen_diff_header = True
                if m := _HEADER_PREFIX.match(line):
                    index.index = js_trim(line[m.end():])
            i += 1
        parse_file_header(index)
        parse_file_header(index)
        if (index.old_file_name is None) != (index.new_file_name is None):
            missing = '"+++ ..."' if index.old_file_name is not None else '"--- ..."'
            name = index.old_file_name if index.old_file_name is not None else index.new_file_name
            raise JsdiffError(f"Missing {missing} file header for {name}")
        while i < len(diffstr):
            line = diffstr[i]
            if _is_diff_header(line) or _is_file_header(line) or line.startswith(_UNDERLINE):
                break
            elif _is_hunk_header(line):
                index.hunks.append(parse_hunk())
            else:
                i += 1

    while i < len(diffstr):
        parse_index()
    return result


def _distance_iterator(start: int, min_line: int, max_line: int):
    """jsdiff's search order around ``start``: start+1, start-1, start+2, ..."""
    want_forward = True
    backward_exhausted = False
    forward_exhausted = False
    local_offset = 1

    def iterator():
        nonlocal want_forward, backward_exhausted, forward_exhausted, local_offset
        while True:
            if want_forward and not forward_exhausted:
                if backward_exhausted:
                    local_offset += 1
                else:
                    want_forward = False
                if start + local_offset <= max_line:
                    return start + local_offset
                forward_exhausted = True
            if not backward_exhausted:
                if not forward_exhausted:
                    want_forward = True
                if min_line <= start - local_offset:
                    value = start - local_offset
                    local_offset += 1
                    return value
                backward_exhausted = True
                continue
            return None

    return iterator


def apply_patch(source: str, patch: FilePatch):
    """jsdiff ``applyPatch`` with ``fuzzFactor: 0`` and no line-ending conversion.

    Returns the patched text, or ``False`` when a hunk cannot be placed.
    """
    lines = source.split("\n")
    hunks = patch.hunks
    if not hunks:
        return source
    prev_line = ""
    remove_eofnl = add_eofnl = False
    for line in hunks[-1].lines:
        if line[:1] == "\\":
            if prev_line[:1] == "+":
                remove_eofnl = True
            elif prev_line[:1] == "-":
                add_eofnl = True
        prev_line = line
    if remove_eofnl:
        if add_eofnl:
            if lines[-1] == "":
                return False
        elif lines[-1] == "":
            lines.pop()
        else:
            return False
    elif add_eofnl:
        if lines[-1] != "":
            lines.append("")
        else:
            return False

    def line_at(position: int):
        return lines[position] if 0 <= position < len(lines) else None

    def apply_hunk(hunk_lines: list[str], to_pos: int):
        # applyHunk with maxErrors = 0: every removed and context line must match.
        patched: list = []
        consecutive_context = 0
        for hunk_line in hunk_lines:
            operation = hunk_line[0] if hunk_line else " "
            content = hunk_line[1:] if hunk_line else hunk_line
            if operation == "-":
                if line_at(to_pos) == content:
                    to_pos += 1
                    consecutive_context = 0
                else:
                    return None
            if operation == "+":
                patched.append(content)
                consecutive_context = 0
            if operation == " ":
                consecutive_context += 1
                if line_at(to_pos) == content:
                    patched.append(line_at(to_pos))
                    to_pos += 1
                else:
                    return None
        if consecutive_context:
            del patched[len(patched) - consecutive_context:]
        to_pos -= consecutive_context
        return patched, to_pos - 1

    result_lines: list[str] = []
    min_line = 0
    prev_hunk_offset = 0
    for hunk in hunks:
        max_line = len(lines) - hunk.old_lines
        to_pos = hunk.old_start + prev_hunk_offset - 1
        iterator = _distance_iterator(to_pos, min_line, max_line)
        hunk_result = None
        while to_pos is not None:
            hunk_result = apply_hunk(hunk.lines, to_pos)
            if hunk_result:
                break
            to_pos = iterator()
        if not hunk_result:
            return False
        # Positions past the end read as '' (JavaScript joins undefined that way).
        result_lines.extend(lines[k] if k < len(lines) else "" for k in range(min_line, to_pos))
        result_lines.extend(hunk_result[0])
        min_line = hunk_result[1] + 1
        prev_hunk_offset = to_pos + 1 - hunk.old_start
    result_lines.extend(lines[min_line:])
    return "\n".join(result_lines)


class Change:
    """One jsdiff change object: ``count`` tokens, added, removed or common."""

    __slots__ = ("count", "added", "removed", "previous", "value")

    def __init__(self, count, added, removed, previous=None):
        self.count = count
        self.added = added
        self.removed = removed
        self.previous = previous
        self.value = None

    def __repr__(self):
        kind = "added" if self.added else "removed" if self.removed else "common"
        return f"Change({kind}, {self.count})"


class _Path:
    __slots__ = ("old_pos", "last")

    def __init__(self, old_pos, last):
        self.old_pos = old_pos
        self.last = last


def diff_arrays(old: list, new: list, *, equals=None, timeout: float | None = None,
                max_edit_length: int | None = None):
    """jsdiff ``diffArrays``: a list of :class:`Change`, or None when a bound is hit.

    ``timeout`` is in seconds (jsdiff takes milliseconds). ``value`` of each change
    is the slice of ``new`` (common, added) or ``old`` (removed) it covers.
    """
    eq = equals or (lambda a, b: a == b)
    new_len, old_len = len(new), len(old)
    edit_length = 1
    max_edit = new_len + old_len
    if max_edit_length is not None:
        max_edit = min(max_edit, max_edit_length)
    deadline = math.inf if timeout is None else time.monotonic() + timeout

    def extract_common(base: _Path, diagonal: int) -> int:
        old_pos = base.old_pos
        new_pos = old_pos - diagonal
        common = 0
        while new_pos + 1 < new_len and old_pos + 1 < old_len and eq(old[old_pos + 1], new[new_pos + 1]):
            new_pos += 1
            old_pos += 1
            common += 1
        if common:
            base.last = Change(common, False, False, base.last)
        base.old_pos = old_pos
        return new_pos

    def add_to_path(path: _Path, added: bool, removed: bool, old_pos_inc: int) -> _Path:
        last = path.last
        if last is not None and last.added == added and last.removed == removed:
            return _Path(path.old_pos + old_pos_inc, Change(last.count + 1, added, removed, last.previous))
        return _Path(path.old_pos + old_pos_inc, Change(1, added, removed, last))

    def build(last: Change | None) -> list[Change]:
        components = []
        while last is not None:
            components.append(last)
            following = last.previous
            last.previous = None
            last = following
        components.reverse()
        new_pos = old_pos = 0
        for component in components:
            if not component.removed:
                component.value = new[new_pos:new_pos + component.count]
                new_pos += component.count
                if not component.added:
                    old_pos += component.count
            else:
                component.value = old[old_pos:old_pos + component.count]
                old_pos += component.count
        return components

    best_path: dict[int, _Path | None] = {0: _Path(-1, None)}
    new_pos = extract_common(best_path[0], 0)
    if best_path[0].old_pos + 1 >= old_len and new_pos + 1 >= new_len:
        return build(best_path[0].last)
    min_diagonal = -math.inf
    max_diagonal = math.inf
    while edit_length <= max_edit and time.monotonic() <= deadline:
        diagonal = int(max(min_diagonal, -edit_length))
        # The upper bound is re-read each step: pruning may lower it mid-loop.
        while diagonal <= min(max_diagonal, edit_length):
            remove_path = best_path.get(diagonal - 1)
            add_path = best_path.get(diagonal + 1)
            if remove_path is not None:
                best_path[diagonal - 1] = None
            can_add = False
            if add_path is not None:
                add_new_pos = add_path.old_pos - diagonal
                can_add = 0 <= add_new_pos < new_len
            can_remove = remove_path is not None and remove_path.old_pos + 1 < old_len
            if not can_add and not can_remove:
                best_path[diagonal] = None
                diagonal += 2
                continue
            if not can_remove or (can_add and remove_path.old_pos < add_path.old_pos):
                base = add_to_path(add_path, True, False, 0)
            else:
                base = add_to_path(remove_path, False, True, 1)
            new_pos = extract_common(base, diagonal)
            if base.old_pos + 1 >= old_len and new_pos + 1 >= new_len:
                return build(base.last)
            best_path[diagonal] = base
            if base.old_pos + 1 >= old_len:
                max_diagonal = min(max_diagonal, diagonal - 1)
            if new_pos + 1 >= new_len:
                min_diagonal = max(min_diagonal, diagonal + 1)
            diagonal += 2
        edit_length += 1
    return None
