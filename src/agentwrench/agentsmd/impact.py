"""Compare pinned Codex discovery, budget metadata and baseline-relative loss."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .core import UPSTREAM_SHA, Report, ScopeError, absolute, directory, inspect_codex, load_settings
from .._text import plural

MAX_WORKDIRS = 64


def normalize_cwds(cwds: list[Path], command: str = "impact") -> list[Path]:
    """Validate the explicit argument bound, then deduplicate lexical paths."""
    if not cwds or len(cwds) > MAX_WORKDIRS:
        raise ScopeError(f"{command} requires 1 to {MAX_WORKDIRS} --cwd arguments")
    return list(dict.fromkeys(directory(path) for path in cwds))


def _snapshot(report: Report) -> dict:
    # Explicit projection: neither instruction bodies nor heading strings belong
    # in an impact report, including its nested before/after states.
    cuts = {point["path"]: {key: point[key] for key in (
        "byte_offset", "line", "splits_utf8_character")}
        for point in report.truncations}
    rows = []
    for order, row in enumerate(report.entries):
        state = {key: row[key] for key in (
            "path", "scope", "size", "status", "bytes_loaded", "text_bytes")}
        state.update(order=order, cut=cuts.get(row["path"]) if row["scope"] == "project" else None)
        rows.append(state)
    return dict(root=report.root, max_bytes=report.max_bytes,
                project_bytes_used=report.project_bytes_used,
                rendered_bytes=len(report.text.encode("utf-8")), entries=rows,
                diagnostics=report.diagnostics, warnings=report.warnings)


def _changes(before: dict, after: dict) -> list[dict]:
    states = [{(row["scope"], row["path"]): {
        key: value for key, value in row.items() if key not in ("scope", "path")}
        for row in snapshot["entries"]} for snapshot in (before, after)]
    old, new = states
    changes = []
    # Preserve old discovery order, followed by newly discovered paths.
    for scope, path in dict.fromkeys([*old, *new]):
        key = scope, path
        left, right = old.get(key), new.get(key)
        if left != right:
            changes.append(dict(path=path, scope=scope,
                                kind="added" if left is None else "removed" if right is None else "changed",
                                before=left, after=right))
    return changes


def _loss(before: dict, after: dict, homes: list[Path], effective_changed: bool) -> dict | None:
    """Compare loss per file, without allowing unrelated retention gains to net it out."""
    if before["diagnostics"] or after["diagnostics"]:
        return None  # Incomplete discovery cannot establish a retention verdict.
    regressions = []
    if effective_changed:
        states = [{(row["scope"], str(Path(row["path"]).relative_to(home))
                    if row["scope"] == "global" else row["path"]): row
                   for row in snapshot["entries"]}
                  for snapshot, home in zip((before, after), homes)]

        def retained(row: dict) -> int:
            return row.get("text_bytes", 0) if row.get("scope") == "global" else row.get("bytes_loaded", 0)

        def excluded(row: dict) -> int:
            # A skipped whitespace prefix is inside the cut but charges no budget.
            return row["size"] - row["cut"]["byte_offset"] if row.get("cut") else 0

        old, new = states
        for scope, path in dict.fromkeys([*old, *new]):
            left, right = old.get((scope, path), {}), new.get((scope, path), {})
            lost = max(retained(left) - retained(right), excluded(right) - excluded(left), 0)
            file_lost = ((retained(left) > 0 and retained(right) == 0)
                         or (excluded(left) == 0 and excluded(right) > 0
                             and right["status"] == "dropped-over-budget"))
            if lost:
                regressions.append(dict(scope=scope, path=path, newly_lost_bytes=lost,
                                        newly_lost_file=file_lost))
    return dict(effective_changed=effective_changed,
                newly_lost_bytes=sum(row["newly_lost_bytes"] for row in regressions),
                newly_lost_files=sum(row["newly_lost_file"] for row in regressions),
                regressions=regressions)


def inspect_impact(cwds: list[Path], before_home: Path, after_home: Path,
                   before_max_bytes: int | None = None,
                   after_max_bytes: int | None = None, *, fail_on_loss: bool = False) -> dict:
    """Read both homes' config.toml once, then inspect each cwd independently.

    Changed means discovery root or file metadata changed; a budget increase
    alone does not mark an unaffected cwd. Diagnostics always make the CLI fail,
    even when identical in both configurations. No filesystem snapshot is taken.
    The optional loss verdict also compares the exact assembled bodies in memory;
    equal bodies pass, and retention gains cannot cancel losses in other files.
    """
    folders = normalize_cwds(cwds)
    homes = [absolute(before_home), absolute(after_home)]
    configs = [load_settings(home, budget) for home, budget in
               zip(homes, (before_max_bytes, after_max_bytes))]
    directories = []
    for folder in folders:
        reports = [inspect_codex(folder, home, settings, warnings)
                   for home, (settings, warnings) in zip(homes, configs)]
        before, after = map(_snapshot, reports)
        changes = _changes(before, after)
        row = dict(cwd=str(folder), changed=bool(changes or before["root"] != after["root"]),
                   before=before, after=after, changes=changes)
        if fail_on_loss:
            # Compare the existing loader results, not re-reads, lengths, hashes
            # of raw sources, or the metadata-only `changed` flag.
            row["loss"] = _loss(before, after, homes, reports[0].text != reports[1].text)
        directories.append(row)
    result = dict(schema_version=1, agent="codex", semantics=UPSTREAM_SHA,
                before=dict(codex_home=str(homes[0]), settings=asdict(configs[0][0])),
                after=dict(codex_home=str(homes[1]), settings=asdict(configs[1][0])),
                summary=dict(cwd_count=len(directories),
                             changed_cwd_count=sum(d["changed"] for d in directories),
                             error_count=sum(len(d[side]["diagnostics"]) for d in directories
                                             for side in ("before", "after"))),
                directories=directories)
    if fail_on_loss:
        losses = [row["loss"] for row in directories if row["loss"] is not None]
        regressed = sum(loss["newly_lost_bytes"] > 0 for loss in losses)
        result["loss_policy"] = dict(
            name="no-new-loss", passed=None if result["summary"]["error_count"] else regressed == 0,
            regressed_cwd_count=regressed,
            newly_lost_bytes=sum(loss["newly_lost_bytes"] for loss in losses),
            newly_lost_files=sum(loss["newly_lost_files"] for loss in losses))
    return result


def explain_impact(report: dict) -> str:
    summary = report["summary"]
    lines = [f"Codex impact: {summary['changed_cwd_count']}/"
             f"{plural(summary['cwd_count'], 'working directory', 'working directories')} changed"]
    if "loss_policy" in report:
        policy = report["loss_policy"]
        verdict = "ERROR" if policy["passed"] is None else "PASS" if policy["passed"] else "FAIL"
        lines.append(f"{verdict}: no-new-loss; "
                     f"{plural(policy['regressed_cwd_count'], 'working directory', 'working directories')} regressed; "
                     f"{policy['newly_lost_bytes']} newly lost B, {plural(policy['newly_lost_files'], 'newly lost file')}")
    for side in ("before", "after"):
        config = report[side]
        lines.append(f"{side} home: {json.dumps(config['codex_home'])}; "
                     f"budget {config['settings']['max_bytes']} B")

    def describe(state: dict | None) -> str:
        if state is None:
            return "absent"
        cut = state["cut"]
        position = f"cut byte {cut['byte_offset']} (line {cut['line']})" if cut else "no cut"
        return f"{state['status']}, {state['bytes_loaded']}/{state['size']} B, {position}"

    for row in report["directories"]:
        lines.append(f"cwd: {json.dumps(row['cwd'])}")
        if "loss" in row:
            loss = row["loss"]
            if loss is None:
                lines.append("  loss policy unavailable: inspection errors")
            else:
                lines.append(f"  loss: {loss['newly_lost_bytes']} newly lost B, "
                             f"{loss['newly_lost_files']} newly lost files")
                for regression in loss["regressions"]:
                    lines.append(f"    {regression['scope']} {json.dumps(regression['path'])}: "
                                 f"{regression['newly_lost_bytes']} newly lost B")
        before, after = row["before"], row["after"]
        lines.append(f"  project bytes: {before['project_bytes_used']} -> {after['project_bytes_used']}")
        if before["root"] != after["root"]:
            lines.append(f"  root: {json.dumps(before['root'])} -> {json.dumps(after['root'])}")
        if not row["changed"]:
            lines.append("  no file or root changes")
        for change in row["changes"]:
            lines.append(f"  {change['kind']} {change['scope']} {json.dumps(change['path'])}: "
                         f"{describe(change['before'])} -> {describe(change['after'])}")
        for side in ("before", "after"):
            for warning in row[side]["warnings"]:
                lines.append(f"  {side} note: {json.dumps(warning)}")
            for error in row[side]["diagnostics"]:
                lines.append(f"  {side} error: {json.dumps(error['path'])}: {json.dumps(error['error'])}")
    return "\n".join(lines)
