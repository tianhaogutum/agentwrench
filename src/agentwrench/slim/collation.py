"""Approximation of ``String.prototype.localeCompare`` (ICU root/en-US collation).

The original tool breaks byte-count ties between tool names with
``a.name.localeCompare(b.name)``. For ASCII names this module reproduces ICU's
default (tertiary strength, non-ignorable punctuation) ordering exactly; the
table below was extracted from Node.js 24. Other characters are approximated:
canonical decomposition supplies accent (secondary) weights and remaining
characters are bucketed by Unicode category next to the matching ASCII group
(symbols before ``$``, letters after ``z``), then ordered by code point.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

# ICU primary order of non-ignorable ASCII; letters share a primary weight per case pair.
_ORDER = [
    '\t', '\n', '\v', '\f', '\r', ' ', '_', '-', ',', ';', ':', '!', '?', '.', "'", '"', '(', ')',
    '[', ']', '{', '}', '@', '*', '/', '\\', '&', '#', '%', '`', '^', '+', '<', '=', '>', '|', '~',
    '$', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
] + [chr(code) + chr(code).upper() for code in range(ord('a'), ord('z') + 1)]

_PRIMARY: dict[str, tuple[int, int]] = {}
for _rank, _group in enumerate(_ORDER, start=1):
    for _char in _group:
        _PRIMARY[_char] = (_rank, 0)
# Buckets for other characters, relative to the ASCII groups they sort near.
_SPACE, _PUNCTUATION, _SYMBOL, _CURRENCY = _PRIMARY[' '][0], _PRIMARY['.'][0], _PRIMARY['~'][0], _PRIMARY['$'][0]
_DIGIT, _LETTER = _PRIMARY['9'][0], _PRIMARY['z'][0]


def _non_ascii_primary(char: str) -> tuple[int, int]:
    category = unicodedata.category(char)
    if category == 'Sc':
        bucket = _CURRENCY
    elif category[0] == 'S':
        bucket = _SYMBOL
    elif category[0] == 'P':
        bucket = _PUNCTUATION
    elif category[0] == 'Z':
        bucket = _SPACE
    elif category[0] == 'N':
        bucket = _DIGIT
    else:
        bucket = _LETTER
    return bucket, ord(char)


@lru_cache(maxsize=65536)
def sort_key(text: str) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, ...], ...], tuple[int, ...]]:
    """A key whose ordering matches ``localeCompare`` (stable ties stay equal)."""
    primary: list[tuple[int, int]] = []
    secondary: list[tuple[int, ...]] = []
    tertiary: list[int] = []
    for char in text:
        pieces = char if char.isascii() else unicodedata.normalize('NFD', char)
        for piece in pieces:
            if piece.isascii():
                weight = _PRIMARY.get(piece)
                if weight is None:  # control characters are completely ignorable
                    continue
                primary.append(weight)
                secondary.append(())
                tertiary.append(1 if piece.isupper() else 0)
            elif unicodedata.combining(piece) and secondary:
                secondary[-1] = secondary[-1] + (ord(piece),)
            else:
                primary.append(_non_ascii_primary(piece))
                secondary.append(())
                tertiary.append(1 if piece.isupper() else 0)
    return tuple(primary), tuple(secondary), tuple(tertiary)
