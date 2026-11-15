"""Small helpers for human-readable output shared by the subcommands."""
from __future__ import annotations


def plural(count: int, noun: str, plural_form: str | None = None) -> str:
    """``plural(1, "issue")`` -> ``"1 issue"``; ``plural(2, "issue")`` -> ``"2 issues"``."""
    return f"{count} {noun if count == 1 else plural_form or noun + 's'}"
