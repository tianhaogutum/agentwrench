import sys

from agentwrench.cli import main

raise SystemExit(main(["handoff", *sys.argv[1:]]))
