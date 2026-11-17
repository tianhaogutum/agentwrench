"""Section 4: plain handoff packs inside the project, checked for staleness.

Uses the standard-library pack workflow of :mod:`agentwrench.handoff` (no
optional extra). Nothing runs unless a pack is found. Then scan runs read-only
Git commands on the project's repository (no optional locks, fsmonitor, filters
or pager) and hashes its files, exactly as ``agentwrench handoff verify`` does.
In-toto receipt handoffs need the ``handoff`` extra and are not checked here.
"""
from __future__ import annotations

import os
import stat
from collections import deque
from pathlib import Path

from ..handoff.io import HandoffError
from ..handoff.pack import BUNDLE_VERSION, PACK_JSON, load_pack, verify_pack
from ..handoff.pack_safety import redact
from ..handoff.pack_scope import repository
from .common import Finding, Locations, quote, show_path

MAX_DEPTH = 3          # <project>/.handoff/pack-01/handoff.json is depth 2
MAX_ENTRIES = 20000    # directory entries examined while looking for packs
MAX_PACKS = 10
MAX_PACK_BYTES = 16 * 1024 * 1024
SKIP = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache"})
MARKERS = (f'"schema": "{BUNDLE_VERSION}"'.encode(), f'"schema":"{BUNDLE_VERSION}"'.encode())


def _is_pack_json(path: str) -> bool:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PACK_BYTES:
            return False
        with open(path, "rb") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return any(marker in head for marker in MARKERS)


def find_packs(project: Path) -> list[Path]:
    """Pack directories (holding a ``relayproof-handoff/1`` handoff.json) below ``project``; no symlinks followed."""
    found: list[Path] = []
    queue: deque[tuple[str, int]] = deque([(str(project), 0)])
    seen = 0
    while queue and len(found) < MAX_PACKS and seen < MAX_ENTRIES:
        directory, depth = queue.popleft()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if entry.name == PACK_JSON and _is_pack_json(entry.path):
                found.append(Path(directory))
            elif depth < MAX_DEPTH and entry.name not in SKIP and entry.is_dir(follow_symlinks=False):
                queue.append((entry.path, depth + 1))
    return sorted(found)[:MAX_PACKS]


def scan_handoff(loc: Locations, findings: list[Finding]) -> dict:
    project = Path(os.path.realpath(loc.project))
    packs = find_packs(project)
    result: dict = {"repository": None, "packs": []}
    if not packs:
        return result
    try:
        repo = repository(str(project))
    except (HandoffError, OSError):
        result["packs"] = [{"path": str(pack), "status": "not_checked", "captured_at": None, "changes": [],
                            "evidence": None, "error": "project is not inside a usable Git worktree"}
                           for pack in packs]
        findings.append(Finding("warning", "handoff", None,
                                f"found {len(packs)} handoff pack(s) but {show_path(project)} is not inside a "
                                "usable Git worktree; their staleness was not checked"))
        return result
    result["repository"] = repo
    for pack in packs:
        shown = show_path(pack)
        command = [f"agentwrench handoff verify {quote(shown)} --repo {quote(show_path(repo))}"]
        entry: dict = {"path": str(pack), "status": "invalid", "captured_at": None, "changes": [],
                       "evidence": None, "error": None}
        result["packs"].append(entry)
        try:
            bundle = load_pack(str(pack))
            report = verify_pack(repo, bundle)
        except OSError as exc:
            entry["error"] = os.strerror(exc.errno) if exc.errno else "cannot read"
            findings.append(Finding("error", "handoff", None, f"handoff pack {shown} cannot be read", command))
            continue
        except HandoffError as exc:
            entry["error"] = redact(str(exc))
            findings.append(Finding("error", "handoff", None,
                                    f"handoff pack {shown} is not a valid pack: {entry['error']}", command))
            continue
        evidence = report["evidence"]
        bound = sum(item["freshness"] == "matches_captured_scope" for item in evidence)
        entry.update(status=report["status"], captured_at=bundle.get("createdAt"),
                     changes=report["snapshot"]["changes"][:20],
                     evidence={"records": len(evidence), "matching": bound})
        if report["snapshot"]["status"] != "matches_captured_scope":
            changes = report["snapshot"]["changes"]
            first = f" (first: {changes[0]})" if changes else ""
            findings.append(Finding("warning", "handoff", None,
                                    f"handoff pack {shown} is stale: {len(changes)} change(s) in the repository "
                                    f"since it was written{first}", command))
        elif bound < len(evidence):
            findings.append(Finding("warning", "handoff", None,
                                    f"handoff pack {shown}: {len(evidence) - bound} of {len(evidence)} run records "
                                    "do not match the captured repository state", command))
    return result
