import sys

from agentwrench.cli import main

raise SystemExit(main(["patch", *sys.argv[1:]]))
