"""Reject data that cannot be safely reserialized with JavaScript number semantics."""

from __future__ import annotations

import math
from typing import Any

from .config import InputError
from .jsjson import MAX_SAFE_INTEGER, js_keys


def validate_json_tree(value: Any, depth: int = 0) -> Any:
    """Depth is checked before recursing into schemas or arbitrary literals."""
    if depth > 128:
        raise InputError('JSON nesting exceeds 128')
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise InputError('JSON number exceeds finite/safe-integer range')
    elif isinstance(value, float):
        if not math.isfinite(value) or (value.is_integer() and abs(value) > MAX_SAFE_INTEGER):
            raise InputError('JSON number exceeds finite/safe-integer range')
    elif isinstance(value, dict):
        for key in js_keys(value):
            validate_json_tree(value[key], depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_json_tree(item, depth + 1)
    return value
