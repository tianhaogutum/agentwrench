"""Read-only saved-plan drift diagnostics; never expose raw row values."""
from pathlib import Path

from .db import KEY, ScopeError, connect, key, read_rows, validate_tables
from .prune import plan
from .prune_plan import FORMAT as PLAN_FORMAT, WRITE_KEY, artifact, database_binding, load_plan, row_fingerprints, schema_fingerprint

FORMAT = "lgckpt-scope.plan-status/v2"


DIFFERENCES = ("added_checkpoints", "removed_checkpoints", "added_database_checkpoints",
               "removed_database_checkpoints", "added_writes", "removed_writes",
               "changed_checkpoints", "changed_writes")


def bounded_result(result, limit):
    if limit is not None:
        result["max_differences"] = limit
        result["difference_summary"] = {}
        for field in DIFFERENCES:
            rows = result[field]
            if rows is None:
                result["difference_summary"][field] = None
                continue
            total = len(rows)
            result[field] = rows[:limit]
            shown = len(result[field])
            result["difference_summary"][field] = {
                "total": total, "shown": shown, "truncated": shown < total}
    return result


def plan_status(path, plan_path, *, max_differences=None):
    if max_differences is not None and (type(max_differences) is not int or max_differences < 0):
        raise ScopeError('--max-differences must be a nonnegative integer')
    saved = load_plan(plan_path)
    v2 = saved["format"] == PLAN_FORMAT
    with connect(path, validate_schema=False) as con:
        result = {
            "format": FORMAT if v2 else "lgckpt-scope.plan-status/v1", "binding_matches": False,
            "path_changed": str(Path(path).expanduser().resolve(strict=True)) != saved["database"]["path"],
            "schema_changed": schema_fingerprint(con) != saved["database"]["schema_sha256"],
            "rows_changed": None, "scope": saved["scope"].copy(),
            "added_checkpoints": None, "removed_checkpoints": None,
            "added_database_checkpoints": None, "removed_database_checkpoints": None,
            "row_localization": "database" if v2 else "unavailable-v1",
            "changed_checkpoints": None, "changed_writes": None,
            "added_writes": None, "removed_writes": None,
        }
        try:
            validate_tables(con)
        except ScopeError:
            result["row_localization"] = "unavailable-layout"
            result["rows_unavailable_reason"] = "unsupported checkpoint/write table layout"
            return bounded_result(result, max_differences)
        cps, writes = read_rows(con)
        binding = database_binding(con, path, cps, writes)
        result["rows_changed"] = binding["rows_sha256"] != saved["database"]["rows_sha256"]
        thread = saved["scope"]["thread"]
        selected = [r for r in cps if thread is None or r["thread_id"] == thread]
        previous = {key(r) for r in saved["checkpoints"]}
        current = {key(r) for r in selected}
        result["added_checkpoints"] = [dict(zip(KEY, k)) for k in sorted(current - previous)]
        result["removed_checkpoints"] = [dict(zip(KEY, k)) for k in sorted(previous - current)]
        raw_rows = row_fingerprints(cps, writes) if v2 else None
        if v2:
            for table, fields in (("checkpoints", KEY), ("writes", WRITE_KEY)):
                previous_rows = {tuple(r[f] for f in fields): r["sha256"] for r in saved["raw_rows"][table]}
                current_rows = {tuple(r[f] for f in fields): r["sha256"] for r in raw_rows[table]}
                membership = "database_checkpoints" if table == "checkpoints" else "writes"
                result[f"added_{membership}"] = [dict(zip(fields, k)) for k in sorted(current_rows.keys() - previous_rows.keys())]
                result[f"removed_{membership}"] = [dict(zip(fields, k)) for k in sorted(previous_rows.keys() - current_rows.keys())]
                result[f"changed_{table}"] = [dict(zip(fields, k)) for k in sorted(previous_rows.keys() & current_rows.keys())
                                              if previous_rows[k] != current_rows[k]]
        result["binding_matches"] = binding == saved["database"]
        if result["binding_matches"]:
            # A matching hash alone must not bless edited actions or estimates.
            selected_writes = [r for r in writes if thread is None or r["thread_id"] == thread]
            summary, doomed = plan(selected, selected_writes, saved["keep_latest"])
            if artifact(binding, selected, doomed, summary, thread, raw_rows, saved["format"]) != saved:
                raise ScopeError("invalid prune plan: checkpoint actions or summary or row fingerprints do not match; export a new plan")
        return bounded_result(result, max_differences)
