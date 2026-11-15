"""Three-way merge of projected native server snapshots; no I/O or evaluation."""
from __future__ import annotations

import copy

from .formats import same_value

MISSING = object()


def change_kind(old, new):
    """Classify an already established difference; absence is distinct from null."""
    return 'add' if old is MISSING else 'delete' if new is MISSING else 'change'


def changes(old, new, path=()):
    """Sorted, value-free target-to-result edits with atomic arrays/map boundaries."""
    if same_value(old, new):
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            yield from changes(old.get(key, MISSING), new.get(key, MISSING), path + (key,))
    else:
        yield {'path': list(path), 'kind': change_kind(old, new)}


def conflict_details(base, incoming, target, conflicts):
    """Classify each side relative to base, without exposing the compared values."""
    def at(snapshot, path):
        for key in path:
            snapshot = snapshot.get(key, MISSING) if isinstance(snapshot, dict) else MISSING
        return snapshot

    return [{'path': list(path),
             'incoming': change_kind(at(base, path), at(incoming, path)),
             'target': change_kind(at(base, path), at(target, path))}
            for path in sorted(conflicts)]


def three_way(base: dict, incoming: dict, target: dict) -> tuple[dict, list[tuple[str, ...]]]:
    """Merge independent map edits. Lists are atomic; conflicts contain paths only.

    Absence differs from null. Concurrent additions of different definitions and
    delete/modify pairs conflict rather than manufacturing a combined definition.
    The caller must refuse the entire result when conflicts are present.
    """
    conflicts = []

    def clone(value):
        return MISSING if value is MISSING else copy.deepcopy(value)

    def merge(old, new, local, path):
        if same_value(new, old):
            return clone(local)
        if same_value(local, old) or same_value(new, local):
            return clone(new)
        if all(isinstance(value, dict) for value in (old, new, local)):
            result = {}
            # Target order survives; incoming-only keys append in source order.
            for key in dict.fromkeys([*local, *new, *old]):
                value = merge(old.get(key, MISSING), new.get(key, MISSING),
                              local.get(key, MISSING), path + (key,))
                if value is not MISSING:
                    result[key] = value
            return result
        conflicts.append(path)
        return clone(local)

    return merge(base, incoming, target, ()), conflicts
