"""Seeded adversarial combinations (the original used fast-check with the same seed and shapes)."""
import json
import random

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch
from agentwrench.fence.patch import apply_exactly, parse_candidate

from .support import create_two_files_patch

SEED = 2026111109
RUNS = 60


def inputs(salt):
    rng = random.Random(SEED + salt)
    for _ in range(RUNS):
        yield {
            "numbers": [rng.randint(0, 1_000_000) for _ in range(rng.randint(2, 8))],
            "index": rng.randint(0, 100),
            "eol": rng.choice(["\n", "\r\n"]),
            "final_newline": rng.random() < 0.5,
            "context": rng.randint(0, 5),
            "banner": rng.choice(["hello world", "你好 😀", "a\\tb"]),
        }


def scenario(data):
    numbers, eol = data["numbers"], data["eol"]
    selected = data["index"] % len(numbers)
    adjacent = (selected + 1) % len(numbers)

    def make(changed=-1, format_adjacent=False):
        functions = []
        for i, number in enumerate(numbers):
            body = f"return {number + (1 if i == changed else 0)};"
            spacing = eol + "  " if format_adjacent and i == adjacent else " "
            functions.append(f"export function fn{i}(): number {{{spacing}{body} }}")
        return (f"const banner = {json.dumps(data['banner'], ensure_ascii=False)};{eol}" + eol.join(functions)
                + (eol if data["final_newline"] else ""))

    before = make()
    allow = [f"generated.ts::fn{selected}"]

    def check(candidate):
        patch = create_two_files_patch("a/generated.ts", "b/generated.ts", before, candidate, context=data["context"])
        assert apply_exactly(before, parse_candidate(patch)[0]) == candidate
        return patch, check_patch(patch=patch, allow=allow, read_base=lambda _: before)

    return before, selected, adjacent, make, check, allow


def test_seeded_property_in_node_changes_survive_exact_patch_reconstruction_and_pass():
    for data in inputs(0):
        _, selected, _, make, check, _ = scenario(data)
        _, report = check(make(selected))
        assert report["ok"] is True, data
        assert len(report["accepted"]) == 1, data


def test_seeded_property_any_changed_adjacent_function_rejects_regardless_of_hunk_grouping():
    for data in inputs(1):
        _, _, adjacent, make, check, _ = scenario(data)
        _, report = check(make(adjacent))
        assert report["ok"] is False, data
        assert len(report["violations"]) > 0, data


def test_seeded_property_adjacent_trivia_fails_strict_policy_and_passes_only_explicit_whitespace_policy():
    for data in inputs(2):
        before, _, _, make, check, allow = scenario(data)
        patch, report = check(make(-1, True))
        assert report["ok"] is False, data
        assert check_patch(patch=patch, allow=allow, read_base=lambda _: before, ignore_formatting=True)["ok"] is True, data
