"""End-to-end plain-pack demo of `agentwrench handoff` (standard library only).

Creates a synthetic Git repository below examples/handoff/demo-output/pack/ with
a commit, a staged change, an unstaged change, an untracked file and a sample
`.env` file. It runs a failing test for real with `run`, writes a pack with
`pack`, checks it with `verify`, edits the source and shows that `verify`
reports the pack as stale, then runs the fixed test and writes a new pack.
No extra packages, no network, no API key.

    python examples/handoff/pack_demo.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
OUTPUT = HERE / "demo-output" / "pack"
TEST = """\
import unittest
from calculator import add

class AddTest(unittest.TestCase):
    def test_adds_two_numbers(self):
        self.assertEqual(add(1, 2), 3)

unittest.main()
"""


def environment() -> dict[str, str]:
    """Let the demo run from a source checkout as well as from an installed package."""
    env = os.environ.copy()
    if (SRC / "agentwrench").is_dir():
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    return env


def handoff(*args: str, expect: int = 0) -> dict:
    argv = [sys.executable, "-m", "agentwrench", "handoff", *args]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=environment())
    if result.returncode != expect:
        raise SystemExit(f"agentwrench handoff {args[0]} exited {result.returncode}, expected {expect}: "
                         f"{result.stderr.strip()}")
    return json.loads(result.stdout)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10)


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"{OUTPUT} already exists; remove or rename it before running the demo")
    repo = OUTPUT / "sample-repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Synthetic Example")
    git(repo, "config", "user.email", "example@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("__pycache__/\nignored-cache.txt\n")
    (repo / "calculator.py").write_text("# SYNTHETIC SAMPLE: deliberately wrong addition\n"
                                        "def add(a, b):\n    return a - b\n")
    (repo / "test_calculator.py").write_text(TEST)
    (repo / "notes.txt").write_text("Synthetic notes\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Synthetic calculator with a failing test")
    with (repo / "notes.txt").open("a") as notes:
        notes.write("Staged sample change\n")
    git(repo, "add", "notes.txt")
    with (repo / "calculator.py").open("a") as source:
        source.write("# Unstaged sample change\n")
    (repo / "scratch.txt").write_text("Untracked synthetic task notes\n")
    (repo / ".env").write_text("SAMPLE_SECRET=DEMO_ONLY_DO_NOT_EXPORT_4852\n")
    (repo / "ignored-cache.txt").write_text("SAMPLE IGNORED DATA\n")
    test = ["--", sys.executable, "-B", "test_calculator.py"]

    failed_run = OUTPUT / "failed-test.json"
    print(handoff("run", "--repo", str(repo), "--output", str(failed_run), "--label", "unit tests", *test, expect=1))
    failed_pack = OUTPUT / "failed-handoff"
    print(handoff("pack", "--repo", str(repo), "--task", str(HERE / "pack-task.json"),
                  "--evidence", str(failed_run), "--output", str(failed_pack)))
    report = handoff("verify", str(failed_pack), "--repo", str(repo))
    print("before the fix:", report["status"])
    pack = json.loads((failed_pack / "handoff.json").read_text(encoding="utf-8"))
    assert pack["evidence"][0]["result"] == "failed"
    for name in ("handoff.json", "handoff.md"):
        assert "DEMO_ONLY_DO_NOT_EXPORT_4852" not in (failed_pack / name).read_text(encoding="utf-8")

    (repo / "calculator.py").write_text("# SYNTHETIC SAMPLE: corrected addition\ndef add(a, b):\n    return a + b\n")
    report = handoff("verify", str(failed_pack), "--repo", str(repo), expect=1)
    print("after editing the source:", report["status"], report["snapshot"]["changes"])

    passed_run = OUTPUT / "passed-test.json"
    print(handoff("run", "--repo", str(repo), "--output", str(passed_run), "--label", "unit tests", *test))
    task = OUTPUT / "final-task.json"
    task.write_text(json.dumps({"goal": "SAMPLE DATA: corrected calculator",
                                "completed": ["Changed addition and ran the recorded test"], "pending": [],
                                "blockers": [], "next": ["Review the patch and commit if appropriate"]}, indent=2))
    passed_pack = OUTPUT / "passed-handoff"
    print(handoff("pack", "--repo", str(repo), "--task", str(task), "--evidence", str(passed_run),
                  "--output", str(passed_pack)))
    report = handoff("verify", str(passed_pack), "--repo", str(repo))
    print("new pack:", report["status"])
    print(f"Hand {passed_pack / 'handoff.md'} and {passed_pack / 'handoff.json'} to the next agent.")


if __name__ == "__main__":
    main()
