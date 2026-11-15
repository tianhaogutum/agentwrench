"""JavaScript-compatible JSON helpers.

The original tool ran on Node.js; its byte-level output contract follows
``JSON.parse``/``JSON.stringify`` and ECMAScript string/number semantics. This
module reproduces the parts of those semantics that are observable in output:

* object keys that are array indices are serialized first, in numeric order;
* numbers are formatted like ``Number.prototype.toString``;
* lone surrogates are escaped as ``\\udxxx``;
* ``NaN``/``Infinity`` literals are rejected and huge integers become floats;
* nesting depth is unlimited (an iterative fallback replaces recursion);
* lengths can be measured in UTF-16 code units like ``String.prototype.length``.
"""

from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from json.decoder import scanstring
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


class _Missing:
    """Marker for an absent property (JavaScript ``undefined``)."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return 'MISSING'

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()

# ECMAScript WhiteSpace + LineTerminator (the \s class and String.prototype.trim set).
JS_WHITESPACE = '\t\n\v\f\r                  　﻿'
JS_WHITESPACE_CLASS = '[' + re.escape(JS_WHITESPACE) + ']'


# --- type predicates ---------------------------------------------------------

def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_integer(value: Any) -> bool:
    """``Number.isInteger``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def is_safe_integer(value: Any) -> bool:
    """``Number.isSafeInteger``."""
    return is_integer(value) and abs(value) <= MAX_SAFE_INTEGER


def is_object(value: Any) -> bool:
    """A plain JSON object (not an array or null)."""
    return isinstance(value, dict)


def truthy(value: Any) -> bool:
    """JavaScript truthiness for JSON values."""
    if value is None or value is MISSING or value is False:
        return False
    if value is True or isinstance(value, (dict, list)):
        return True
    if isinstance(value, str):
        return value != ''
    if is_number(value):
        return value != 0 and value == value
    return bool(value)


def get(value: Any, key: str) -> Any:
    """Optional property access (``value?.[key]``) on a JSON value."""
    if isinstance(value, dict):
        return value.get(key, MISSING)
    return MISSING


def utf16_length(text: str) -> int:
    """``String.prototype.length`` (UTF-16 code units)."""
    if text.isascii():
        return len(text)
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def utf8_length(text: str) -> int:
    """``Buffer.byteLength`` (lone surrogates count as U+FFFD, 3 bytes)."""
    return len(text.encode('utf-8', 'surrogatepass'))


def id_key(value: Any) -> Any:
    """A hashable key with JavaScript ``Map`` (SameValueZero) identity."""
    if isinstance(value, bool):
        return ('bool', value)
    if is_number(value):
        return ('number', value)
    if isinstance(value, str):
        return ('string', value)
    if value is None:
        return ('null',)
    return ('object', id(value))


# --- object key order ---------------------------------------------------------

_INDEX_KEY = re.compile(r'(?:0|[1-9][0-9]{0,9})\Z')


def _is_index_key(key: str) -> bool:
    return bool(key) and '0' <= key[0] <= '9' and _INDEX_KEY.match(key) is not None and int(key) < 4294967295


def js_keys(obj: dict) -> list[str]:
    """``Object.keys`` order: integer-index keys ascending, then insertion order."""
    indices = [key for key in obj if key[:1].isdigit() and _is_index_key(key)]
    if not indices:
        return list(obj)
    indices.sort(key=int)
    chosen = set(indices)
    return indices + [key for key in obj if key not in chosen]


# --- parsing ------------------------------------------------------------------

def decode_utf8(data: bytes) -> str:
    """``new TextDecoder('utf-8', { fatal: true }).decode(data)`` (drops one BOM)."""
    text = data.decode('utf-8')
    return text[1:] if text.startswith('﻿') else text


def _parse_int(literal: str) -> int | float:
    # Integers beyond the safe range are rejected by callers; keep huge literals
    # as floats so they become Infinity like JavaScript instead of raising.
    return int(literal) if len(literal) <= 400 else float(literal)


def _reject_constant(name: str) -> Any:
    raise ValueError(f'invalid JSON literal {name}')


def parse(text: str) -> Any:
    """``JSON.parse`` for a decoded string; raises ``ValueError`` on bad input."""
    try:
        return json.loads(text, parse_int=_parse_int, parse_constant=_reject_constant)
    except RecursionError:
        return _parse_iterative(text)


_WHITESPACE = re.compile(r'[ \t\n\r]*')
_NUMBER = re.compile(r'(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?')


def _parse_iterative(s: str) -> Any:
    """Stack-based JSON parser used only for documents deeper than recursion allows."""
    n = len(s)

    def skip(i: int) -> int:
        return _WHITESPACE.match(s, i).end()

    def key_at(i: int) -> tuple[str, int]:
        if i >= n or s[i] != '"':
            raise ValueError('expected property name')
        key, i = scanstring(s, i + 1, True)
        i = skip(i)
        if i >= n or s[i] != ':':
            raise ValueError("expected ':'")
        return key, skip(i + 1)

    stack: list[list[Any]] = []
    i = skip(0)
    while True:
        if i >= n:
            raise ValueError('unexpected end of JSON input')
        char = s[i]
        if char == '{':
            i = skip(i + 1)
            if i < n and s[i] == '}':
                value: Any = {}
                i += 1
            else:
                key, i = key_at(i)
                stack.append([{}, key])
                continue
        elif char == '[':
            i = skip(i + 1)
            if i < n and s[i] == ']':
                value = []
                i += 1
            else:
                stack.append([[], None])
                continue
        elif char == '"':
            value, i = scanstring(s, i + 1, True)
        elif s.startswith('true', i):
            value, i = True, i + 4
        elif s.startswith('false', i):
            value, i = False, i + 5
        elif s.startswith('null', i):
            value, i = None, i + 4
        else:
            match = _NUMBER.match(s, i)
            if match is None:
                raise ValueError('unexpected token')
            integer, fraction, exponent = match.groups()
            value = float(match.group(0)) if fraction or exponent else _parse_int(integer)
            i = match.end()
        while True:
            i = skip(i)
            if not stack:
                if i != n:
                    raise ValueError('unexpected data after JSON')
                return value
            frame = stack[-1]
            container = frame[0]
            if isinstance(container, dict):
                container[frame[1]] = value
            else:
                container.append(value)
            if i < n and s[i] == ',':
                i = skip(i + 1)
                if isinstance(container, dict):
                    frame[1], i = key_at(i)
                break
            if i < n and s[i] == ('}' if isinstance(container, dict) else ']'):
                i += 1
                stack.pop()
                value = container
                continue
            raise ValueError('unexpected token')


# --- serialization --------------------------------------------------------------

_ESCAPES = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t'}
_NEEDS_ESCAPE = re.compile('[\x00-\x1f"\\\\\ud800-\udfff]')


def _escape(match: re.Match[str]) -> str:
    char = match.group(0)
    return _ESCAPES.get(char) or f'\\u{ord(char):04x}'


def quote(text: str) -> str:
    """``JSON.stringify`` for a string."""
    return '"' + _NEEDS_ESCAPE.sub(_escape, text) + '"'


def format_number(value: int | float) -> str:
    """``Number.prototype.toString`` / ``JSON.stringify`` for a number."""
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INTEGER + 1:
            return str(value)
        value = float(value)
    if value != value or value in (math.inf, -math.inf):
        return 'null'
    if value == 0:
        return '0'
    sign = '-' if value < 0 else ''
    # repr() yields the shortest round-tripping digits, as ECMAScript requires.
    mantissa, _, exponent = repr(abs(value)).partition('e')
    whole, _, fraction = mantissa.partition('.')
    raw = whole + fraction
    stripped = raw.lstrip('0')
    # value == 0.<digits> * 10**point
    point = len(whole) - (len(raw) - len(stripped)) + (int(exponent) if exponent else 0)
    digits = stripped.rstrip('0')
    k = len(digits)
    if k <= point <= 21:
        body = digits + '0' * (point - k)
    elif 0 < point <= 21:
        body = digits[:point] + '.' + digits[point:]
    elif -6 < point <= 0:
        body = '0.' + '0' * -point + digits
    else:
        e = point - 1
        mantissa_text = digits if k == 1 else digits[0] + '.' + digits[1:]
        body = f"{mantissa_text}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + body


def to_fixed(value: float, places: int) -> str:
    """``Number.prototype.toFixed`` (round half away from zero on the exact value)."""
    if value == 0:
        value = 0.0
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value)`` or ``JSON.stringify(value, null, indent)``."""
    parts: list[str] = []
    _encode(value, parts, '\n' if indent else '', ' ' * indent if indent else '')
    return ''.join(parts)


def _encode(value: Any, out: list[str], newline: str, step: str) -> None:
    if value is None:
        out.append('null')
    elif value is True:
        out.append('true')
    elif value is False:
        out.append('false')
    elif isinstance(value, str):
        out.append(quote(value))
    elif isinstance(value, (int, float)):
        out.append(format_number(value))
    elif isinstance(value, dict):
        keys = [key for key in js_keys(value) if value[key] is not MISSING]
        if not keys:
            out.append('{}')
            return
        inner = newline + step
        out.append('{')
        for index, key in enumerate(keys):
            if index:
                out.append(',')
            out.append(inner)
            out.append(quote(key))
            out.append(': ' if step else ':')
            _encode(value[key], out, inner, step)
        out.append(newline)
        out.append('}')
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append('[]')
            return
        inner = newline + step
        out.append('[')
        for index, item in enumerate(value):
            if index:
                out.append(',')
            out.append(inner)
            if item is MISSING:
                out.append('null')
            else:
                _encode(item, out, inner, step)
        out.append(newline)
        out.append(']')
    else:
        raise TypeError(f'cannot serialize {type(value).__name__}')
