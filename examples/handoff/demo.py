"""End-to-end `agentwrench handoff` demo with real Git, Python and in-toto.

Creates a throwaway Git repository below examples/handoff/demo-output/basic/,
records a real check with `record`, packages it with `capture`, verifies it,
then edits the source and shows that `verify` reports drift. Nothing is sent
anywhere. Requires: pip install 'agentwrench[handoff]'.

    python examples/handoff/demo.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
OUTPUT = HERE / "demo-output" / "basic"


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
        raise SystemExit(f"agentwrench handoff {args[0]} exited {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=10)


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"{OUTPUT} already exists; remove or rename it before running the demo")
    repo = OUTPUT / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Demo")
    git(repo, "config", "user.email", "demo@example.invalid")
    (repo / ".gitignore").write_text("__pycache__/\n")
    source = repo / "retry.py"
    source.write_text("def attempts():\n    return 5\n")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "retry baseline")

    # Task claims are explicit input text; the handoff never marks them as verified.
    task = OUTPUT / "task.json"
    task.write_text(json.dumps({
        "goal": "Cap retries at five", "claimed_done": ["The cap is implemented"],
        "unresolved": ["Decide whether to add jitter"], "artifacts": ["retry.py"],
        "verification_commands": [["python", "-c", "from retry import attempts; assert attempts() == 5"]],
    }))
    # Outputs live outside the repository so they do not change the captured state.
    log = OUTPUT / "check.link"
    package = OUTPUT / "handoff.json"
    recorded = handoff(
        "record", "--repo", str(repo), "--output", str(log), "--",
        sys.executable, "-B", "-c", "from retry import attempts; assert attempts() == 5",
    )
    captured = handoff("capture", "--repo", str(repo), "--task", str(task), "--log", str(log), "--output", str(package))
    clean = handoff("verify", str(package), "--repo", str(repo), "--require-current-pass")
    summary = handoff("inspect", str(package))
    assert clean["ok"], clean

    source.write_text("def attempts():\n    return 8\n")
    drifted = handoff("verify", str(package), "--repo", str(repo), "--require-current-pass", expect=1)
    assert not drifted["ok"], drifted

    (OUTPUT / "verification-before.json").write_text(json.dumps(clean, indent=2) + "\n")
    (OUTPUT / "verification-after.json").write_text(json.dumps(drifted, indent=2) + "\n")
    print(json.dumps({
        "handoff": str(package),
        "record": recorded,
        "capture": captured,
        "unresolved": summary["task_claims"]["unresolved"],
        "before": {"ok": clean["ok"], "repository": clean["repository"],
                   "current_pass": clean["evidence"][0]["current_pass"]},
        "after": {"ok": drifted["ok"], "repository": drifted["repository"],
                  "differences": drifted["differences"], "policy_errors": drifted["policy_errors"]},
    }, indent=2))


if __name__ == "__main__":
    main()
