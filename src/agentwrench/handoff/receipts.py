"""In-toto receipt subcommands of `agentwrench handoff` (needs the `handoff` extra).

Imported by ``cli.py`` only after the arguments are parsed and the extra is
known to be installed. JSON on stdout; bounded errors on stderr.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from pydantic import ValidationError

from .core import capture, describe, export_diff, read_manifest, record, verify
from .evidence import load_keys
from .io import HandoffError
from .models import Manifest, Task


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False))


def run(args: argparse.Namespace) -> int:
    try:
        if args.action == "capture":
            manifest = capture(
                repo=args.repo, task_path=args.task, logs=args.log, output=args.output,
                base=args.base, supplied_diff=args.diff, force=args.force,
            )
            emit({"output": str(args.output), "head": manifest.snapshot.head, "source_state_sha256": manifest.snapshot.state_sha256, "command_logs": len(manifest.evidence), "task_completion": "not_assessed"})
            return 0
        if args.action == "verify":
            result = verify(
                read_manifest(args.handoff), repo=None if args.bundle_only else args.repo,
                keys=load_keys(args.key), require_signed=args.require_signed,
                require_current_pass=args.require_current_pass,
            )
            emit(result)
            return 0 if result["ok"] else 1
        if args.action == "record":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            code, result = record(
                repo=args.repo, argv=command, output=args.output, name=args.name,
                products=args.product, timeout=args.timeout, signing_key=args.signing_key,
                dsse=args.dsse, force=args.force,
            )
            emit(result)
            return 0 if code == 0 and not result["missing_requested_products"] else 1
        if args.action == "inspect":
            emit(describe(read_manifest(args.manifest)))
            return 0
        if args.action == "diff":
            emit(export_diff(repo=args.repo, output=args.output, force=args.force))
            return 0
        emit((Manifest if args.kind == "manifest" else Task).model_json_schema())
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"error": "operation interrupted; verify any existing output before using it"}), file=sys.stderr)
        return 130
    except ValidationError as exc:
        # Avoid Pydantic's default repr, which embeds arbitrary input contents.
        # Unknown object keys are input too: report only schema-owned field names.
        schema = Manifest.model_json_schema()
        known = set(schema.get("properties", {}))
        for definition in schema.get("$defs", {}).values():
            known.update(definition.get("properties", {}))
        issues = exc.errors(include_input=False)[:10]
        fields = [
            ".".join(str(part) if isinstance(part, int) or part in known else "<unknown>" for part in item["loc"])[:256]
            for item in issues
        ]
        print(json.dumps({"error": "schema validation failed", "fields": fields, "issue_types": [item["type"] for item in issues]}), file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "recorded command timed out; no completed receipt was written"}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": f"local operation failed ({exc.strerror or 'operating system error'})", "errno": exc.errno}), file=sys.stderr)
        return 2
    except (HandoffError, ValueError) as exc:
        message = str(exc) if isinstance(exc, HandoffError) else "local operation failed (details omitted)"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 2
