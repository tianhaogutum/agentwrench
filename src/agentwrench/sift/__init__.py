"""Token-bounded, source-locatable evidence from a large JSON tool result or test log.

``agentwrench sift`` is an adapter around shrinkfit: it keeps exact original
excerpts with byte ranges, JSON Pointers or line ranges, records every omitted
byte range, and measures the whole envelope with tiktoken.

The library API (``Rules``, ``summarize``, ``verify``, ``recover``, ``Summary``,
``BudgetError``, ``InputError``) is loaded lazily on first attribute access, so
importing this package does not need the optional ``sift`` extra.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# One-line help per subcommand. Kept here (no optional imports) so that
# `agentwrench sift --help` works before the `sift` extra is installed.
SUBCOMMANDS = {
    "summarize": "emit one compact evidence JSON envelope",
    "recover": "read original bytes after verifying summary SHA-256",
    "verify": "check saved evidence and its exact serialized token count",
}

_EXPORTS = {
    "BudgetError": ".core",
    "InputError": ".core",
    "Rules": ".core",
    "Summary": ".core",
    "recover": ".core",
    "summarize": ".core",
    "verify": ".verification",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
