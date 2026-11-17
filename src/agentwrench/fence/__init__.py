"""Scope gate for agent-generated TypeScript patches: exact allowed symbols, everything else unchanged.

Library use::

    from agentwrench.fence import check_patch, git_baseline
    report = check_patch(patch=text, allow=["src/api.ts::getUser"],
                         read_base=git_baseline("/path/to/repo", "reviewed-base-sha"))

The Tree-sitter dependencies (``pip install 'agentwrench[fence]'``) are imported
only when a source is parsed, so importing this package needs no optional extra.
"""
from agentwrench import __version__  # noqa: F401

# One-line help per subcommand, readable without the optional extra.
SUBCOMMANDS = {
    "check": "check a unified patch against exact allowed TypeScript symbols (exit 0 pass, 1 reject, 2 invalid)",
    "symbols": "list the exact symbol selectors of a baseline TypeScript file",
}

_EXPORTS = {
    "check_patch": "check", "parse_selector": "check",
    "parse_source": "syntax", "list_symbols": "syntax",
    "directory_baseline": "baseline", "git_baseline": "baseline",
    "FenceError": "errors",
}
__all__ = sorted(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f"{__name__}.{module}"), name)
