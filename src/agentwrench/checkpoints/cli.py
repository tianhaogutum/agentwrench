"""Command line entry point. No graph imports, network calls, or workflow execution."""
import argparse
import json
import sqlite3
import sys

from .budget import budget_report, load_policy, load_snapshot, snapshot_report
from .compare_plans import DIFFERENCES, compare_plans
from .db import ScopeError, connect, read_rows
from .inspect import diff_report, stats_report, threads_report
from .prune import prune
from .plan_status import plan_status


def difference_limit(value):
    try:
        if not value or any(char not in '0123456789' for char in value):
            raise ValueError
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a nonnegative ASCII decimal integer') from None


def parser():
    p = argparse.ArgumentParser(prog="agentwrench checkpoints", description="Offline LangGraph SQLite checkpoint inspector")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("threads", "stats", "diff", "prune", "snapshot", "plan-status"):
        command = sub.add_parser(name)
        command.add_argument("db", help="existing SqliteSaver database (never created implicitly)")
        command.add_argument("--json", action="store_true", help="machine-readable JSON")
        if name not in ("threads", "plan-status"):
            command.add_argument("--thread", required=name == "diff")
        if name == "plan-status":
            command.add_argument("--plan", required=True, help="saved plan to diagnose without modifying the database")
            command.add_argument("--max-differences", type=difference_limit, metavar="N",
                                 help="show at most N entries per identity list with exact totals (0: counts only)")
        if name == "stats":
            command.add_argument("--top", type=int, default=10)
        if name == "diff":
            command.add_argument("--namespace", default="", help="checkpoint namespace (default: empty)")
            command.add_argument("--from", dest="from_id")
            command.add_argument("--to", dest="to_id")
            command.add_argument("--last", type=int, choices=[2])
        if name == "prune":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--keep-latest", type=int)
            source.add_argument("--apply-plan", help="saved plan to revalidate and apply")
            command.add_argument("--export-plan", help="new plan file; requires --dry-run")
            command.add_argument("--dry-run", action="store_true")
            backups = command.add_mutually_exclusive_group()
            backups.add_argument("--backup", help="new backup file; existing paths are never overwritten")
            backups.add_argument("--no-backup", action="store_true")
            command.add_argument("--vacuum", action="store_true", help="compact after verified commit")
    gate = sub.add_parser("budget", help="compare saved snapshots against per-group byte growth limits")
    gate.add_argument("baseline", help="baseline snapshot JSON")
    gate.add_argument("current", help="current snapshot JSON")
    gate.add_argument("--policy", help="versioned JSON policy with defaults and exact group overrides")
    gate.add_argument("--require-policy-match", action="store_true",
                      help="require every policy override to match a group in either snapshot (requires --policy)")
    gate.add_argument("--show-policy-coverage", action="store_true",
                      help="report coverage counts and unused exact selectors (requires --policy)")
    gate.add_argument("--max-unused-selectors", type=difference_limit, metavar="N",
                      help="show at most N unused selectors per kind with exact totals "
                           "(requires --show-policy-coverage and --policy; 0: counts only)")
    gate.add_argument("--show-byte-components", action="store_true",
                      help="show baseline/current/growth checkpoint or channel-value and write bytes per row")
    gate.add_argument("--show-group-presence", action="store_true",
                      help="show exact baseline/current membership for each compared group")
    gate.add_argument("--show-group-presence-summary", action="store_true",
                      help="report complete baseline/current/added/removed/common group counts")
    gate.add_argument("--max-results", type=difference_limit, metavar="N",
                      help="show at most N comparisons and N violations with exact totals (0: counts only)")
    gate.add_argument("--max-thread-growth-bytes", type=int)
    gate.add_argument("--max-channel-growth-bytes", type=int)
    gate.add_argument("--json", action="store_true", help="machine-readable JSON")
    comparison = sub.add_parser("compare-plans", help="compare saved checkpoint actions without database access")
    comparison.add_argument("baseline", help="baseline saved prune plan JSON")
    comparison.add_argument("current", help="current saved prune plan JSON")
    comparison.add_argument("--max-differences", type=difference_limit, metavar="N",
                            help="show at most N entries per difference list with exact totals (0: counts only)")
    comparison.add_argument("--json", action="store_true", help="machine-readable JSON")
    return p


def quoted(value):
    return json.dumps(value, ensure_ascii=True)


def render(command, result):
    if command == "threads":
        print("thread\tnamespaces\tcheckpoints\twrites\tbytes\topaque\tfirst\tlast")
        for row in result:
            print("\t".join(str(v) for v in [quoted(row["thread_id"]), quoted(row["checkpoint_ns"]),
                  row["checkpoints"], row["writes"], row["bytes"], row["opaque_checkpoints"],
                  quoted(row["first"]), quoted(row["last"])]))
    elif command == "stats":
        print(f"{result['checkpoints']} checkpoints, {result['writes']} writes, {result['bytes']} payload bytes")
        print(f"opaque: {result['opaque_checkpoints']} checkpoints, {result['opaque_writes']} writes ({result['opaque_bytes']} bytes)")
        print("channel\tserialized state bytes\twrite bytes\tcombined attribution")
        for row in result["channels"]:
            print(f"{quoted(row['channel'])}\t{row['checkpoint_value_bytes']}\t{row['write_bytes']}\t{row['bytes']}")
        print("largest checkpoints (checkpoint + associated writes):")
        for row in result["largest_checkpoints"]:
            print(f"  {quoted(row['thread_id'])} {quoted(row['checkpoint_ns'])} {quoted(row['checkpoint_id'])}: {row['bytes']}")
        print("growth per step (ordered by checkpoint id; first step has no delta):")
        for row in result["growth_per_step"]:
            delta = "n/a" if row["growth_bytes"] is None else f"{row['growth_bytes']:+d}"
            print(f"  {quoted(row['thread_id'])} {quoted(row['checkpoint_ns'])} {quoted(row['checkpoint_id'])}: {row['bytes']} ({delta})")
    elif command == "diff":
        print(f"{quoted(result['from'])} -> {quoted(result['to'])}")
        if result["opaque"]:
            print(result["error"])
        for change in result["changes"]:
            print(f"{quoted(change['channel'])}: {change['kind']} {change.get('summary', '')}")
            if "before" in change:
                print(f"  before: {change['before']}")
            if "after" in change:
                print(f"  after:  {change['after']}")
    elif command == "snapshot":
        print("retained-history byte snapshot (use --json to save for budget)")
        for name in ("threads", "channels"):
            for row in result[name]:
                identity = " ".join(quoted(row[f]) for f in ("thread_id", "checkpoint_ns", "channel") if f in row)
                print(f"  {name}: {identity}: {row['bytes']} bytes")
    elif command == "budget":
        print("growth budget: " + ("PASS" if result["passed"] else "EXCEEDED"))
        sections = ("comparisons", "violations") if "result_summary" in result else ("comparisons",)
        for name in sections:
            if "result_summary" in result:
                counts = result["result_summary"][name]
                print(f"{name}: {counts['total']} total, {counts['shown']} shown, "
                      f"truncated: {str(counts['truncated']).lower()}")
            for row in result[name]:
                identity = " ".join(quoted(row[f]) for f in ("thread_id", "checkpoint_ns", "channel") if f in row)
                source = f" ({row['limit_source']})" if "limit_source" in row else ""
                presence = (f"; baseline_present: {str(row['baseline_present']).lower()}, "
                            f"current_present: {str(row['current_present']).lower()}"
                            if "baseline_present" in row else "")
                components = ("; byte_components: " + ", ".join(
                    f"{component} {sizes['baseline_bytes']} -> {sizes['current_bytes']} bytes "
                    f"(growth {sizes['growth_bytes']:+d})"
                    for component, sizes in row["byte_components"].items())
                    if "byte_components" in row else "")
                print(f"  {row['group']} {identity}: {row['baseline_bytes']} -> {row['current_bytes']} bytes; "
                      f"growth {row['growth_bytes']:+d}, limit {row['limit_bytes']}{source}, "
                      f"exceeded by {row['exceeded_bytes']} bytes{presence}{components}")
        if "group_presence_summary" in result:
            print("group presence summary:")
            for name, counts in result["group_presence_summary"].items():
                print(f"  {name}: {counts['baseline']} baseline, {counts['current']} current, "
                      f"{counts['added']} added, {counts['removed']} removed, {counts['common']} common")
        if "policy_coverage" in result:
            print("policy coverage:")
            for name, coverage in result["policy_coverage"].items():
                print(f"  {name}: {coverage['total_overrides']} total overrides, "
                      f"{coverage['covered_overrides']} covered, {coverage['unused_overrides']} unused")
                if "unused_selectors_summary" in coverage:
                    counts = coverage["unused_selectors_summary"]
                    print(f"    unused selectors: {counts['total']} total, {counts['shown']} shown, "
                          f"truncated: {str(counts['truncated']).lower()}")
                for selector in coverage["unused_selectors"]:
                    print("    unused: " + ", ".join(f"{field}={quoted(value)}" for field, value in selector.items()))
    elif command == "compare-plans":
        print("prune plan actions: " + ("IDENTICAL" if result["actions_identical"] else "DIFFERENT"))
        print(f"checkpoint scope: {quoted(result['scope'])}")
        for field in DIFFERENCES:
            if "difference_summary" in result:
                counts = result["difference_summary"][field]
                print(f"{field}: {counts['total']} total, {counts['shown']} shown, "
                      f"truncated: {str(counts['truncated']).lower()}")
            else:
                print(f"{field}: {len(result[field])}")
            for row in result[field]:
                identity = " ".join(quoted(row[f]) for f in ("thread_id", "checkpoint_ns", "checkpoint_id"))
                action = f" {row['action']}" if "action" in row else ""
                print(f"  {identity}{action}")
    elif command == "plan-status":
        def counts(field):
            if result[field] is None:
                return "unavailable"
            if "difference_summary" in result:
                summary = result["difference_summary"][field]
                return (f"{summary['total']} total, {summary['shown']} shown, "
                        f"truncated: {str(summary['truncated']).lower()}")
            return str(len(result[field]))

        print("prune plan binding: " + ("UNCHANGED" if result["binding_matches"] else "STALE"))
        for field in ("path_changed", "schema_changed", "rows_changed"):
            value = result[field]
            print(f"{field}: " + ("unknown" if value is None else str(value).lower()))
        print(f"checkpoint scope: {quoted(result['scope'])}")
        for kind in ("added", "removed"):
            rows = result[f"{kind}_checkpoints"]
            print(f"{kind} checkpoints: " + counts(f"{kind}_checkpoints"))
            for row in rows or []:
                print("  " + " ".join(quoted(row[f]) for f in ("thread_id", "checkpoint_ns", "checkpoint_id")))
        localization = result["row_localization"]
        label = {"database": "whole database", "unavailable-v1": "unavailable (v1 plan)",
                 "unavailable-layout": "unavailable (table layout)"}[localization]
        print(f"per-row localization: {label}")
        print(f"database checkpoint membership localization: {label}")
        for kind in ("added", "removed"):
            rows = result[f"{kind}_database_checkpoints"]
            print(f"{kind} database checkpoints: " + counts(f"{kind}_database_checkpoints"))
            for row in rows or []:
                print("  " + " ".join(quoted(row[f]) for f in ("thread_id", "checkpoint_ns", "checkpoint_id")))
        print(f"write membership localization: {label}")
        for kind in ("added", "removed"):
            rows = result[f"{kind}_writes"]
            print(f"{kind} writes: " + counts(f"{kind}_writes"))
            for row in rows or []:
                print("  " + " ".join(quoted(row[f]) for f in ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx")))
        for table, fields in (("checkpoints", ("thread_id", "checkpoint_ns", "checkpoint_id")),
                              ("writes", ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"))):
            rows = result[f"changed_{table}"]
            print(f"changed {table}: " + counts(f"changed_{table}"))
            for row in rows or []:
                print("  " + " ".join(quoted(row[f]) for f in fields))
        if "rows_unavailable_reason" in result:
            print(result["rows_unavailable_reason"])
        print("read-only diagnosis; apply-plan must revalidate before execution")
    else:
        verb = "would delete" if result["dry_run"] else "deleted"
        print(f"{verb} {result['delete_checkpoints']} checkpoints, {result['delete_writes']} writes "
              f"(~{result['reclaimable_payload_bytes']} payload bytes + {result['reclaimable_metadata_bytes']} metadata bytes)")
        print(result["verification"])
        for row in result["latest"]:
            print(f"  {quoted(row['thread_id'])} {quoted(row['checkpoint_ns'])}: {row['state_sha256']}")
        if "backup" in result:
            print(f"backup: {quoted(result['backup'])}")
        if "exported_plan" in result:
            print(f"exported plan: {quoted(result['exported_plan'])}")
        if "plan_source" in result:
            print(f"saved plan: {quoted(result['plan_source'])}")
        print(f"vacuum: {result['vacuum']}")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "compare-plans":
            result = compare_plans(args.baseline, args.current, max_differences=args.max_differences)
        elif args.command == "budget":
            result = budget_report(load_snapshot(args.baseline), load_snapshot(args.current),
                                   max_thread_growth_bytes=args.max_thread_growth_bytes,
                                   max_channel_growth_bytes=args.max_channel_growth_bytes,
                                   policy=load_policy(args.policy) if args.policy is not None else None,
                                   require_policy_match=args.require_policy_match,
                                   show_policy_coverage=args.show_policy_coverage,
                                   max_unused_selectors=args.max_unused_selectors,
                                   max_results=args.max_results,
                                   show_group_presence=args.show_group_presence,
                                   show_group_presence_summary=args.show_group_presence_summary,
                                   show_byte_components=args.show_byte_components)
        elif args.command == "plan-status":
            result = plan_status(args.db, args.plan, max_differences=args.max_differences)
        elif args.command == "prune":
            result = prune(args.db, args.keep_latest, thread=args.thread, dry_run=args.dry_run,
                           backup=args.backup, no_backup=args.no_backup, vacuum=args.vacuum,
                           export_plan=args.export_plan, apply_plan=args.apply_plan)
        else:
            if args.command == "stats" and args.top < 1:
                raise ScopeError("--top must be >= 1")
            if args.command == "diff":
                if bool(args.from_id) != bool(args.to_id) or (args.last and args.from_id):
                    raise ScopeError("use --from ID --to ID or --last 2")
            with connect(args.db) as con:
                cps, writes = read_rows(con, getattr(args, "thread", None))
                if args.command == "threads":
                    result = threads_report(cps, writes)
                elif args.command == "snapshot":
                    result = snapshot_report(cps, writes, thread=args.thread)
                elif args.command == "stats":
                    result = stats_report(cps, writes, args.top)
                else:
                    result = diff_report(cps, namespace=args.namespace, from_id=args.from_id, to_id=args.to_id)
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        else:
            render(args.command, result)
        if args.command == "budget":
            return 0 if result["passed"] else 3
        if args.command == "compare-plans":
            return 0 if result["actions_identical"] else 3
        if args.command == "plan-status":
            return 0 if result["binding_matches"] else 3
        return 2 if (isinstance(result, dict) and (result.get("opaque") or result.get("vacuum_failed"))) else 0
    except ScopeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except sqlite3.Error as exc:
        # SQLite errors are classified without leaking schema SQL or state data.
        name = getattr(exc, "sqlite_errorname", "SQLITE_ERROR")
        detail = "database locked/busy; no prune changes committed" if name in ("SQLITE_BUSY", "SQLITE_LOCKED") else "invalid/unreadable SQLite database or operation failed"
        print(f"error: {detail} ({name})", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: file operation failed ({exc.strerror or type(exc).__name__})", file=sys.stderr)
        return 2
