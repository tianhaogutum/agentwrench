"""Slimming configuration validation and tool-name globbing."""

from __future__ import annotations

import re
from typing import Any

from .jsjson import MISSING, is_safe_integer, js_keys, utf16_length

PROGRAM = 'agentwrench slim'
VERSION = '0.1.0'

ANNOTATIONS: tuple[str, ...] = ('description', 'title', 'examples', '$comment', 'default')
DEFAULT_ANNOTATIONS: tuple[str, ...] = ANNOTATIONS[:4]
_KEYS = frozenset({'allow', 'deny', 'maxDescriptionChars', 'stripSchemaAnnotations'})
_CONTROL = re.compile('[\x00-\x1f\x7f]')


class InputError(Exception):
    """Invalid user input or configuration (CLI exit status 2)."""

    @property
    def message(self) -> str:
        return str(self)


def validate_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError('config: expected an object')
    for key in js_keys(value):
        if key not in _KEYS:
            raise InputError(f'config.{key}: unknown key')
    for key in ('allow', 'deny'):
        globs = value.get(key, MISSING)
        if globs is MISSING:
            continue
        if not isinstance(globs, list) or len(globs) > 1024:
            raise InputError(f'config.{key}: expected an array of at most 1024 globs')
        for glob in globs:
            if not isinstance(glob, str) or not glob or utf16_length(glob) > 256 or _CONTROL.search(glob):
                raise InputError(f'config.{key}: globs must be nonempty strings of at most 256 characters without control characters')
    cap = value.get('maxDescriptionChars', MISSING)
    if cap is not MISSING and (not is_safe_integer(cap) or cap < 0 or cap > 1_000_000):
        raise InputError('config.maxDescriptionChars: expected an integer from 0 to 1000000')
    strip = value.get('stripSchemaAnnotations', MISSING)
    if strip is not MISSING and (not isinstance(strip, list) or any(not isinstance(k, str) or k not in ANNOTATIONS for k in strip)):
        raise InputError(f"config.stripSchemaAnnotations: expected an array containing only {', '.join(ANNOTATIONS)}")
    return dict(value)


def glob_match(pattern: str, value: str) -> bool:
    """Whole-string match where only ``*`` and ``?`` are special (code points)."""
    i = j = 0
    star = -1
    retry = 0
    plen, slen = len(pattern), len(value)
    while j < slen:
        if i < plen and (pattern[i] == '?' or (pattern[i] != '*' and pattern[i] == value[j])):
            i += 1
            j += 1
        elif i < plen and pattern[i] == '*':
            star = i
            i += 1
            retry = j
        elif star != -1:
            i = star + 1
            retry += 1
            j = retry
        else:
            return False
    while i < plen and pattern[i] == '*':
        i += 1
    return i == plen


def is_visible(name: str, config: dict[str, Any]) -> bool:
    allow = config.get('allow', MISSING)
    if allow is not MISSING and not any(glob_match(p, name) for p in allow):
        return False
    return not any(glob_match(p, name) for p in config.get('deny') or [])
