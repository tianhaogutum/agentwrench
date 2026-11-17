"""Attach an `agentwrench handoff` package to a Portable Handoff capsule.

Runs the workflow from docs/handoff/integrations.md against a throwaway Git
repository below examples/handoff/demo-output/portable-integration/: record a
check, capture the evidence attachment, finalize and validate a Portable Handoff
0.1.0 capsule that references it, then verify the attachment independently.

Requires: pip install 'agentwrench[handoff]' portable-handoff==0.1.0

    python examples/handoff/portable_handoff_recipe.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
OUTPUT = HERE / "demo-output" / "portable-integration"


def environment() -> dict[str, str]:
    """Let the recipe run from a source checkout as well as from an installed package."""
    env = os.environ.copy()
    if (SRC / "agentwrench").is_dir():
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    return env


def main() -> None:
    if importlib.util.find_spec("portable_handoff") is None:
        raise SystemExit("Install portable-handoff 0.1.0 first; it is an optional integration.")
    if OUTPUT.exists():
        raise SystemExit(f"{OUTPUT} already exists; remove or rename it first")
    repo = OUTPUT / "repo"
    repo.mkdir(parents=True)
    commands = []

    def run(argv: list[str], expect: int = 0) -> str:
        result = subprocess.run(argv, cwd=repo, timeout=120, capture_output=True, text=True, env=environment())
        if result.returncode != expect:
            raise SystemExit(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()}")
        commands.append({"argv": argv, "exit_code": result.returncode})
        return result.stdout

    handoff = [sys.executable, "-m", "agentwrench", "handoff"]
    quiet_git = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false"]
    for argv in [["init", "-q"], ["config", "user.name", "Integration"],
                 ["config", "user.email", "integration@example.invalid"]]:
        run([*quiet_git, *argv])
    # .handoff/ is Git-ignored, so outputs inside the checkout do not change the captured state.
    (repo / ".gitignore").write_text(".handoff/\n__pycache__/\n")
    (repo / "retry.py").write_text("def attempts():\n    return 5\n")
    run(["git", "add", "."])
    run([*quiet_git, "commit", "-qm", "integration baseline"])
    task = OUTPUT / "task.json"
    task.write_text(json.dumps({
        "goal": "Cap retries at five", "unresolved": ["Decide jitter policy"], "artifacts": ["retry.py"],
    }))
    log = repo / ".handoff/check.link"
    package = repo / ".handoff/evidence/handoff.json"
    run([*handoff, "record", "--repo", str(repo), "--output", str(log), "--",
         sys.executable, "-B", "-c", "from retry import attempts; assert attempts() == 5"])
    run([*handoff, "capture", "--repo", str(repo), "--task", str(task), "--log", str(log), "--output", str(package)])

    portable = [sys.executable, "-m", "portable_handoff"]
    preflight = json.loads(run([*portable, "preflight", "--cwd", str(repo), "--source-host", "other"]))
    draft = OUTPUT / "draft.json"
    draft.write_text(json.dumps({
        "task": {"goal": "Cap retries at five"},
        "state": {"status": "in_progress", "pending": ["Decide jitter policy"]},
        "files": [{"path": "retry.py"}, {"path": ".handoff/evidence/handoff.json"}],
        "evidence": [{
            "evidence_id": "local-command-evidence", "kind": "agentwrench-handoff",
            "source": ".handoff/evidence/handoff.json",
            "summary": "Local in-toto receipts and Git state; re-run agentwrench handoff verify before relying on them",
            "provenance": "file", "trust": "claimed",
        }],
        "next_action": {"instruction": "Resolve the jitter decision before extending retry behavior"},
    }))
    capsule = repo / ".handoff/capsule.md"
    finalized = json.loads(run([*portable, "finalize", "--preflight", preflight["preflight"],
                                "--draft", str(draft), "--output", str(capsule)]))
    validation = run([*portable, "validate", str(capsule), "--cwd", str(repo)]).strip()

    # The receiving side re-checks the attachment itself instead of trusting the capsule.
    result = json.loads(run([*handoff, "verify", str(package), "--repo", str(repo), "--require-current-pass"]))
    assert result["ok"], result
    report = {
        "portable_handoff_version": "0.1.0", "commands": commands, "finalize": finalized["outcome"],
        "capsule_validation": validation, "attachment_verification": result, "task_completion": "not_assessed",
    }
    (OUTPUT / "integration-results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(OUTPUT), "capsule": str(capsule), "verified": result["ok"]}))


if __name__ == "__main__":
    main()
