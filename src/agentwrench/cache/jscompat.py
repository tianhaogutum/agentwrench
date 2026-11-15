"""JavaScript-compatible primitives that keep output byte-identical to the Node tool.

The original implementation relied on V8 semantics in a few observable places:

* ``JSON.parse``: every number is an IEEE double, ``NaN``/``Infinity`` are
  rejected, and integer-like object keys ("0", "17") are enumerated first in
  ascending numeric order, before all other keys in insertion order.
* ``JSON.stringify``: shortest round-trip number formatting with JavaScript's
  exponent rules, lone UTF-16 surrogates escaped, compact or 2-space indentation.
* ``Number.prototype.toFixed``: round-half-up on the exact binary value.
* Strings are sequences of UTF-16 code units: lengths, indexes and the default
  sort order are measured in code units, not code points.
* ``String.prototype.trim`` and the regular-expression ``\\s`` class use the
  ECMAScript whitespace set rather than Python's.
"""

from __future__ import annotations

import json
import math
import re
import sys
from decimal import ROUND_HALF_UP, Decimal, localcontext
from json.decoder import scanstring
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
_MAX_ARRAY_INDEX = 2**32 - 2

# ECMAScript WhiteSpace + LineTerminator (used by trim() and \s).
JS_WHITESPACE = "\t\n\v\f\r    -     　﻿"
JS_S = f"[{JS_WHITESPACE}]"
JS_NOT_S = f"[^{JS_WHITESPACE}]"
_BLANK = re.compile(f"{JS_S}*")


class JSONSyntaxError(ValueError):
    """Raised when text is not accepted by ``JSON.parse``."""


# --------------------------------------------------------------------------- types


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_object(value: Any) -> bool:
    """``v !== null && typeof v === 'object' && !Array.isArray(v)`` for JSON data."""
    return isinstance(value, dict)


def is_safe_integer(value: Any) -> bool:
    if not is_number(value):
        return False
    if isinstance(value, int):
        return abs(value) <= MAX_SAFE_INTEGER
    return math.isfinite(value) and value.is_integer() and abs(value) <= MAX_SAFE_INTEGER


def is_blank(text: str) -> bool:
    """``!text.trim()``."""
    return _BLANK.fullmatch(text) is not None


# --------------------------------------------------------------------------- strings


_ASTRAL = re.compile("[\U00010000-\U0010ffff]")
_SURROGATE = re.compile("[\ud800-\udfff]")
_UTF16 = "utf-16-le" if sys.byteorder == "little" else "utf-16-be"


def utf16_key(text: str) -> bytes:
    """Sort key that orders strings by UTF-16 code units, like JavaScript ``<``."""
    return text.encode("utf-16-be", "surrogatepass")


def utf16_length(text: str) -> int:
    """JavaScript ``string.length``."""
    return len(text) + len(_ASTRAL.findall(text))


def to_code_units(text: str) -> str:
    """Split astral characters into surrogate pairs so indexes match JavaScript."""
    if not _ASTRAL.search(text):
        return text
    data = memoryview(text.encode(_UTF16, "surrogatepass")).cast("H")
    return "".join(map(chr, data))


def from_code_units(text: str) -> str:
    """Join adjacent valid surrogate pairs back into code points; keep lone ones."""
    if not _SURROGATE.search(text):
        return text
    return text.encode(_UTF16, "surrogatepass").decode(_UTF16, "surrogatepass")


def well_formed(text: str) -> str:
    """Replace lone surrogates with U+FFFD, as Node does when writing to a stream."""
    if not _SURROGATE.search(text):
        return text
    return text.encode(_UTF16, "surrogatepass").decode(_UTF16, "replace")


def is_array_index(key: str) -> bool:
    """True for canonical decimal strings in 0..2**32-2 (V8 'integer index' keys)."""
    if not key or not ("0" <= key[0] <= "9"):
        return False
    if key == "0":
        return True
    if key[0] == "0" or not key.isascii() or not key.isdigit():
        return False
    return int(key) <= _MAX_ARRAY_INDEX


# --------------------------------------------------------------------------- objects


def js_object(pairs: Any) -> dict[str, Any]:
    """Build a dict whose key order matches a JavaScript object's enumeration order."""
    result: dict[str, Any] = {}
    for key, value in pairs.items() if isinstance(pairs, dict) else pairs:
        result[key] = value
    indexes = [key for key in result if is_array_index(key)]
    if not indexes:
        return result
    indexes.sort(key=int)
    ordered = {key: result[key] for key in indexes}
    for key, value in result.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def prop(value: Any, key: str) -> Any:
    """JSON-data property read that yields None for missing (``?.`` / ``??`` use)."""
    return value.get(key) if isinstance(value, dict) else None


# --------------------------------------------------------------------------- numbers


def number_to_string(value: float | int) -> str:
    """JavaScript ``String(number)`` (Number::toString)."""
    if isinstance(value, int) and abs(value) <= MAX_SAFE_INTEGER:
        return str(value)
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + (int(exponent) if exponent else 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k, n = len(digits), point
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        e = n - 1
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + ("e+" if e >= 0 else "e-") + str(abs(e))
    return sign + text


def to_fixed(value: float | int, digits: int) -> str:
    """JavaScript ``Number.prototype.toFixed`` (exact value, ties away from zero)."""
    value = float(value)
    if not math.isfinite(value) or abs(value) >= 1e21:
        return number_to_string(value)
    with localcontext() as context:
        context.prec = 64
        text = format(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), "f")
    if value == 0 and text.startswith("-"):
        text = text[1:]
    return text


# --------------------------------------------------------------------------- JSON.stringify


_ESCAPE = re.compile('[\\x00-\\x1f"\\\\\ud800-\udfff]')
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(match: re.Match[str]) -> str:
    char = match.group()
    return _SHORT_ESCAPES.get(char) or f"\\u{ord(char):04x}"


def quote(text: str) -> str:
    """``JSON.stringify(string)`` including well-formed lone-surrogate escapes."""
    return '"' + _ESCAPE.sub(_escape, from_code_units(text)) + '"'


def stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value)`` or ``JSON.stringify(value, null, indent)``."""
    if indent:
        return _pretty(value, " " * indent, "")
    return _compact(value)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return quote(value)
    if is_number(value):
        return number_to_string(value) if math.isfinite(value) else "null"
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{quote(k)}:{_compact(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_compact(v) for v in value) + "]"
    return _scalar(value)


def _pretty(value: Any, step: str, current: str) -> str:
    inner = current + step
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = (f"{inner}{quote(k)}: {_pretty(v, step, inner)}" for k, v in value.items())
        return "{\n" + ",\n".join(items) + "\n" + current + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return "[\n" + ",\n".join(inner + _pretty(v, step, inner) for v in value) + "\n" + current + "]"
    return _scalar(value)


# --------------------------------------------------------------------------- JSON.parse


def _reject_constant(name: str) -> Any:
    raise JSONSyntaxError(f"invalid literal {name}")


_DECODER = json.JSONDecoder(
    object_pairs_hook=js_object, parse_int=float, parse_float=float, parse_constant=_reject_constant
)


def parse_json(text: str) -> Any:
    """``JSON.parse(text)`` with JavaScript number and key-order semantics."""
    try:
        return _DECODER.decode(text)
    except RecursionError:
        # CPython's decoder recurses; V8's does not. Deep input stays parseable.
        return _IterativeParser(text).parse()
    except ValueError as error:
        raise JSONSyntaxError(str(error)) from None


_WS = re.compile(r"[ \t\n\r]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_LITERALS = (("true", True), ("false", False), ("null", None))


class _IterativeParser:
    """Stack-based JSON parser used only for nesting deeper than CPython allows."""

    def __init__(self, text: str) -> None:
        self.text = text

    def _ws(self, pos: int) -> int:
        return _WS.match(self.text, pos).end()  # type: ignore[union-attr]

    def _expect(self, pos: int, char: str) -> int:
        if self.text[pos : pos + 1] != char:
            raise JSONSyntaxError(f"expected {char!r} at {pos}")
        return pos + 1

    def _key(self, pos: int) -> tuple[str, int]:
        pos = self._expect(pos, '"')
        try:
            key, pos = scanstring(self.text, pos, True)
        except ValueError as error:
            raise JSONSyntaxError(str(error)) from None
        pos = self._expect(self._ws(pos), ":")
        return key, self._ws(pos)

    def parse(self) -> Any:
        text = self.text
        stack: list[list[Any]] = []
        pos = self._ws(0)
        while True:
            char = text[pos : pos + 1]
            if char == "{":
                pos = self._ws(pos + 1)
                if text[pos : pos + 1] == "}":
                    value: Any = {}
                    pos += 1
                else:
                    key, pos = self._key(pos)
                    stack.append([[], key])
                    continue
            elif char == "[":
                pos = self._ws(pos + 1)
                if text[pos : pos + 1] == "]":
                    value = []
                    pos += 1
                else:
                    stack.append([[]])
                    continue
            elif char == '"':
                try:
                    value, pos = scanstring(text, pos + 1, True)
                except ValueError as error:
                    raise JSONSyntaxError(str(error)) from None
            else:
                match = _NUMBER.match(text, pos)
                if match:
                    value, pos = float(match.group()), match.end()
                else:
                    for literal, literal_value in _LITERALS:
                        if text.startswith(literal, pos):
                            value, pos = literal_value, pos + len(literal)
                            break
                    else:
                        raise JSONSyntaxError(f"unexpected value at {pos}")
            while True:
                pos = self._ws(pos)
                if not stack:
                    if pos != len(text):
                        raise JSONSyntaxError(f"extra data at {pos}")
                    return value
                top = stack[-1]
                separator = text[pos : pos + 1]
                if len(top) == 1:
                    top[0].append(value)
                    if separator == ",":
                        pos = self._ws(pos + 1)
                        break
                    pos = self._expect(pos, "]")
                    value = top[0]
                else:
                    top[0].append((top[1], value))
                    if separator == ",":
                        top[1], pos = self._key(self._ws(pos + 1))
                        break
                    pos = self._expect(pos, "}")
                    value = js_object(top[0])
                stack.pop()
