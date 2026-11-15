"""Offline prompt-prefix drift diagnostics and canonicalization for captured LLM requests.

Python port of the ``prefix-drift`` Node tool, exposed as ``agentwrench cache``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .analyze import (
    CLASSES,
    analyze,
    compare_pair,
    differing_span,
    first_difference,
    segments,
    usage_chain,
)
from .canonicalize import RULES, canonicalize, write_capture
from .capture import FORMATS, LIMITS, Record, detect_format, parse_capture, read_capture
from .errors import CacheError, FsError
from .sessions import SESSION_LIMITS, analyze_sessions
from .volatile import mask_volatile_fields, read_volatile_fields

__all__ = [
    "CLASSES",
    "FORMATS",
    "LIMITS",
    "RULES",
    "SESSION_LIMITS",
    "CacheError",
    "FsError",
    "Record",
    "analyze",
    "analyze_sessions",
    "canonicalize",
    "compare_pair",
    "detect_format",
    "differing_span",
    "first_difference",
    "mask_volatile_fields",
    "parse_capture",
    "read_capture",
    "read_volatile_fields",
    "segments",
    "usage_chain",
    "write_capture",
]
