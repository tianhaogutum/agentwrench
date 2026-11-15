import sys

from agentwrench.cli import main

raise SystemExit(main(["checkpoints", *sys.argv[1:]]))
