"""Offline CLI. Diagnostics deliberately show field paths, never field values."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .core import (Finding, RAW_TOKEN, TOKEN, diff_paths, document_findings, paths, project, redacted,  # noqa: F401 (kept: public re-export)
                   secret_locations, secret_paths, semantic, to_ir)
from .formats import ConfigError, FORMATS, MAX_BYTES, ROOTS, loads, new_document
from .merge import changes, conflict_details, three_way
from .policy import POLICY_KINDS, POLICY_SCOPES, POLICY_RULES, POLICY_MAX_BYTES, parse_policy, selector_matches
from .relocation import create_manifest, read_manifest, relocate, selection
from .._text import plural


def label(text):
    # Names and keys are untrusted too. Escape terminal controls and obvious tokens.
    text = TOKEN.sub('[REDACTED]', str(text))
    return json.dumps(text, ensure_ascii=True)[1:-1]


def read_file(path: Path, *, max_bytes=MAX_BYTES):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ConfigError('input must be a regular file, not a link/device/pipe')
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ConfigError('configuration exceeds 1 MiB limit' if max_bytes == MAX_BYTES
                              else 'policy file exceeds 64 KiB limit')
        return raw.decode('utf-8')
    except (OSError, UnicodeError):
        raise ConfigError('cannot read UTF-8 regular input file (path/value masked)') from None


def validate_write(path: Path, expected: bytes | None):
    """Read-only preflight shared by writes and relocation previews."""
    absolute = path.absolute()
    if any(parent.is_symlink() for parent in (absolute, *absolute.parents)):
        raise ConfigError('output and parent directories must not be symlinks')
    if not path.parent.is_dir():
        raise ConfigError('output parent directory must already exist')
    backup = path.with_name(path.name + '.bak')
    if expected is not None and os.path.lexists(backup):
        raise ConfigError('backup already exists; archive it before another update')
    if expected is None:
        if os.path.lexists(path):
            raise ConfigError('output appeared concurrently; refusing replacement')
    else:
        if read_file(path).encode('utf-8') != expected:
            raise ConfigError('output changed concurrently; retry after review')


def atomic_write(path: Path, text: str, expected: bytes | None):
    """Same-directory atomic replacement, exclusive .bak, mode 0600, stale-read check."""
    validate_write(path, expected)
    backup = path.with_name(path.name + '.bak')
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.' + path.name + '.', delete=False) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(text.encode('utf-8'))
            tmp.flush()
            os.fsync(tmp.fileno())
        if expected is not None:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
            if read_file(path).encode('utf-8') != expected:
                raise ConfigError('output changed concurrently; backup retained, no replacement made')
            os.replace(temp_path, path)
        else:
            # link is exclusive: a concurrent creator cannot be overwritten.
            os.link(temp_path, path)
            temp_path.unlink()
        temp_path = None
        dirfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        raise ConfigError('atomic output write failed (path/value masked); inspect any retained backup') from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def safe_findings(findings):
    return [{k: TOKEN.sub('[REDACTED]', str(v)) for k, v in item.as_dict().items()}
            for item in findings]


def native_findings(findings):
    """Shared source-native locations; do not interpret display labels."""
    def masked(text):
        return TOKEN.sub('[REDACTED]', text)
    return [{'scope': item.scope,
             'server': masked(item.server) if item.scope == 'server' else None,
             'path': [masked(part) if isinstance(part, str) else part for part in item.path],
             **{key: masked(getattr(item, key)) for key in ('field', 'kind', 'message')}}
            for item in findings]


def print_report(status, *, findings=(), error=None):
    """Opt-in report v1; legacy print_findings remains byte compatible."""
    print(json.dumps({'schemaVersion': 1, 'operation': 'report', 'status': status,
                      'findings': native_findings(findings),
                      'error': TOKEN.sub('[REDACTED]', error) if error else None},
                     indent=2, ensure_ascii=True))


def policy_matches(findings, fail_on, scopes):
    return [kind for kind in POLICY_KINDS if kind in fail_on and any(
        item.kind == kind and item.scope in scopes for item in findings)]


def normalized_rules(rules):
    return [{'kind': kind, 'scope': scope} for kind in POLICY_KINDS for scope in POLICY_SCOPES
            if f'{kind}:{scope}' in rules]


def rule_matches(findings, rules):
    return [rule for rule in normalized_rules(rules) if any(
        item.kind == rule['kind'] and item.scope == rule['scope'] for item in findings)]


def masked_rules(rules):
    return [{**rule,
             **({'server': TOKEN.sub('[REDACTED]', rule['server'])} if 'server' in rule else {}),
             **({'path': [TOKEN.sub('[REDACTED]', p) if isinstance(p, str) else p
                          for p in rule['path']]} if 'path' in rule else {})} for rule in rules]


def print_policy_report(status, fail_on, *, scopes=None, rules=None, exact_rules=None, findings=(), error=None):
    """Explicit versions preserve v1/v2 shapes; v3 selects independent pairs."""
    selected = [kind for kind in POLICY_KINDS if kind in fail_on]
    selected_scopes = [scope for scope in POLICY_SCOPES if scopes is None or scope in scopes]
    matches = None if status == 'error' else policy_matches(findings, selected, selected_scopes)
    decision = 'not-evaluated' if matches is None else 'rejected' if matches else 'accepted'
    policy = {'failOn': selected, **({'scopes': selected_scopes} if scopes is not None else {}),
              'matchedKinds': matches, 'decision': decision}
    version = 2 if scopes is not None else 1
    if rules is not None:
        matches = None if status == 'error' else rule_matches(findings, rules)
        policy = {'rules': normalized_rules(rules), 'matchedRules': matches,
                  'decision': 'not-evaluated' if matches is None else 'rejected' if matches else 'accepted'}
        version = 3
    if exact_rules is not None:
        matches = None if status == 'error' else selector_matches(findings, exact_rules)
        policy = {'rules': masked_rules(exact_rules),
                  'matchedRules': None if matches is None else masked_rules(matches),
                  'decision': 'not-evaluated' if matches is None else 'rejected' if matches else 'accepted'}
        version = 4
    print(json.dumps({'schemaVersion': version,
                      'operation': 'report-policy', 'status': status,
                      'findings': native_findings(findings), 'policy': policy,
                      'error': TOKEN.sub('[REDACTED]', error) if error else None},
                     indent=2, ensure_ascii=True))


def print_check(status, *, drift=(), findings=(), error=None):
    """Value-free check result; preserve literal segments while masking tokens."""
    records = [{**item, 'path': [TOKEN.sub('[REDACTED]', part) for part in item['path']]}
               for item in drift]
    print(json.dumps({'schemaVersion': 2, 'operation': 'check', 'status': status,
                      'drift': records, 'findings': native_findings(findings),
                      'error': TOKEN.sub('[REDACTED]', error) if error else None},
                     indent=2, ensure_ascii=True))


def receipt_json(status, *, manifest_text=None, source_text=None, result=None,
                 provenance=None, error=None):
    """Hash loaded/rendered UTF-8 bytes; errors contain no partial artifact claims."""
    def digest(text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest() if text is not None else None

    return json.dumps({
        'schemaVersion': 1, 'operation': 'relocation-application', 'status': status,
        'manifestSha256': digest(manifest_text), 'sourceSha256': digest(source_text),
        'outputSha256': digest(result), 'provenance': provenance, 'error': error,
    }, indent=2, ensure_ascii=True)


def print_receipt(args, record):
    print(record, file=sys.stdout if args.dry_run else sys.stderr)


def check_drift(expected, actual, same_format):
    """Comparable target-to-source differences; unrelated servers are ignored."""
    records = []
    for server in expected:
        if server.name not in actual:
            records.append({'path': [server.name], 'kind': 'add', 'basis': 'normalized'})
            continue
        target = actual[server.name]
        comparisons = [('normalized', semantic(target), semantic(server))]
        if same_format:
            comparisons.append(('native', target.passthrough[target.origin],
                                server.passthrough[server.origin]))
        for basis, old, new in comparisons:
            records.extend({**item, 'basis': basis}
                           for item in changes(old, new, (server.name,)))
    return sorted(records, key=lambda item: (item['path'], item['basis']))


def print_preview(status, *, changes=(), conflicts=(), servers=None,
                  source_findings=(), base_findings=(), error=None):
    """One versioned CI record; no native config or field values on either stream."""
    # Paths contain untrusted names/keys too. Escape via JSON, without changing
    # segment boundaries; mask known credential signatures even in target keys.
    def safe(value):
        if isinstance(value, str):
            return TOKEN.sub('[REDACTED]', value)
        if isinstance(value, list):
            return [safe(item) for item in value]
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        return value

    record = {'schemaVersion': 2, 'operation': 'three-way-merge', 'status': status,
              'changes': list(changes), 'conflicts': list(conflicts), 'servers': servers,
              'findings': {'source': native_findings(source_findings),
                           'base': native_findings(base_findings)}, 'error': error}
    print(json.dumps(safe(record), indent=2, ensure_ascii=True))


def print_findings(findings, stream=sys.stderr, as_json=False):
    if as_json:
        print(json.dumps({'findings': safe_findings(findings)}, indent=2, ensure_ascii=True), file=stream)
    elif findings:
        print('SERVER\tFIELD\tKIND\tDETAIL', file=stream)
        for item in findings:
            print('\t'.join(label(v) for v in (item.server, item.field, item.kind, item.message)), file=stream)
    else:
        print('No fidelity issues detected.', file=stream)


def document_fidelity(source, target_format):
    """Root diagnostics shared by the two explicit source snapshots."""
    outside_servers = {k: v for k, v in source.data.items() if k != ROOTS[source.format]}
    return document_findings(source, target_format) + [
        Finding('(document)', p, 'secret', 'literal [REDACTED]; use an environment reference',
                segments, 'document')
        for segments, p in secret_locations(outside_servers)]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, 'agentwrench mcp-config: ' + label(message) + '\n')


def parser():
    p = Parser(prog='agentwrench mcp-config', description='Offline, non-destructive MCP native-file conversion. Never starts servers.')
    sub = p.add_subparsers(dest='action', required=True, parser_class=Parser)
    for action in ('convert', 'report', 'check'):
        cmd = sub.add_parser(action)
        cmd.add_argument('--from', dest='source_format', required=True, choices=FORMATS)
        cmd.add_argument('--to', dest='target_format', required=True, choices=FORMATS)
        if action == 'check':
            cmd.add_argument('--source', type=Path, required=True)
            cmd.add_argument('--target', type=Path, required=True)
        else:
            cmd.add_argument('input', type=Path)
        if action == 'convert':
            cmd.add_argument('-o', '--output', type=Path)
            cmd.add_argument('--merge', action='store_true', help='overlay only source server fields onto output')
            cmd.add_argument('--base', type=Path,
                             help='previous source snapshot in --from format; three-way merge into existing output')
            cmd.add_argument('--overwrite', action='store_true', help='allow named-server collisions with --merge')
            cmd.add_argument('--redact-secrets', action='store_true', help='replace detected literal credentials with [REDACTED]')
            cmd.add_argument('--dry-run', action='store_true', help='validate and report planned counts without writing')
        json_options = cmd.add_mutually_exclusive_group() if action == 'report' else cmd
        json_options.add_argument('--json', action='store_true',
                         help='JSON findings (report), drift (check), or three-way preview '
                              '(convert; requires --base --dry-run)')
        if action == 'report':
            json_options.add_argument('--json-v1', action='store_true',
                                      help='version-1 scoped native fidelity report and runtime errors')
            json_options.add_argument('--policy-json-v1', action='store_true',
                                      help='version-1 scoped report policy result; selected matches exit 1')
            json_options.add_argument('--policy-json-v2', action='store_true',
                                      help='version-2 report policy result with optional scope selection')
            json_options.add_argument('--policy-json-v3', action='store_true',
                                      help='version-3 report policy result with independent kind:scope rules')
            json_options.add_argument('--policy-json-v4', action='store_true',
                                      help='version-4 exact native selector result; requires version-2 --policy-file')
            cmd.add_argument('--policy-file', type=Path,
                             help='strict policy JSON: artifact v1 for result v3, artifact v2 for result v4; refuses inline selectors')
            cmd.add_argument('--fail-rule', action='append', choices=POLICY_RULES,
                             help='reject this kind:scope pair; repeatable, requires --policy-json-v3; default: none')
            cmd.add_argument('--fail-scope', action='append', choices=POLICY_SCOPES,
                             help='gate this diagnostic scope; repeatable, requires --policy-json-v2; default: both')
            cmd.add_argument('--fail-on', action='append', choices=POLICY_KINDS, default=[],
                             help='reject this finding kind; repeatable, requires --policy-json-v1 or --policy-json-v2; default: none')
    cmd = sub.add_parser('relocate', help='relocate explicitly selected project paths in one native config')
    cmd.add_argument('--format', choices=FORMATS, help='required unless using --manifest')
    cmd.add_argument('input', type=Path)
    cmd.add_argument('--from-root', help='absolute normalized POSIX source project root')
    cmd.add_argument('--to-root', help='absolute normalized POSIX target project root')
    cmd.add_argument('--path', action='append',
                     help='explicit JSON array: ["server","cwd"] or ["server","args",index]; repeatable')
    manifest = cmd.add_mutually_exclusive_group()
    manifest.add_argument('--plan', action='store_true',
                          help='validate and print a source-digest-bound selection manifest; no writes')
    manifest.add_argument('--manifest', type=Path,
                          help='reuse a selection manifest; refuse any changed source bytes or selector overrides')
    output = cmd.add_mutually_exclusive_group()
    output.add_argument('-o', '--output', type=Path, help='create a new native config file')
    output.add_argument('--in-place', action='store_true', help='replace input with exclusive .bak backup')
    cmd.add_argument('--dry-run', action='store_true', help='print JSON provenance to stdout; write nothing')
    cmd.add_argument('--receipt', action='store_true',
                     help='emit an exact-byte planned/applied/error receipt; requires --manifest')
    return p


def run_relocation(args):
    if args.receipt and not args.manifest:
        raise ConfigError('--receipt requires --manifest')
    if args.plan and (args.dry_run or args.output or args.in_place):
        raise ConfigError('--plan cannot be combined with --dry-run, --output or --in-place')
    supplied = (args.format, args.from_root, args.to_root, args.path)
    if args.manifest and any(value is not None for value in supplied):
        raise ConfigError('--manifest cannot be combined with --format, --from-root, --to-root or --path')
    if not args.manifest and any(value is None for value in supplied):
        raise ConfigError('relocation requires --format, --from-root, --to-root and --path, or --manifest')
    source_text = read_file(args.input)
    if args.manifest:
        manifest_text = read_file(args.manifest)
        manifest = read_manifest(manifest_text, source_text)
        fmt, old_root, new_root, selections = (manifest[key] for key in (
            'format', 'sourceRoot', 'targetRoot', 'paths'))
    else:
        fmt, old_root, new_root = args.format, args.from_root, args.to_root
        selections = [selection(text) for text in args.path]
    source = loads(source_text, fmt)
    # Provenance deliberately exposes selected paths. Refuse detected credentials
    # before rendering either the config or its provenance, including raw comments.
    if (list(secret_paths(source.data))
            or RAW_TOKEN.search(re.sub(r'\$\{\{?[^{}]*\}\}?', '', source_text))):
        raise ConfigError('literal credential detected [REDACTED]; relocation requires environment references')
    if args.plan:
        print(json.dumps(create_manifest(source, old_root, new_root, selections),
                         indent=2, ensure_ascii=True))
        return 0
    result, provenance = relocate(source, old_root, new_root, selections)
    output = args.input if args.in_place else args.output
    expected = source_text.encode('utf-8') if args.in_place else None
    if output:
        validate_write(output, expected)
    # Serialize before publishing any config bytes. An applied record is emitted
    # only after atomic_write or binary stdout write AND flush return successfully.
    receipt = None
    if args.receipt:
        receipt = receipt_json('planned' if args.dry_run else 'applied',
                               manifest_text=manifest_text, source_text=source_text,
                               result=result, provenance=provenance)
    if args.dry_run:
        if args.receipt:
            print_receipt(args, receipt)
        else:
            print(json.dumps(provenance, indent=2, ensure_ascii=True))
    else:
        if output:
            atomic_write(output, result, expected)
        elif args.receipt:
            # Text stdout may re-encode or translate newlines. Receipts attest to
            # these exact UTF-8 bytes, including on non-UTF-8 stdout environments.
            rendered = result.encode('utf-8')
            try:
                if sys.stdout.buffer.write(rendered) != len(rendered):
                    raise ConfigError('native stdout write incomplete (details masked)')
                sys.stdout.buffer.flush()
            except OSError:
                # A buffered broken pipe otherwise flushes again at interpreter
                # shutdown, corrupting the error envelope and changing exit 2 to
                # 120. Discard pending bytes on this failed stream only.
                try:
                    with open(os.devnull, 'wb') as sink:
                        os.dup2(sink.fileno(), sys.stdout.fileno())
                except (AttributeError, OSError, ValueError):
                    pass  # Embedded/custom streams may have no descriptor.
                raise ConfigError('native stdout write failed (details masked)') from None
        else:
            sys.stdout.write(result)
        if args.receipt:
            print_receipt(args, receipt)
        else:
            print(json.dumps(provenance, indent=2, ensure_ascii=True), file=sys.stderr)
    return 0


def run(args):
    if args.action == 'relocate':
        return run_relocation(args)
    as_json = args.action == 'convert' and args.json
    if as_json and (not args.base or not args.dry_run):
        raise ConfigError('--json requires --base and --dry-run; JSON conversion output is preview-only')
    if args.action == 'convert' and args.base:
        if not args.merge or not args.output:
            raise ConfigError('--base requires --merge and an explicit existing output file')
        if args.overwrite or args.redact_secrets:
            raise ConfigError('--base cannot be combined with --overwrite or --redact-secrets')
        if not os.path.lexists(args.output):
            raise ConfigError('--base requires an existing output file')
    source_path = args.source if args.action == 'check' else args.input
    source_text = read_file(source_path)
    source = loads(source_text, args.source_format)
    servers = to_ir(source)
    projected, findings = project(servers, args.target_format)
    # Keep server fidelity separate from document findings: a real server may
    # itself be named "(document)" and its unrepresentable settings still drift.
    server_losses = [f for f in findings if f.kind in ('loss', 'error')]
    findings += document_fidelity(source, args.target_format)
    if args.action == 'report':
        unsupported = any(f.kind == 'error' for f in findings)
        if args.policy_json_v1 or args.policy_json_v2 or args.policy_json_v3 or args.policy_json_v4:
            scopes = (args.fail_scope or POLICY_SCOPES) if args.policy_json_v2 else None
            rules = (args.fail_rule or []) if args.policy_json_v3 else None
            exact_rules = args.exact_rules if args.policy_json_v4 else None
            rejected = bool(selector_matches(findings, exact_rules) if exact_rules is not None else
                            rule_matches(findings, rules) if rules is not None else
                            policy_matches(findings, args.fail_on, scopes or POLICY_SCOPES))
            status = 'unsupported' if unsupported else 'rejected' if rejected else 'accepted'
            print_policy_report(status, args.fail_on, scopes=scopes, rules=rules, exact_rules=exact_rules, findings=findings,
                                error='unsupported server conversion; no files written' if unsupported else None)
            return 3 if unsupported else 1 if rejected else 0
        elif args.json_v1:
            print_report('unsupported' if unsupported else 'reported', findings=findings,
                         error='unsupported server conversion; no files written' if unsupported else None)
        else:
            print_findings(findings, sys.stdout, args.json)
        return 3 if unsupported else 0
    if args.action == 'check':
        target = loads(read_file(args.target), args.target_format)
        actual = {s.name: s for s in to_ir(target)}
        expected_doc = loads(new_document(args.target_format, projected), args.target_format)
        expected = to_ir(expected_doc)
        if args.json:
            drift = check_drift(expected, actual, args.source_format == args.target_format)
            has_drift = bool(drift or server_losses)
            print_check('drift' if has_drift else 'in-sync', drift=drift, findings=findings)
            return 1 if has_drift else 0
        drift = []
        for server in expected:
            if server.name not in actual:
                drift.append((server.name, '(server missing)'))
                continue
            drift.extend((server.name, p) for p in diff_paths(semantic(server), semantic(actual[server.name])))
            if args.source_format == args.target_format:
                drift.extend((server.name, p) for p in diff_paths(server.passthrough, actual[server.name].passthrough))
        for item in server_losses:
            drift.append((item.server, item.field + ' (unrepresentable)'))
        print_findings(findings)
        for name, path in drift:
            print(f'DRIFT {label(name)}.{label(path)}: source and target differ (values masked)')
        if not drift:
            print(f'In sync: {plural(len(expected), "source server")}; unrelated target servers ignored.')
        return 1 if drift else 0
    if args.overwrite and not args.merge:
        raise ConfigError('--overwrite requires --merge')
    if args.merge and not args.output:
        raise ConfigError('--merge requires an explicit output file')
    # Scan the entire source, not just mapped fields. Never print parser exception text.
    sensitive = list(secret_paths(source.data))
    # Comments are not structured values; known credential token signatures still block them.
    if RAW_TOKEN.search(re.sub(r'\$\{\{?[^{}]*\}\}?', '', source_text)):
        sensitive.append('(raw token signature, possibly in a comment)')
    if sensitive and not args.redact_secrets:
        raise ConfigError('literal credential detected [REDACTED]; use environment references or --redact-secrets')
    if not as_json:
        print_findings(findings)
    errors = any(f.kind == 'error' for f in findings)
    base_projected = None
    base_findings = []
    if args.base:
        base = loads(read_file(args.base), args.source_format)
        base_projected, base_findings = project(to_ir(base), args.target_format)
        if as_json:
            base_findings += document_fidelity(base, args.target_format)
        if base_findings and not as_json:
            print('Base snapshot fidelity:', file=sys.stderr)
            print_findings(base_findings)
        errors = errors or any(f.kind == 'error' for f in base_findings)
    if errors and (args.base or not projected):
        # An omitted unsupported server must never be interpreted as a deletion.
        if as_json:
            print_preview('error', source_findings=findings, base_findings=base_findings,
                          error='unsupported server conversion; no files written')
        return 3
    if args.redact_secrets:
        # Remove token-looking comments too: native source text cannot be reused safely.
        projected = {name: redacted(raw) for name, raw in projected.items()}
    target = None
    expected_bytes = None
    if args.output and os.path.lexists(args.output):
        if not args.merge:
            raise ConfigError('output exists; use --merge and --overwrite for deliberate field updates')
        target_text = read_file(args.output)
        expected_bytes = target_text.encode('utf-8')
        target = loads(target_text, args.target_format)
        to_ir(target)  # Never merge into a malformed server collection.
        collisions = set(target.servers) & set(projected)
        if collisions and not args.overwrite and not args.base:
            raise ConfigError('server name collision; use --overwrite with --merge after review')
    if args.base:
        merged, conflicts = three_way(base_projected, projected, target.servers)
        if conflicts:
            if as_json:
                print_preview('conflict', conflicts=conflict_details(
                    base_projected, projected, target.servers, conflicts),
                    source_findings=findings, base_findings=base_findings,
                    error='three-way merge conflicts; no files written')
                return 2
            for path in conflicts:
                print(f'CONFLICT {label(".".join(path))}: both sides changed (values masked)', file=sys.stderr)
            raise ConfigError('three-way merge conflicts; no files written')
        result = target.render_merged(merged)
    elif target:
        result = target.render(projected)
    elif args.source_format == args.target_format and not args.redact_secrets:
        result = source.text
    else:
        result = new_document(args.target_format, projected)
    if args.redact_secrets and not target and RAW_TOKEN.search(result):
        raise ConfigError('credential signature in a name/key cannot be safely redacted; rename it before conversion')
    if args.redact_secrets:
        print('Redacted server-only output: literal credentials replaced; source document metadata/comments are not copied.', file=sys.stderr)
    # Validate the complete edited output before any backup or mutation.
    to_ir(loads(result, args.target_format))
    if as_json:
        print_preview('preview', changes=changes(target.servers, merged),
                      servers={'added': len(merged.keys() - target.servers.keys()),
                               'removed': len(target.servers.keys() - merged.keys()),
                               'total': len(merged)},
                      source_findings=findings, base_findings=base_findings)
        return 0
    if args.dry_run:
        operation = 'three-way merge' if args.base else 'merge' if target else 'create'
        if args.base:
            for path in diff_paths(target.servers, merged):
                print(f'CHANGE {label(path)}: target would change (values masked)')
            added = len(merged.keys() - target.servers.keys())
            removed = len(target.servers.keys() - merged.keys())
            print(f'PLAN: {plural(added, "server")} added; {removed} removed; {len(merged)} total after merge.')
        print(f'DRY RUN: {plural(len(projected), "source server")}; {operation}; no files written.')
        return 3 if errors else 0
    if args.output:
        if expected_bytes == result.encode('utf-8'):
            print('Unchanged; no write or backup needed.', file=sys.stderr)
        else:
            atomic_write(args.output, result, expected_bytes)
            count = len(merged) if args.base else len(projected)
            print(f'Wrote {plural(count, "server")}; output path masked.', file=sys.stderr)
    else:
        sys.stdout.write(result)
    return 3 if errors else 0


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.action == 'report':
        if args.policy_json_v4 and args.policy_file is None:
            p.error('--policy-json-v4 requires --policy-file')
        if args.policy_file is not None:
            if not (args.policy_json_v3 or args.policy_json_v4):
                p.error('--policy-file requires --policy-json-v3 or --policy-json-v4')
            if args.fail_rule is not None or args.fail_on or args.fail_scope is not None:
                p.error('--policy-file refuses --fail-rule, --fail-on and --fail-scope overrides')
        if args.fail_on and not (args.policy_json_v1 or args.policy_json_v2):
            p.error('--fail-on requires --policy-json-v1 or --policy-json-v2')
        if args.fail_scope is not None and not args.policy_json_v2:
            p.error('--fail-scope requires --policy-json-v2')
        if args.fail_rule is not None and not args.policy_json_v3:
            p.error('--fail-rule requires --policy-json-v3')
    as_json = args.action == 'convert' and args.json
    json_check = args.action == 'check' and args.json
    json_report = args.action == 'report' and args.json_v1
    policy_report = args.action == 'report' and (args.policy_json_v1 or args.policy_json_v2 or args.policy_json_v3 or args.policy_json_v4)
    policy_scopes = (args.fail_scope or POLICY_SCOPES) if policy_report and args.policy_json_v2 else None
    policy_rules = (args.fail_rule or []) if policy_report and args.policy_json_v3 else None
    exact_rules = [] if policy_report and args.policy_json_v4 else None
    receipt = args.action == 'relocate' and args.receipt
    try:
        if policy_report and args.policy_file is not None:
            # Commit the effective rules only after the whole artifact validates,
            # before run() can open the native input or collect any findings.
            text = read_file(args.policy_file, max_bytes=POLICY_MAX_BYTES)
            if args.policy_json_v4:
                exact_rules = parse_policy(text, version=2)
                args.exact_rules = exact_rules
            else:
                policy_rules = parse_policy(text)
                args.fail_rule = policy_rules
        return run(args)
    except ConfigError as exc:
        if receipt:
            print_receipt(args, receipt_json('error', error=str(exc)))
        elif policy_report:
            print_policy_report('error', args.fail_on, scopes=policy_scopes, rules=policy_rules, exact_rules=exact_rules, error=str(exc))
        elif json_report:
            print_report('error', error=str(exc))
        elif json_check:
            print_check('error', error=str(exc))
        elif as_json:
            print_preview('error', error=str(exc))
        else:
            print('agentwrench mcp-config: ' + label(str(exc)), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, RecursionError):
        # Do not echo hostile document content in third-party serializer exceptions.
        message = 'configuration cannot be processed safely (details masked)'
        if receipt:
            print_receipt(args, receipt_json('error', error=message))
        elif policy_report:
            print_policy_report('error', args.fail_on, scopes=policy_scopes, rules=policy_rules, exact_rules=exact_rules, error=message)
        elif json_report:
            print_report('error', error=message)
        elif json_check:
            print_check('error', error=message)
        elif as_json:
            print_preview('error', error=message)
        else:
            print('agentwrench mcp-config: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
