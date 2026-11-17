"""Archive a captured JSON result before applying any lossy observation cap.

The backend call has already completed. The caller retains its original structured
result separately; only this returned text belongs in the agent's observation history.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentwrench.sift import Rules, Summary, summarize, verify


def observation(raw: bytes, archive: Path, *, budget: int) -> Summary:
    # Exclusive creation protects an earlier recovery artifact from being overwritten.
    # Keep this before truncation: BudgetError must not cause a backend call to be retried.
    with archive.open("xb") as stream:
        stream.write(raw)
    payload = json.loads(raw)
    # MCP's isError is retained when present; a missing flag is never synthesized as false.
    keep = ("/isError",) if isinstance(payload, dict) and "isError" in payload else ()
    result = summarize(raw, format="json", budget=budget, rules=Rules(keep=keep))
    verify(raw, result.text, budget=budget)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="already captured JSON tool result")
    parser.add_argument(
        "--archive", type=Path, required=True, help="new recovery file; must not exist"
    )
    parser.add_argument("--budget", type=int, default=1600)
    args = parser.parse_args()
    result = observation(args.input.read_bytes(), args.archive, budget=args.budget)
    sys.stdout.buffer.write(result.text.encode("utf-8"))


if __name__ == "__main__":
    main()
