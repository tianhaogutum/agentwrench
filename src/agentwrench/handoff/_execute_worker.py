"""Private POSIX worker: reuse in-toto execution inside a disposable process group."""

import json
import subprocess
import sys

from in_toto.runlib import execute_link


def main() -> int:
    request = json.load(sys.stdin)
    try:
        result = execute_link(request["argv"], record_streams=False, timeout=request["timeout"])
    except subprocess.TimeoutExpired:
        json.dump({"error": "timeout"}, sys.stdout)
        return 2
    except Exception:
        json.dump({"error": "execution_failed"}, sys.stdout)
        return 2
    json.dump({"byproducts": result}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
