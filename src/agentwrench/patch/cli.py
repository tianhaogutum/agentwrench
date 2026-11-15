"""CLI. Input filenames or '-' for stdin; all conversion commands are read-only."""
import argparse
import json
from pathlib import Path
import sys

from .engine import MAX_FILE_BYTES, plan
from .model import ApplyError, PatchError
from .parser import parse
from .planfile import MAX_PLAN_BYTES, check_plan, load_plan, plan_check_report, preview
from .transaction import commit
from .unified import check_unified, from_unified, to_unified, unified_check_report
from .._text import cannot_read, os_reason, plural, safe_path


class InputReadError(OSError):
    """The patch/plan file named on the command line could not be read."""


def read_input(name: str, limit: int = MAX_FILE_BYTES) -> str:
    if name == '-':
        data = sys.stdin.buffer.read(limit+1)
    else:
        try:
            with Path(name).open('rb') as handle:
                data = handle.read(limit+1)
        except OSError as exc:
            raise InputReadError(exc.errno, exc.strerror, name) from None
    if len(data) > limit:
        raise PatchError(f"input exceeds {limit // (1024 * 1024)} MiB limit")
    return data.decode('utf-8')


def render(diagnostics: list[dict]) -> str:
    result = []
    for d in diagnostics:
        label = f"hunk {d['hunk']} of {d['path']}" if d['hunk'] else d['path']
        result.append(f"{label}: {d['reason']}" + (f" ({d.get('tier')})" if d.get('tier') else ''))
        if 'candidate' in d:
            c = d['candidate']
            result.append(f"  best candidate: lines {c['start_line']}-{c['end_line']} (similarity {c['similarity']:.4f})")
            flags = ', '.join(k for k, v in d['diagnosis'].items() if v) or 'none'
            result.append(f"  diagnosis: {flags}")
            if d['eof_fallback']:
                result.append("  EOF tail did not match; fallback search succeeded")
        result.append('  suggested fix: ' + d['suggested_fix'])
    return '\n'.join(result) or 'No hunk failures.'


def check_json(ok: bool, diagnostics: list[dict]) -> None:
    print(json.dumps({'format': 'v4a-check', 'version': 1, 'ok': ok,
                      'diagnostics': diagnostics}, ensure_ascii=False, indent=2))


def check_error(code: str, exc: Exception) -> None:
    line = getattr(exc, 'line', None)
    check_json(False, [{'code': code, 'path': None, 'destination_path': None,
                        'hunk': None, 'status': 'failed', 'reason': str(exc),
                        'patch_span': {'start_line': line, 'end_line': line+1} if line else None,
                        'source_span': None, 'result_span': None,
                        'suggested_fix': 'Correct the input or target tree and check again.'}])


def plan_check_error(code: str, exc: Exception) -> None:
    print(json.dumps(plan_check_report([{'path': None, 'codes': [code],
        'expected': None, 'actual': None, 'reason': str(exc)}]), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='agentwrench patch', description='Parse, atomically apply, convert, and diagnose V4A patches.')
    commands = parser.add_subparsers(dest='command', required=True)
    helps = {
        'parse': 'parse a V4A patch and print its operations',
        'apply': 'apply a V4A patch atomically under --root (use --dry-run first)',
        'to-unified': 'convert a V4A patch to a unified diff',
        'from-unified': 'convert a unified diff to a V4A patch',
        'explain': 'explain per hunk why a patch does or does not match the files',
        'preview': 'save a hash-bound plan of what apply would change, without writing files',
        'apply-plan': 'apply a saved preview plan if the files are still unchanged',
        'check-plan': 'check whether a saved plan still matches the files (no writes)',
        'check-unified': 'check whether a unified diff applies cleanly (no writes)',
    }
    for name in ('parse', 'apply', 'to-unified', 'from-unified', 'explain', 'preview', 'apply-plan', 'check-plan', 'check-unified'):
        cmd = commands.add_parser(name, help=helps[name], description=helps[name])
        cmd.add_argument('patch', help="UTF-8 patch/plan file, or '-' for stdin")
        if name in ('parse', 'explain', 'check-plan'):
            cmd.add_argument('--json', action='store_true')
        if name in ('apply', 'to-unified', 'explain', 'preview', 'apply-plan', 'check-plan', 'check-unified'):
            cmd.add_argument('--root', required=True, help='explicit target tree')
        if name == 'from-unified':
            cmd.add_argument('--root', help='optional base tree, required for pure renames')
        if name in ('apply', 'apply-plan'):
            validation = cmd.add_mutually_exclusive_group()
            validation.add_argument('--dry-run', action='store_true', help='validate in memory; create no files or directories')
            validation.add_argument('--check-only', action='store_true',
                                    help='read-only validation; emit versioned JSON ' +
                                    ('hunk diagnostics' if name == 'apply' else 'saved-plan diagnostics'))
            cmd.add_argument('--atomic', action='store_true', default=True, help='all-or-nothing (always enabled)')
        if name == 'preview':
            cmd.add_argument('--output', help='create a new JSON plan file (default: stdout); never overwrite')
        if name in ('to-unified', 'from-unified'):
            cmd.add_argument('--context', type=int, default=3, help='1..1000 context lines (default 3)')
    args = parser.parse_args(argv)
    unified_check = args.command == 'check-unified'
    check_only = getattr(args, 'check_only', False)
    saved_plan_check = args.command == 'check-plan' or (args.command == 'apply-plan' and check_only)
    check_plan_json = saved_plan_check and (check_only or args.json)
    try:
        text = read_input(args.patch, MAX_PLAN_BYTES if args.command in ('apply-plan', 'check-plan') else MAX_FILE_BYTES)
        if unified_check:
            result = check_unified(text, args.root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['ok'] else 1
        if saved_plan_check:
            result = check_plan(text, args.root)
            if check_plan_json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print('\n'.join(f"{d['path'] or 'plan'}: {', '.join(d['codes'])}: {d['reason']}"
                                for d in result['diagnostics']) or 'Plan is valid.')
            return 0 if result['ok'] else 1
        if args.command == 'apply-plan':
            prepared = load_plan(text, args.root)
        else:
            prepared = None
        if args.command == 'from-unified':
            sys.stdout.write(from_unified(text, args.context, root=args.root).to_text())
            return 0
        patch = parse(text) if prepared is None else None
        if args.command == 'parse':
            if args.json:
                print(json.dumps(patch.to_dict(), ensure_ascii=False, indent=2))
            else:
                for f in patch.files:
                    print(f"{f.kind} {f.path}" + (f" -> {f.move_to}" if f.move_to else '') + f" ({plural(len(f.hunks), 'hunk')})")
            return 0
        if args.command == 'preview':
            result = preview(patch, args.root)
            if args.output:
                output = Path(args.output).resolve()
                root = Path(args.root).resolve()
                for f in patch.files:
                    for relative in ([f.path, f.move_to] if f.move_to else [f.path]):
                        target = root / relative
                        if output == target or output in target.parents or target in output.parents:
                            raise PatchError('plan output overlaps a patch path')
                with Path(args.output).open('x', encoding='utf-8', newline='') as handle:
                    handle.write(result)
            else:
                sys.stdout.write(result)
            return 0
        if prepared is None:
            prepared = plan(patch, args.root)
        if check_only:
            check_json(prepared.ok, prepared.diagnostics)
            return 0 if prepared.ok else 1
        if args.command == 'explain':
            print(json.dumps({'ok': prepared.ok, 'diagnostics': prepared.diagnostics}, ensure_ascii=False, indent=2)
                  if args.json else render(prepared.diagnostics))
            return 0 if prepared.ok else 1
        if args.command == 'to-unified':
            sys.stdout.write(to_unified(prepared, args.context))
            return 0
        commit(prepared, dry_run=args.dry_run)
        cleanup = [d for d in prepared.diagnostics if d['status'] == 'warning']
        if cleanup:
            print(render(cleanup), file=sys.stderr)
        print(f"{'Validated' if args.dry_run else 'Applied'} {plural(len(prepared.changes), 'file operation')}.")
        return 0
    except (PatchError, UnicodeError) as exc:
        if unified_check:
            print(json.dumps(unified_check_report([], [{'code': 'INVALID_DIFF',
                  'patch_line': getattr(exc, 'line', None)}]), indent=2))
            return 2
        if check_plan_json:
            plan_check_error('INVALID_PLAN', exc)
            return 2
        if check_only:
            check_error('INVALID_PATCH', exc)
            return 2
        print(f"invalid patch: {exc}", file=sys.stderr)
        return 2
    except ApplyError as exc:
        if check_plan_json:
            plan_check_error('VALIDATION_FAILED', exc)
            return 1
        if check_only:
            if exc.diagnostics:
                check_json(False, exc.diagnostics)
            else:
                check_error('VALIDATION_FAILED', exc)
            return 1
        print(str(exc), file=sys.stderr)
        if exc.diagnostics:
            print(render(exc.diagnostics), file=sys.stderr)
        return 1
    except OSError as exc:
        if unified_check:
            print(json.dumps(unified_check_report([], [{'code': 'IO_ERROR', 'patch_line': None}]), indent=2))
            return 1
        if check_plan_json:
            plan_check_error('IO_ERROR', exc)
            return 1
        if check_only:
            check_error('IO_ERROR', exc)
            return 1
        if isinstance(exc, InputReadError):
            print(f"error: {cannot_read(exc.filename, exc)}", file=sys.stderr)
        elif exc.filename:
            print(f"I/O error: {safe_path(exc.filename)}: {os_reason(exc)}", file=sys.stderr)
        else:
            print(f"I/O error: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
