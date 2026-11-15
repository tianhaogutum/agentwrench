"""Shared data model. No event payload is ever executed or opened as a path."""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


class InputError(ValueError):
    """Malformed or unsupported input, reported as CLI exit 2."""


@dataclass
class Trajectory:
    source: str
    events: list[dict[str, Any]] = field(default_factory=list)
    ignored: dict[str, int] = field(default_factory=dict)
    complete: bool = False
    pending_calls: list[str] = field(default_factory=list)
    losses: list[dict[str, Any]] = field(default_factory=list)
    export_losses: list[dict[str, Any]] = field(default_factory=list, repr=False)
    original_jsonl: str | None = field(default=None, repr=False)

    def emit(self, kind: str, line: int, **fields: Any) -> dict[str, Any]:
        event = {"index": len(self.events) + 1, "source_line": line, "kind": kind, **fields}
        self.events.append(event)
        return event

    def ignore(self, name: str) -> None:
        self.ignored[name] = self.ignored.get(name, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        # Keep originals out of normal reports and comparison fingerprints.
        return {"schema_version": 1, "source": self.source,
                "events": deepcopy(self.events), "ignored": dict(self.ignored),
                "complete": self.complete, "pending_calls": list(self.pending_calls),
                "losses": deepcopy(self.losses), "ignored_count": sum(self.ignored.values())}


def obj(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise InputError(f"{label}: expected an object")
    return value


def string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label}: expected a nonempty string")
    return value


def integer(value: Any, label: str, *, nonnegative: bool = True) -> int:
    if type(value) is not int or (nonnegative and value < 0):
        raise InputError(f"{label}: expected {'nonnegative ' if nonnegative else ''}integer")
    return value
