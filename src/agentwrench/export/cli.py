"""CLI: read explicit files, write only the explicitly requested output."""
import argparse
from contextlib import closing
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
import sys
from . import __version__
from .adapters import load_session, validate_session
from .export import handoff, jsonl, markdown, prepare, to_atif
from .model import InputError, dumps
from .render_merge import render_merge, timestamp_window, validate_merge
from .merge import merge_jsonl, timestamp_instant
from .._text import cannot_read, plural, safe_path
from ..mcpconfig.core import TOKEN


def _stats_line(diagnostics: dict, steps: int, output: Path | None) -> str:
    """One readable line: what was written, warnings, redactions and truncations."""
    where = f'wrote {label(output)}: ' if output else ''
    parts = [plural(steps, 'step')]
    warnings = diagnostics['warnings']
    total = sum(warnings.values())
    parts.append(plural(total, 'warning') + (f" ({', '.join(f'{k} {v}' for k, v in sorted(warnings.items()))})"
                                              if total else ''))
    redaction = diagnostics['redaction']
    if redaction.get('mode') == 'off':
        parts.append('redaction off')
    else:
        parts.append(f"{plural(redaction.get('total', 0), 'value')} redacted")
    if diagnostics['truncated_fields']:
        parts.append(f"{plural(diagnostics['truncated_fields'], 'tool result')} truncated")
    return where + ', '.join(parts)


def _is_input(args, exc: OSError) -> bool:
    """The failing path is an input file named on the command line (not an output)."""
    if not exc.filename or isinstance(exc, FileExistsError):
        return False
    inputs = [getattr(args, 'session', None), *(getattr(args, 'sessions', None) or [])]
    return any(path is not None and os.fspath(path) == os.fspath(exc.filename) for path in inputs)


def label(path) -> str:
    # Name the file as the user gave it, without terminal controls or obvious tokens.
    return safe_path(TOKEN.sub('[REDACTED]', os.fspath(path)))


def parser():
    p = argparse.ArgumentParser(prog='agentwrench export', description='Offline session conversion and redacted hand-off briefs')
    p.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    subs = p.add_subparsers(dest='action', required=True)
    helps = {'convert': 'convert a Codex, Claude Code or Cline session to ATIF, Markdown or agentwrench export JSONL',
             'summary': 'write a redacted Markdown hand-off brief for one session'}
    for command in ('convert', 'summary'):
        sub = subs.add_parser(command, help=helps[command], description=helps[command])
        sub.add_argument('session', type=Path)
        sub.add_argument('--from', dest='fmt', choices=('auto', 'codex', 'claude', 'cline'), default='auto')
        sub.add_argument('--redact', choices=('default', 'strict', 'off'), default='default')
        sub.add_argument('--include-sidechains', action='store_true')
        sub.add_argument('--strict-jsonl', action='store_true',
                         help='reject malformed final JSONL records instead of recovering a truncated write')
        sub.add_argument('--max-output-chars', type=int, default=20000,
                         help='limit characters per tool result; 0 is unlimited (default: 20000)')
        sub.add_argument('-o', '--output', type=Path, help='create a new private output file (never overwrite)')
        sub.add_argument('--stats-json', action='store_true',
                         help='print warning/redaction counts on stderr as one JSON object instead of a sentence')
        if command == 'convert':
            sub.add_argument('--to', choices=('atif', 'md', 'jsonl'), required=True)
            sub.add_argument('--view', choices=('conversation', 'tools', 'full'), default='full')
    validate = subs.add_parser('validate', help='strictly validate raw Codex/Claude JSONL without an export')
    validate.add_argument('session', type=Path)
    validate.add_argument('--from', dest='fmt', choices=('auto', 'codex', 'claude'), default='auto')
    validate.add_argument('--include-sidechains', action='store_true')
    validate.add_argument('--fail-on-warning', action='store_true',
                          help='exit 2 for counted adapter warnings; still report the same JSON counts')
    merge = subs.add_parser('merge', help='merge agentwrench export JSONL files (format trajport/v1), optionally sorting unsorted inputs')
    merge.add_argument('sessions', type=Path, nargs='+')
    merge.add_argument('--check', action='store_true',
                       help='validate all inputs and report counts without staging or writing output; '
                            'still uses temporary index/run storage; '
                            'conflicts with --output/--output-temp-dir/--max-output-bytes/--since/--until')
    merge.add_argument('--since', metavar='TIMESTAMP',
                       help='inclusive lower timestamp bound (merge timestamp syntax); '
                            'all input events are still validated; conflicts with --check')
    merge.add_argument('--until', metavar='TIMESTAMP',
                       help='exclusive upper timestamp bound; equal bounds allow a metadata-only export; '
                            'all input events are still validated; conflicts with --check')
    merge.add_argument('--external-sort', action='store_true',
                       help='sort unsorted normalized exports using bounded temporary disk runs')
    merge.add_argument('--sort-buffer-bytes', type=int,
                       help='encoded event bytes per sort run, 1–67108864 (default: 8388608); requires --external-sort')
    merge.add_argument('--max-sort-temp-bytes', type=int,
                       help='positive live run-byte quota including merge overlap (default: unlimited); '
                            'excludes identity index/output; requires --external-sort')
    merge.add_argument('--sort-temp-dir', metavar='PATH',
                       help='existing writable root for private identity index in either mode and '
                            'external-sort runs (default: platform temp); independent of stdout staging')
    merge.add_argument('--output-temp-dir', metavar='PATH',
                       help='existing writable root for private stdout staging only (default: platform temp); '
                            'independent of index/runs; conflicts with --check/--output')
    merge.add_argument('--max-output-bytes', type=int,
                       help='positive total UTF-8 output-byte limit including header/LF '
                            '(default: unlimited); independent of sort run quota')
    merge.add_argument('-o', '--output', type=Path, help='create a new private output file (never overwrite)')
    validate_merged = subs.add_parser('validate-merge', help='validate one merged export (format trajport/merge-v1) without rendering or staging')
    validate_merged.add_argument('session', type=Path, help='one merged agentwrench export JSONL file (format trajport/merge-v1); no originals required')
    render = subs.add_parser('render-merge', help='render a validated merged export (format trajport/merge-v1) as Markdown')
    render.add_argument('session', type=Path, help='one merged agentwrench export JSONL file (format trajport/merge-v1)')
    render.add_argument('--view', choices=('conversation', 'tools', 'full'), default='full',
                        help='normalized presentation (default: full); combine with source/time selection; '
                             'all events and hidden fields are still validated')
    render.add_argument('--source', type=int, action='append', metavar='N',
                        help='select a declared positive input ordinal; repeatable, repeats ignored '
                             '(default: all); all events are still validated')
    render.add_argument('--since', metavar='TIMESTAMP',
                        help='inclusive lower RFC3339 timestamp bound; combine with --source; '
                             'all events are still validated')
    render.add_argument('--until', metavar='TIMESTAMP',
                        help='exclusive upper RFC3339 timestamp bound; equal bounds show no events; '
                             'all events are still validated')
    render.add_argument('-o', '--output', type=Path, help='create a new private Markdown file (never overwrite)')
    render.add_argument('--output-temp-dir', metavar='PATH',
                        help='existing writable stdout staging root; conflicts with --output')
    render.add_argument('--max-output-bytes', type=int,
                        help='positive total Markdown UTF-8 byte limit (default: unlimited)')
    return p


def _stdout_stage(root):
    if root is None:
        stage = tempfile.TemporaryFile(mode='w+', encoding='utf-8', newline='\n')
    else:
        root_error = 'output temporary directory must be an existing writable directory'
        if not root or '\0' in root:
            raise InputError(root_error)
        # Create directly in the selected root; no access-check race or
        # default-root fallback. TemporaryFile is private and cleans itself.
        try:
            stage = tempfile.TemporaryFile(mode='w+', encoding='utf-8', newline='\n',
                                           dir=root)
        except (OSError, ValueError):
            raise InputError(root_error) from None
    return stage


def _render_merge(args):
    if args.output_temp_dir is not None and args.output is not None:
        raise InputError('--output-temp-dir cannot be combined with --output')
    if args.max_output_bytes is not None and args.max_output_bytes <= 0:
        raise InputError('max output bytes must be a positive integer')
    if args.source is not None and any(n <= 0 for n in args.source):
        raise InputError('source ordinal must be a positive integer')
    timestamp_window(args.since, args.until)
    if args.output is not None:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                diagnostics = render_merge(args.session, stream, max_output_bytes=args.max_output_bytes,
                                           selected_sources=args.source, since=args.since, until=args.until,
                                           view=args.view)
        except BaseException:
            args.output.unlink(missing_ok=True)
            raise
    else:
        with _stdout_stage(args.output_temp_dir) as stream:
            diagnostics = render_merge(args.session, stream, max_output_bytes=args.max_output_bytes,
                                       selected_sources=args.source, since=args.since, until=args.until,
                                       view=args.view)
            stream.seek(0)
            shutil.copyfileobj(stream, sys.stdout)
    sys.stderr.write('agentwrench export: ' + dumps(diagnostics) + '\n')
    return 0


def _merge(args):
    # Stage stdout on disk, so even a late identity conflict publishes no prefix.
    # A failed file output is removed; O_EXCL preserves existing files/symlinks.
    if args.output_temp_dir is not None:
        if args.check:
            raise InputError('--output-temp-dir cannot be combined with --check')
        if args.output is not None:
            raise InputError('--output-temp-dir cannot be combined with --output')
    if args.check and args.output is not None:
        raise InputError('--check cannot be combined with --output')
    if args.check and args.max_output_bytes is not None:
        raise InputError('--check cannot be combined with --max-output-bytes')
    if args.max_output_bytes is not None and args.max_output_bytes <= 0:
        raise InputError('max output bytes must be a positive integer')
    bounds = []
    for name in ('since', 'until'):
        value = getattr(args, name)
        if args.check and value is not None:
            raise InputError(f'--check cannot be combined with --{name}')
        try:
            bounds.append(None if value is None else timestamp_instant(value))
        except ValueError:
            raise InputError(f'--{name} requires RFC3339 with timezone and at most 6 fractional digits') from None
    since, until = bounds
    if since is not None and until is not None and since > until:
        raise InputError('--since must be before or equal to --until')
    filtered = since is not None or until is not None
    count = excluded = 0

    def consume(stream=None):
        nonlocal count, excluded
        used = 0
        with closing(merge_jsonl(args.sessions, external_sort=args.external_sort,
                                 sort_buffer_bytes=args.sort_buffer_bytes,
                                 max_sort_temp_bytes=args.max_sort_temp_bytes,
                                 sort_temp_dir=args.sort_temp_dir)) as rows:
            for row in rows:
                # The iterator validates every event and indexes its identity
                # before yielding. Keep consuming beyond the upper bound so
                # excluded records and malformed tails cannot hide failures.
                if filtered and row['type'] == 'event':
                    instant = timestamp_instant(row['step']['timestamp'])
                    if ((since is not None and instant < since)
                            or (until is not None and instant >= until)):
                        excluded += 1
                        continue
                if stream is not None:
                    text = dumps(row) + '\n'
                    if args.max_output_bytes is not None:
                        size = len(text.encode('utf-8'))
                        if used + size > args.max_output_bytes:
                            raise InputError('merge output byte limit exceeded')
                        used += size
                    stream.write(text)
                count += row['type'] == 'event'

    if args.check:
        consume()
    elif args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                consume(stream)
        except BaseException:
            args.output.unlink(missing_ok=True)
            raise
    else:
        stage = _stdout_stage(args.output_temp_dir)
        with stage as stream:
            consume(stream)
            stream.seek(0)
            shutil.copyfileobj(stream, sys.stdout)
    diagnostics = {'merged_events': count, 'sources': len(args.sessions)}
    if filtered:
        diagnostics['excluded_events'] = excluded
    sys.stderr.write('agentwrench export: ' + dumps(diagnostics) + '\n')
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'validate-merge':
            diagnostics = validate_merge(args.session)
            sys.stderr.write('agentwrench export: ' + dumps(diagnostics) + '\n')
            return 0
        if args.action == 'render-merge':
            return _render_merge(args)
        if args.action == 'merge':
            return _merge(args)
        if args.action == 'validate':
            diagnostics = validate_session(args.session, args.fmt,
                                           include_sidechains=args.include_sidechains)
            sys.stderr.write('agentwrench export: ' + dumps(diagnostics) + '\n')
            return 2 if args.fail_on_warning and diagnostics['warning_count'] else 0
        doc = load_session(args.session, args.fmt, include_sidechains=args.include_sidechains,
                           strict_jsonl=args.strict_jsonl)
        doc = prepare(doc, view=getattr(args, 'view', 'full'), redact=args.redact,
                      max_output_chars=args.max_output_chars)
        if args.action == 'summary':
            output = handoff(doc)
        else:
            output = {'atif': lambda: dumps(to_atif(doc), pretty=True) + '\n',
                      'md': lambda: markdown(doc), 'jsonl': lambda: jsonl(doc)}[args.to]()
        if args.output:
            # O_EXCL refuses existing files and symlinks; no input-derived paths.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(output)
            except BaseException:
                args.output.unlink(missing_ok=True)
                raise
        else:
            sys.stdout.write(output)
        diagnostics = {'warnings': doc['extra']['warnings'], 'redaction': doc['extra']['redaction'],
                       'truncated_fields': doc['extra']['truncated_fields']}
        if args.stats_json:
            sys.stderr.write('agentwrench export: ' + dumps(diagnostics) + '\n')
        else:
            sys.stderr.write('agentwrench export: ' + _stats_line(diagnostics, len(doc['steps']), args.output) + '\n')
        return 0
    except BrokenPipeError:
        return 0
    except InputError as exc:
        print(f'agentwrench export: error: {exc}', file=sys.stderr)
        return 2
    except OSError as exc:
        if _is_input(args, exc):
            print(f"agentwrench export: error: {cannot_read(TOKEN.sub('[REDACTED]', os.fspath(exc.filename)), exc)}", file=sys.stderr)
        else:
            print(f'agentwrench export: error: cannot process input/output ({type(exc).__name__})', file=sys.stderr)
        return 2
    except (RecursionError, ValueError, TypeError, sqlite3.Error) as exc:
        # OS errors may contain private paths; JSON errors may contain source data.
        print(f'agentwrench export: error: cannot process input/output ({type(exc).__name__})', file=sys.stderr)
        return 2
