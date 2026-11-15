"""Paired offline evaluations over the existing normalized trajectory model."""
import hashlib
import json
from pathlib import Path
from typing import BinaryIO

from . import __version__
from .model import InputError, string
from .policy import Rule, check, keys, parse_policy, read_policy
from .streams import _finite_float, _unique_object, normalize

MAX_MANIFEST_BYTES = 65536
MAX_TASKS = 200


def load_manifest(path: str | Path) -> dict[str, dict]:
    """Only manifest entries authorize input reads; event payloads never do."""
    path = Path(path)
    with path.open("rb") as handle:
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise InputError("manifest exceeds 64 KiB")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_float=_finite_float,
                         parse_constant=lambda x: (_ for _ in ()).throw(InputError(f"invalid JSON number: {x}")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InputError(f"invalid manifest JSON: {exc}") from exc
    raw = keys(raw, {"schema_version", "tasks"}, {"schema_version", "tasks"}, "manifest")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise InputError("manifest.schema_version: expected integer 1")
    if not isinstance(raw["tasks"], list) or not 1 <= len(raw["tasks"]) <= MAX_TASKS:
        raise InputError("manifest.tasks: expected nonempty list of at most 200 tasks")
    tasks = {}
    for entry in raw["tasks"]:
        entry = keys(entry, {"id", "stream", "source", "root"}, {"id", "stream"}, "task")
        task_id = string(entry["id"], "task.id")
        if task_id in tasks:
            raise InputError(f"duplicate task ID: {task_id}")
        stream = string(entry["stream"], f"task {task_id}.stream")
        if "\x00" in stream:
            raise InputError(f"task {task_id}.stream: NUL is unsupported")
        source = entry.get("source", "auto")
        if source not in ("auto", "codex-exec", "claude-stream"):
            raise InputError(f"task {task_id}.source: unsupported source")
        root = entry.get("root")
        if "root" in entry:
            string(root, f"task {task_id}.root")
            if not root.startswith("/") or "\x00" in root or "\\" in root:
                raise InputError(f"task {task_id}.root: expected absolute POSIX path")
        tasks[task_id] = {"stream": path.parent / stream, "source": source, "root": root}
    return tasks


def _evaluate(task_id: str, entry: dict, rules: list[Rule], side: str) -> dict:
    try:
        trajectory = normalize(entry["stream"], entry["source"], entry["root"])
        report = check(trajectory, rules)
        canonical = json.dumps(trajectory.as_dict(), sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False).encode("ascii")
        report["trajectory_sha256"] = hashlib.sha256(canonical).hexdigest()
        return report
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        raise InputError(f"{side} task {task_id}: {exc}") from exc


def _outcome(old: bool, new: bool) -> str:
    if old and not new:
        return "regressed"
    if not old and new:
        return "improved"
    return "unchanged_pass" if new else "unchanged_fail"


def compare(baseline: str | Path, candidate: str | Path, policy: str | Path | BinaryIO,
            max_regressions: int = 0, *, regression_unit: str = "task") -> dict:
    """Gate pass-to-fail tasks or (task ID, rule ID) pairs by an absolute allowance.

    Improvements never offset regressions. Both sides use the same policy
    snapshot from a file or borrowed binary stream, without seeking or closing
    the stream. Manifest paths always name files. All inputs are validated/
    evaluated before the CLI creates a report.
    """
    if type(max_regressions) is not int or max_regressions < 0:
        raise InputError("max_regressions: expected nonnegative integer")
    if regression_unit not in ("task", "rule"):
        raise InputError("regression_unit: expected task or rule")
    before, after = load_manifest(baseline), load_manifest(candidate)
    if before.keys() != after.keys():
        raise InputError("task IDs differ: missing in candidate=" + json.dumps(sorted(before.keys() - after.keys()))
                         + "; extra in candidate=" + json.dumps(sorted(after.keys() - before.keys())))
    policy_data = read_policy(policy)
    rules = parse_policy(policy_data)
    summary = {"total": len(before), "regressed": 0, "improved": 0,
               "unchanged_pass": 0, "unchanged_fail": 0}
    rule_summary = {"total": 0, "regressed": 0, "improved": 0,
                    "unchanged_pass": 0, "unchanged_fail": 0}
    tasks = []
    for task_id in sorted(before):
        # Normalize/evaluate one trajectory at a time; retain only policy reports.
        baseline_report = _evaluate(task_id, before[task_id], rules, "baseline")
        candidate_report = _evaluate(task_id, after[task_id], rules, "candidate")
        outcome = _outcome(baseline_report["passed"], candidate_report["passed"])
        summary[outcome] += 1
        row = {"id": task_id, "outcome": outcome,
               "baseline": baseline_report, "candidate": candidate_report}
        if regression_unit == "rule":
            # The same policy snapshot and mandatory completeness check give
            # both sides identical rule IDs. Join by ID, never result position.
            old = {r["id"]: r["passed"] for r in baseline_report["results"]}
            new = {r["id"]: r["passed"] for r in candidate_report["results"]}
            row["rules"] = []
            for rule_id in sorted(old):
                outcome = _outcome(old[rule_id], new[rule_id])
                rule_summary[outcome] += 1
                rule_summary["total"] += 1
                row["rules"].append({"id": rule_id, "outcome": outcome,
                                     "baseline_passed": old[rule_id], "candidate_passed": new[rule_id]})
        tasks.append(row)
    observed = (rule_summary if regression_unit == "rule" else summary)["regressed"]
    report = {"schema_version": 1, "trajgate_version": __version__,
              "policy_sha256": hashlib.sha256(policy_data).hexdigest(),
              "regression_unit": regression_unit, "passed": observed <= max_regressions,
              "threshold": {"max_regressions": max_regressions, "observed_regressions": observed},
              "summary": summary, "tasks": tasks}
    if regression_unit == "rule":
        report["rule_summary"] = rule_summary
    return report
