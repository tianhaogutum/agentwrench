import sys

from agentwrench.cli import main

raise SystemExit(main(["scan", *sys.argv[1:]]))
